# ruff: noqa: E501
"""Creates the objects the simulator needs: device metric mappings and the 100 inlet sensors. Every create is
look-up-first, so a second run changes nothing."""

import hashlib
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.placement_service import get_current_equipment_placement, move_equipment
from app.application.spatial_validation import validate_u_range_against_rack_capacity
from app.domain.cooling.models import SENSOR_KIND_METRICS, EnvironmentalSensor
from app.domain.identity.models import ALLOWED_LIFECYCLE_TRANSITIONS, ManagedAsset
from app.domain.placement.models import EquipmentPlacement
from app.domain.telemetry.models import IntegrationMetricMapping, IntegrationMetricMappingRevision
from app.domain.telemetry.registry import METRIC_REGISTRY, validate_metric_unit
from app.tools.demo_loader import LOADER_VERSION
from app.tools.demo_loader.constants import (
    DEVICE_METRICS,
    MAPPING_LABEL,
    SENSOR_MARKER_KEY,
    SENSOR_METRICS,
    SENSOR_TAG_PREFIX,
    SOURCE_PREFIX,
)
from app.tools.demo_loader.selection import DeviceInfo, IntegrationRoute, LoaderError, RackInfo, Targets

AUDIT_REASON = f"demo_loader {LOADER_VERSION}"
SENSOR_ELEVATION_PER_U_MM = 44.45
AUDIT_SOURCE = "system"  # the audit table only allows ui/api/system/import; `reason` names the loader


MAX_TAG = 64
_HASH_CHARS = 10


def sensor_tag(rack_tag: str) -> str:
    """`DEMO-ENV-<rack tag>`. A rack tag too long for the 64-character asset tag keeps its start and gains a hash of the whole
    tag, so two long tags that share their first characters still get different sensor tags."""
    full = f"{SENSOR_TAG_PREFIX}{rack_tag}"
    if len(full) <= MAX_TAG:
        return full
    digest = hashlib.sha256(rack_tag.encode()).hexdigest()[:_HASH_CHARS]
    keep = MAX_TAG - len(SENSOR_TAG_PREFIX) - 1 - _HASH_CHARS
    return f"{SENSOR_TAG_PREFIX}{rack_tag[:keep]}-{digest}"


def mapping_source(asset_tag: str, metric: str) -> str:
    return f"{SOURCE_PREFIX}{asset_tag}:{metric}"[:255]


async def ensure_mappings(
    db: AsyncSession, *, asset_id: uuid.UUID, asset_tag: str, metrics: tuple[str, ...], route: IntegrationRoute,
) -> tuple[list[IntegrationMetricMapping], int]:
    """The asset's loader mappings (asset-specific source identifier, canonical unit, scale 1), creating missing ones.
    A new mapping is created through the ORM, which seeds its authored revision 1 in the same transaction."""
    created = 0
    result: list[IntegrationMetricMapping] = []
    for metric in metrics:
        source = mapping_source(asset_tag, metric)
        mapping = (
            await db.execute(
                select(IntegrationMetricMapping).where(
                    IntegrationMetricMapping.integration_id == route.integration.id, IntegrationMetricMapping.source_identifier == source
                )
            )
        ).scalar_one_or_none()
        if mapping is None:
            unit = METRIC_REGISTRY[metric].canonical_unit
            validate_metric_unit(metric, unit)
            mapping = IntegrationMetricMapping(
                id=uuid.uuid4(), integration_id=route.integration.id, managed_asset_id=asset_id, source_identifier=source,
                canonical_metric=metric, unit=unit, scale=1, label=MAPPING_LABEL,
            )
            db.add(mapping)
            await db.flush()
            created += 1
        elif mapping.managed_asset_id != asset_id:
            raise LoaderError(f"Mapping {source!r} on {route.integration.name!r} already belongs to another asset.")
        result.append(mapping)
    return result, created


