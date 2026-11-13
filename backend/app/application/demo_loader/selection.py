"""Finds the demo data the loader may touch, and nothing else.

Scope rule: a site is in scope only when its code starts with `DEMO_SITE_PREFIX`. Every query below filters on that
set of site ids, so a fourth site (HRC) is never selected, written, or counted as a target."""

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.collector_service import current_assignment
from app.application.demo_loader.constants import DEMO_SITE_PREFIX
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.location.models import Building, Floor, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement


class LoaderError(Exception):
    """A condition the operator must fix; the message says what and how."""


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


@dataclass
class IntegrationRoute:
    """Where a site's synthetic series are ingested: the integration and the collector currently assigned to it."""

    integration: Integration
    collector: Collector


@dataclass
class Targets:
    sites: list[SiteInfo] = field(default_factory=list)
    racks: list[RackInfo] = field(default_factory=list)
    devices: list[DeviceInfo] = field(default_factory=list)
    routes: dict[uuid.UUID, IntegrationRoute] = field(default_factory=dict)  # site id -> route


async def demo_sites(db: AsyncSession, prefix: str = DEMO_SITE_PREFIX) -> list[SiteInfo]:
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
    return IntegrationRoute(integration=integration, collector=collector)


async def resolve_routes(
    db: AsyncSession, sites: list[SiteInfo], overrides: dict[str, str] | None = None,
) -> dict[uuid.UUID, IntegrationRoute]:
    """One integration per demo site: by `integration.site_id`, else by the site code appearing in the integration
    name, else the explicit `--integration SITE=NAME` override. Ambiguity is an error, never a guess."""
    overrides = overrides or {}
    enabled = select(Integration).where(Integration.enabled.is_(True)).order_by(Integration.name)
    integrations = list((await db.execute(enabled)).scalars())
    routes: dict[uuid.UUID, IntegrationRoute] = {}
    for site in sites:
        if site.code in overrides:
            matches = [i for i in integrations if i.name == overrides[site.code]]
        else:
            by_site = [i for i in integrations if i.site_id == site.id]
            matches = by_site or [i for i in integrations if site.code.lower() in i.name.lower()]
        if len(matches) != 1:
            raise LoaderError(
                f"Cannot choose one enabled integration for {site.code}: {len(matches)} candidates "
                f"({', '.join(i.name for i in matches) or 'none'}). Pass --integration {site.code}=<integration name>."
            )
        routes[site.id] = await route_for_integration(db, matches[0])
    return routes


async def discover(db: AsyncSession, *, prefix: str = DEMO_SITE_PREFIX, overrides: dict[str, str] | None = None,
                   need_routes: bool = True) -> Targets:
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
    digest = hashlib.sha256(repr(sorted(parts.items(), key=lambda kv: kv[0])).encode()).hexdigest()
    return {"digest": digest, "counts": {k: (v if isinstance(v, int) else len(v)) for k, v in parts.items()}}
