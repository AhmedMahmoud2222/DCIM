# ruff: noqa: E501
"""Finds the demo data the loader may touch, and nothing else.

Scope rule: a site is in scope only when its code starts with `DEMO_SITE_PREFIX`. Every query below filters on that
set of site ids, so a fourth site (HRC) is never selected, written, or counted as a target."""

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.collector_service import current_assignment
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.location.models import Building, Floor, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.tools.demo_loader.constants import DEMO_SITE_PREFIX


class LoaderError(Exception):
    """A condition the operator must fix; the message says what and how."""


_PREFIX_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")


def validate_site_prefix(prefix: str) -> str:
    """The demo-site boundary is fixed: a selector must be `DEMO-SITE-` or longer, made of letters, digits and hyphens.

    `--site-prefix HRC`, an empty selector and anything containing LIKE wildcards (`%`, `_`) are refused before any
    query, so no selector can reach a site outside the demo sites."""
    if not prefix.startswith(DEMO_SITE_PREFIX) or not set(prefix) <= _PREFIX_CHARS:
        raise LoaderError(
            f"--site-prefix {prefix!r} is not allowed: it must start with {DEMO_SITE_PREFIX!r} and contain only letters, "
            "digits and hyphens (for example DEMO-SITE-02)."
        )
    return prefix


@dataclass(frozen=True)
class SiteInfo:
    id: uuid.UUID
    code: str
    name: str


@dataclass(frozen=True)
class RackInfo:
    id: uuid.UUID
    asset_tag: str
    name: str
    site_id: uuid.UUID
    room_id: uuid.UUID
    x_mm: int | None
    y_mm: int | None
    rotation_deg: int | None
    height_u: int
    width_mm: int
    depth_mm: int


@dataclass(frozen=True)
class DeviceInfo:
    id: uuid.UUID
    asset_tag: str
    site_id: uuid.UUID


@dataclass(frozen=True)
class IntegrationRef:
    """Plain copy of the integration fields the loader needs. A rollback expires ORM instances, and an AsyncSession cannot
    reload them lazily, so nothing that must outlive a failed batch is kept as an ORM object."""

    id: uuid.UUID
    name: str
    site_id: uuid.UUID | None
    poll_interval_seconds: int


@dataclass(frozen=True)
class CollectorRef:
    id: uuid.UUID
    name: str


@dataclass(frozen=True)
class IntegrationRoute:
    """Where a site's synthetic series are ingested: the integration and the collector currently assigned to it."""

    integration: IntegrationRef
    collector: CollectorRef


@dataclass
class Targets:
    sites: list[SiteInfo] = field(default_factory=list)
    racks: list[RackInfo] = field(default_factory=list)
    devices: list[DeviceInfo] = field(default_factory=list)
    routes: dict[uuid.UUID, IntegrationRoute] = field(default_factory=dict)  # site id -> route


async def demo_sites(db: AsyncSession, prefix: str = DEMO_SITE_PREFIX) -> list[SiteInfo]:
    validate_site_prefix(prefix)
    rows = (await db.execute(select(Site.id, Site.code, Site.name).where(Site.code.startswith(prefix)).order_by(Site.code))).all()
    return [SiteInfo(*row) for row in rows]