async def preflight_existing_mappings(db: AsyncSession, targets: Targets) -> None:
    """Refuse, before any write, to reuse a mapping that merely carries a loader-style source identifier.

    A mapping named `demo-sim:...` on a routed integration must be this loader's: the "Demo simulator" label, the metric
    that its source identifier names, the canonical unit with scale 1, and a current revision that says the same. Anything
    else could route synthetic readings into an unrelated series."""
    integration_ids = [route.integration.id for route in targets.routes.values()]
    rows = (
        await db.execute(
            select(IntegrationMetricMapping, IntegrationMetricMappingRevision)
            .outerjoin(IntegrationMetricMappingRevision, IntegrationMetricMappingRevision.id == IntegrationMetricMapping.current_revision_id)
            .where(IntegrationMetricMapping.integration_id.in_(integration_ids), IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX))
        )
    ).all()
    problems: list[str] = []
    for mapping, revision in rows:
        metric = mapping.source_identifier.rsplit(":", 1)[-1]
        canonical_unit = METRIC_REGISTRY[metric].canonical_unit if metric in METRIC_REGISTRY else None
        wrong = []
        if mapping.label != MAPPING_LABEL:
            wrong.append("label")
        if metric not in (*DEVICE_METRICS, *SENSOR_METRICS) or mapping.canonical_metric != metric:
            wrong.append("metric")
        if canonical_unit is None or mapping.unit != canonical_unit or Decimal(str(mapping.scale)) != 1:
            wrong.append("unit/scale")
        if revision is None or revision.canonical_metric != mapping.canonical_metric or revision.source_unit != mapping.unit or Decimal(str(revision.source_scale)) != Decimal(str(mapping.scale)):
            wrong.append("revision")
        if wrong:
            problems.append(f"{mapping.source_identifier} ({', '.join(wrong)})")
    if problems:
        raise LoaderError(
            f"{len(problems)} existing mapping(s) use a loader source identifier but are not this loader's: {problems[:5]}. "
            "Nothing was written. Rename or remove them first."
        )


@dataclass
class DeviceReport:
    devices: int = 0
    mappings_created: int = 0
    mappings_total: int = 0


async def ensure_device_mappings(db: AsyncSession, targets: Targets) -> tuple[DeviceReport, list[IntegrationMetricMapping]]:
    await preflight_existing_mappings(db, targets)
    report = DeviceReport()
    mappings: list[IntegrationMetricMapping] = []
    for index, device in enumerate(targets.devices, start=1):
        route = targets.routes[device.site_id]
        made, created = await ensure_mappings(db, asset_id=device.id, asset_tag=device.asset_tag, metrics=DEVICE_METRICS, route=route)
        mappings.extend(made)
        report.devices += 1
        report.mappings_created += created
        if index % 100 == 0:
            await db.commit()
    await db.commit()
    report.mappings_total = len(mappings)
    return report, mappings


def free_front_slot(height_u: int, occupied: set[int]) -> int | None:
    """A free single U on the front face, searched outward from two-thirds of the way up the rack."""
    centre = max(1, min(height_u, round(height_u * 2 / 3)))
    for offset in range(height_u):
        for u in (centre - offset, centre + offset):
            if 1 <= u <= height_u and u not in occupied:
                return u
    return None


async def occupied_front_units(db: AsyncSession, rack_id: uuid.UUID) -> set[int]:
    rows = await db.execute(
        select(EquipmentPlacement.u_range).where(
            EquipmentPlacement.rack_id == rack_id, EquipmentPlacement.effective_to.is_(None),
            EquipmentPlacement.u_range.is_not(None), EquipmentPlacement.occupies_front.is_(True),
        )
    )
    units: set[int] = set()
    for (u_range,) in rows.all():
        if u_range is not None and u_range.lower is not None and u_range.upper is not None:
            units.update(range(u_range.lower, u_range.upper))
    return units


async def transition(db: AsyncSession, asset: ManagedAsset, to_status: str) -> None:
    if to_status not in ALLOWED_LIFECYCLE_TRANSITIONS.get(asset.lifecycle_status, set()):
        raise LoaderError(f"{asset.asset_tag}: cannot move from {asset.lifecycle_status!r} to {to_status!r}.")
    before = asset.lifecycle_status
    asset.lifecycle_status = to_status
    await db.flush()
    await write_audit_log(
        db, actor_user_id=None, action="managed_asset.lifecycle_transition", entity_type="managed_asset", entity_id=asset.id,
        request_id=None, correlation_id=None, source=AUDIT_SOURCE, reason=AUDIT_REASON, before={"lifecycle_status": before}, after={"lifecycle_status": to_status},
    )


@dataclass
class SensorReport:
    racks: int = 0
    created: int = 0
    commissioned: int = 0
    placed: int = 0
    mappings_created: int = 0
    problems: list[str] = field(default_factory=list)
    mappings: list[IntegrationMetricMapping] = field(default_factory=list)