async def demo_racks(db: AsyncSession, site_ids: list[uuid.UUID]) -> list[RackInfo]:
    stmt = (
        select(
            Rack.id, ManagedAsset.asset_tag, Rack.name, Building.site_id, RackPlacement.room_id, RackPlacement.x_mm,
            RackPlacement.y_mm, RackPlacement.rotation_deg, RackModelRevision.height_u, RackModelRevision.width_mm,
            RackModelRevision.depth_mm,
        )
        .join(ManagedAsset, ManagedAsset.id == Rack.id)
        .join(RackModelRevision, RackModelRevision.id == Rack.model_revision_id)
        .join(RackPlacement, (RackPlacement.rack_id == Rack.id) & RackPlacement.effective_to.is_(None))
        .join(Room, Room.id == RackPlacement.room_id)
        .join(Floor, Floor.id == Room.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .where(Building.site_id.in_(site_ids), ManagedAsset.lifecycle_status.notin_(("decommissioned", "removed")))
        .order_by(Building.site_id, ManagedAsset.asset_tag)
    )
    return [RackInfo(*row) for row in (await db.execute(stmt)).all()]


async def demo_devices(db: AsyncSession, site_ids: list[uuid.UUID]) -> list[DeviceInfo]:
    stmt = (
        select(Equipment.id, ManagedAsset.asset_tag, Building.site_id)
        .join(ManagedAsset, ManagedAsset.id == Equipment.id)
        .join(EquipmentPlacement, (EquipmentPlacement.equipment_id == Equipment.id) & EquipmentPlacement.effective_to.is_(None))
        .join(Room, Room.id == EquipmentPlacement.room_id)
        .join(Floor, Floor.id == Room.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .where(Building.site_id.in_(site_ids), ManagedAsset.lifecycle_status.notin_(("decommissioned", "removed")))
        .order_by(ManagedAsset.asset_tag)
    )
    return [DeviceInfo(*row) for row in (await db.execute(stmt)).all()]


async def route_for_integration(db: AsyncSession, integration: Integration) -> IntegrationRoute:
    assignment = await current_assignment(db, integration.id)
    if assignment is None:
        raise LoaderError(f"Integration {integration.name!r} has no current collector assignment; assign a collector first.")
    collector = await db.get(Collector, assignment.collector_id)
    assert collector is not None
    return IntegrationRoute(
        integration=IntegrationRef(integration.id, integration.name, integration.site_id, integration.poll_interval_seconds),
        collector=CollectorRef(collector.id, collector.name),
    )


async def resolve_routes(
    db: AsyncSession, sites: list[SiteInfo], overrides: dict[str, str] | None = None,
) -> dict[uuid.UUID, IntegrationRoute]:
    """Exactly one integration per selected demo site, and each integration serves exactly one selected site.

    An integration is compatible with a site when its `site_id` is that site, or when it has no site at all and is
    chosen unambiguously: by an explicit `--integration SITE=NAME`, or because its name contains this demo site's code
    and no other demo site's code. An integration that belongs to another site (HRC, or a different demo site) is never
    compatible, whether it was found by name or named on the command line. Anything ambiguous is an error."""
    overrides = overrides or {}
    for code in overrides:
        if code not in {s.code for s in sites}:
            raise LoaderError(f"--integration names site {code!r}, which is not one of the selected demo sites.")
    enabled = select(Integration).where(Integration.enabled.is_(True)).order_by(Integration.name)
    integrations = list((await db.execute(enabled)).scalars())
    site_codes = {sid: code for sid, code in (await db.execute(select(Site.id, Site.code))).all()}
    demo_codes = [code for code in site_codes.values() if code.startswith(DEMO_SITE_PREFIX)]
    chosen: dict[uuid.UUID, Integration] = {}
    for site in sites:
        if site.code in overrides:
            named = [i for i in integrations if i.name == overrides[site.code]]
            if len(named) != 1:
                raise LoaderError(f"--integration {site.code}={overrides[site.code]!r}: no single enabled integration has that name.")
            owner = named[0].site_id
            if owner is not None and owner != site.id:
                raise LoaderError(
                    f"Integration {named[0].name!r} belongs to site {site_codes.get(owner, owner)}, not {site.code}; refusing to route "
                    f"{site.code} through it."
                )
            matches = named
        else:
            by_site = [i for i in integrations if i.site_id == site.id]
            others = [c for c in demo_codes if c != site.code]
            by_name = [
                i for i in integrations
                if i.site_id is None and site.code.lower() in i.name.lower() and not any(o.lower() in i.name.lower() for o in others)
            ]
            matches = by_site or by_name
        if len(matches) != 1:
            raise LoaderError(
                f"Cannot choose one enabled integration for {site.code}: {len(matches)} candidates "
                f"({', '.join(i.name for i in matches) or 'none'}). Pass --integration {site.code}=<integration name>."
            )
        chosen[site.id] = matches[0]
    seen: dict[uuid.UUID, str] = {}
    for site in sites:
        integration = chosen[site.id]
        if integration.id in seen:
            raise LoaderError(f"Integration {integration.name!r} would serve both {seen[integration.id]} and {site.code}; each site needs its own.")
        seen[integration.id] = site.code
    return {site.id: await route_for_integration(db, chosen[site.id]) for site in sites}


async def asset_site_ids(db: AsyncSession, asset_ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """The site of every asset whose site can be determined (sensor, cooling unit, rack and equipment placements)."""
    from app.domain.cooling.models import CoolingUnit, EnvironmentalSensor

    found: dict[uuid.UUID, uuid.UUID] = {}
    if not asset_ids:
        return found
    ids = list(asset_ids)
    for model in (EnvironmentalSensor, CoolingUnit):
        for asset_id, site_id in (await db.execute(select(model.id, model.site_id).where(model.id.in_(ids)))).all():
            found[asset_id] = site_id
    room_site = (
        select(Room.id.label("room_id"), Building.site_id.label("site_id"))
        .join(Floor, Floor.id == Room.floor_id).join(Building, Building.id == Floor.building_id).subquery()
    )
    for asset_id, site_id in (
        await db.execute(
            select(EquipmentPlacement.equipment_id, room_site.c.site_id)
            .join(room_site, room_site.c.room_id == EquipmentPlacement.room_id)
            .where(EquipmentPlacement.equipment_id.in_(ids), EquipmentPlacement.effective_to.is_(None))
        )
    ).all():
        found.setdefault(asset_id, site_id)
    for asset_id, site_id in (
        await db.execute(
            select(RackPlacement.rack_id, room_site.c.site_id)
            .join(room_site, room_site.c.room_id == RackPlacement.room_id)
            .where(RackPlacement.rack_id.in_(ids), RackPlacement.effective_to.is_(None))
        )
    ).all():
        found.setdefault(asset_id, site_id)
    return found


@dataclass
class MappingOwnership:
    """Which mappings on the routed integrations the loader may simulate."""

    allowed: set[uuid.UUID] = field(default_factory=set)
    unresolved: dict[uuid.UUID, str] = field(default_factory=dict)  # mapping id -> source identifier (skipped, reported)


async def check_mapping_ownership(db: AsyncSession, targets: "Targets") -> MappingOwnership:
    """Fail closed if any mapping on a routed integration points at an asset that positively belongs to another site.

    A mapping with no asset, or whose asset is in a selected site, is allowed. An asset whose site cannot be determined
    (for example a power node) is allowed only when its integration belongs to the demo site by `site_id`; on a site-less
    integration it is skipped and reported instead of guessed at."""
    from app.domain.telemetry.models import IntegrationMetricMapping

    selected = {s.id for s in targets.sites}
    by_integration = {route.integration.id: (site_id, route) for site_id, route in targets.routes.items()}
    rows = (
        await db.execute(
            select(IntegrationMetricMapping.id, IntegrationMetricMapping.integration_id, IntegrationMetricMapping.managed_asset_id,
                   IntegrationMetricMapping.source_identifier)
            .where(IntegrationMetricMapping.integration_id.in_(list(by_integration)))
        )
    ).all()
    sites = await asset_site_ids(db, {r.managed_asset_id for r in rows if r.managed_asset_id is not None})
    result = MappingOwnership()
    foreign: list[str] = []
    for mapping_id, integration_id, asset_id, source in rows:
        site_id, route = by_integration[integration_id]
        if asset_id is None:
            result.allowed.add(mapping_id)
        elif asset_id in sites:
            if sites[asset_id] in selected:
                result.allowed.add(mapping_id)
            else:
                foreign.append(f"{source} (integration {route.integration.name})")
        elif route.integration.site_id == site_id:
            result.allowed.add(mapping_id)
        else:
            result.unresolved[mapping_id] = source
    if foreign:
        raise LoaderError(
            f"{len(foreign)} mapping(s) on the routed integrations point at assets in sites that are not selected, for example "
            f"{foreign[:3]}. Nothing was written. Fix the integration/site assignment or choose different integrations."
        )
    return result


async def discover(db: AsyncSession, *, prefix: str = DEMO_SITE_PREFIX, overrides: dict[str, str] | None = None,
                   need_routes: bool = True) -> Targets:
    validate_site_prefix(prefix)
    sites = await demo_sites(db, prefix)
    if not sites:
        raise LoaderError(f"No site with a code starting {prefix!r} exists. Nothing to do.")
    ids = [s.id for s in sites]
    targets = Targets(sites=sites, racks=await demo_racks(db, ids), devices=await demo_devices(db, ids))
    if need_routes:
        targets.routes = await resolve_routes(db, sites, overrides)
    return targets


async def outside_fingerprint(db: AsyncSession, prefix: str = DEMO_SITE_PREFIX) -> dict:
    """A digest of everything the loader could conceivably change outside the demo sites (the HRC site and any other
    non-demo site): its racks, equipment, sensors, placements, floor plans and calibrations. Taken before and after a
    run; equal digests are the evidence that the loader left those sites alone."""
    from app.domain.cooling.models import EnvironmentalSensor
    from app.domain.spatial.models import FloorPlan
    from app.domain.telemetry.models import IntegrationMetricMapping

    outside = select(Site.id).where(~Site.code.startswith(prefix))
    site_room = (
        select(Room.id).join(Floor, Floor.id == Room.floor_id).join(Building, Building.id == Floor.building_id)
        .where(Building.site_id.in_(outside))
    )
    parts: dict[str, int | list[str]] = {}
    parts["sites"] = (await db.execute(select(func.count()).select_from(Site).where(~Site.code.startswith(prefix)))).scalar_one()
    parts["rack_placements"] = sorted(
        str((r.rack_id, r.x_mm, r.y_mm, r.rotation_deg, r.effective_to))
        for r in (await db.execute(select(RackPlacement).where(RackPlacement.room_id.in_(site_room)))).scalars()
    )
    parts["equipment_placements"] = sorted(
        str((r.equipment_id, r.rack_id, r.u_range, r.x_mm, r.y_mm, r.effective_to))
        for r in (await db.execute(select(EquipmentPlacement).where(EquipmentPlacement.room_id.in_(site_room)))).scalars()
    )
    parts["sensors"] = sorted(
        str((r.id, r.name, r.version))
        for r in (await db.execute(select(EnvironmentalSensor).where(EnvironmentalSensor.site_id.in_(outside)))).scalars()
    )
    parts["plans"] = sorted(
        str((r.id, r.status, r.version, r.current_calibration_id))
        for r in (await db.execute(select(FloorPlan).where(FloorPlan.room_id.in_(site_room)))).scalars()
    )
    parts["integrations"] = sorted(
        str((r.id, r.name, r.version))
        for r in (await db.execute(select(Integration).where(Integration.site_id.in_(outside)))).scalars()
    )
    mapping_ids = select(Integration.id).where(Integration.site_id.in_(outside))
    mappings = select(IntegrationMetricMapping).where(IntegrationMetricMapping.integration_id.in_(mapping_ids))
    parts["mappings"] = sorted(
        str((r.id, r.source_identifier, r.current_revision_id)) for r in (await db.execute(mappings)).scalars()
    )
    from app.domain.alarm.models import Alarm
    from app.domain.telemetry.models import TelemetryReading

    readings = (
        await db.execute(
            select(func.count(), func.max(TelemetryReading.received_at), func.max(TelemetryReading.occurred_at))
            .where(TelemetryReading.integration_id.in_(mapping_ids))
        )
    ).one()
    parts["readings"] = [f"{readings[0]}|{readings[1]}|{readings[2]}"]
    parts["alarms"] = sorted(
        str((r.id, r.status, r.severity, r.telemetry_reading_id, r.last_value, r.acknowledged_at, r.cleared_at))
        for r in (await db.execute(select(Alarm).where(Alarm.integration_id.in_(mapping_ids)))).scalars()
    )
    digest = hashlib.sha256(repr(sorted(parts.items(), key=lambda kv: kv[0])).encode()).hexdigest()
    return {"digest": digest, "counts": {k: (v if isinstance(v, int) else len(v)) for k, v in parts.items()}}