async def ensure_sensor(db: AsyncSession, rack: RackInfo, route: IntegrationRoute, report: SensorReport) -> None:
    tag = sensor_tag(rack.asset_tag)
    asset = (await db.execute(select(ManagedAsset).where(ManagedAsset.asset_tag == tag))).scalar_one_or_none()
    if asset is None:
        asset = ManagedAsset(
            asset_type="sensor", asset_tag=tag, lifecycle_status="planned",
            external_ids={SENSOR_MARKER_KEY: {"version": LOADER_VERSION, "rack_id": str(rack.id)}},
        )
        db.add(asset)
        await db.flush()
        sensor: EnvironmentalSensor | None = EnvironmentalSensor(
            id=asset.id, site_id=rack.site_id, name=f"{rack.name} inlet T/RH", sensor_kind="combined", measurement_role="rack_inlet",
            notes="Simulated combined temperature/humidity inlet sensor (demo loader).",
        )
        db.add(sensor)
        await db.flush()
        await write_audit_log(
            db, actor_user_id=None, action="cooling.sensor.create", entity_type="sensor", entity_id=asset.id, request_id=None,
            correlation_id=None, source=AUDIT_SOURCE, reason=AUDIT_REASON, after={"asset_tag": tag, "sensor_kind": "combined", "site_id": str(rack.site_id)},
        )
        report.created += 1
    else:
        sensor = await db.get(EnvironmentalSensor, asset.id)
        if sensor is None or sensor.site_id != rack.site_id or not owned_by_loader(asset, rack):
            raise LoaderError(f"{tag} exists but is not this loader's sensor for rack {rack.asset_tag}; refusing to touch it.")
    assert sensor is not None
    for current, following in (("planned", "installed"), ("installed", "active")):
        if asset.lifecycle_status == current:
            await transition(db, asset, following)
            report.commissioned += 1
    if asset.lifecycle_status != "active":
        report.problems.append(f"{tag}: lifecycle is {asset.lifecycle_status!r}, expected 'active'.")
    placement = await get_current_equipment_placement(db, asset.id)
    if placement is None:
        occupied = await occupied_front_units(db, rack.id)
        u = free_front_slot(rack.height_u, occupied)
        if u is None:
            report.problems.append(f"{tag}: rack {rack.asset_tag} has no free front U for a sensor.")
            return
        validate_u_range_against_rack_capacity(u_start=u, u_end=u + 1, rack_height_u=rack.height_u)
        await move_equipment(
            db, equipment_id=asset.id, placement_type="rack_mounted", room_id=rack.room_id, rack_id=rack.id, u_start=u, u_end=u + 1,
            side="front",
        )
        sensor.elevation_mm = int((u - 1) * SENSOR_ELEVATION_PER_U_MM)
        await db.flush()
        await write_audit_log(
            db, actor_user_id=None, action="cooling.placement.set", entity_type="managed_asset", entity_id=asset.id, request_id=None,
            correlation_id=None, source=AUDIT_SOURCE, reason=AUDIT_REASON,
            after={"room_id": str(rack.room_id), "placement_type": "rack_mounted", "rack_id": str(rack.id), "u_start": u, "u_end": u + 1},
        )
        report.placed += 1
    elif placement.rack_id != rack.id:
        report.problems.append(f"{tag}: already placed in another rack; left unchanged.")
    made, created = await ensure_mappings(db, asset_id=asset.id, asset_tag=tag, metrics=SENSOR_METRICS, route=route)
    for mapping in made:
        if mapping.canonical_metric not in SENSOR_KIND_METRICS[sensor.sensor_kind]:
            raise LoaderError(f"{tag}: metric {mapping.canonical_metric} is not allowed for a {sensor.sensor_kind} sensor.")
    report.mappings_created += created
    report.mappings.extend(made)


def owned_by_loader(asset: ManagedAsset, rack: RackInfo) -> bool:
    marker = (asset.external_ids or {}).get(SENSOR_MARKER_KEY)
    return asset.asset_type == "sensor" and isinstance(marker, dict) and marker.get("rack_id") == str(rack.id)


async def preflight_sensors(db: AsyncSession, racks: list[RackInfo]) -> None:
    """Refuse before any write if two racks would share a sensor tag, or a sensor with a wanted tag is not ours for that rack."""
    by_tag: dict[str, RackInfo] = {}
    for rack in racks:
        tag = sensor_tag(rack.asset_tag)
        if tag in by_tag:
            raise LoaderError(f"Racks {by_tag[tag].asset_tag!r} and {rack.asset_tag!r} would share the sensor tag {tag!r}; nothing was written.")
        by_tag[tag] = rack
    existing = (await db.execute(select(ManagedAsset).where(ManagedAsset.asset_tag.in_(list(by_tag))))).scalars().all()
    for asset in existing:
        if not owned_by_loader(asset, by_tag[asset.asset_tag]):
            raise LoaderError(
                f"Asset {asset.asset_tag!r} already exists and is not this loader's sensor for rack {by_tag[asset.asset_tag].asset_tag!r}; "
                "nothing was written. Rename or retire it first."
            )


async def ensure_sensors(db: AsyncSession, targets: Targets) -> SensorReport:
    await preflight_sensors(db, targets.racks)
    await preflight_existing_mappings(db, targets)
    report = SensorReport()
    for index, rack in enumerate(targets.racks, start=1):
        await ensure_sensor(db, rack, targets.routes[rack.site_id], report)
        report.racks += 1
        if index % 20 == 0:
            await db.commit()
    await db.commit()
    return report


def device_ids(devices: list[DeviceInfo]) -> set[uuid.UUID]:
    return {d.id for d in devices}
