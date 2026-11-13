"""Builds a synthetic stand-in for the local demonstration database, directly through the ORM.

Shape (defaults match the real demo): three `DEMO-SITE-0n` sites, each with one building / floor / room whose active
floor plan is 13,400 x 12,800 mm and uncalibrated; racks with positions; five rack-mounted devices per rack; one
integration + central collector + assignment per site; three metric mappings per integration with one reading each; and
a fourth site, HRC, with its own plan, racks, device, integration and mapping that the loader must never change."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.placement_service import move_equipment
from app.application.telemetry_service import ingest_reading
from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, CollectorAssignment, Integration
from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import RackPlacement
from app.domain.spatial.models import FloorPlan
from app.domain.telemetry.models import IntegrationMetricMapping

PLAN_WIDTH_MM = 13_400
PLAN_HEIGHT_MM = 12_800


@dataclass
class SiteWorld:
    code: str
    site_id: uuid.UUID
    room_id: uuid.UUID
    plan_id: uuid.UUID
    integration_id: uuid.UUID
    collector_id: uuid.UUID
    rack_ids: list[uuid.UUID] = field(default_factory=list)
    device_ids: list[uuid.UUID] = field(default_factory=list)
    mapping_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class World:
    sites: list[SiteWorld]
    hrc: SiteWorld
    rack_revision_id: uuid.UUID
    equipment_revision_id: uuid.UUID

    @property
    def racks(self) -> int:
        return sum(len(s.rack_ids) for s in self.sites)

    @property
    def devices(self) -> int:
        return sum(len(s.device_ids) for s in self.sites)


async def _site(db: AsyncSession, city: City, code: str, *, poll: int, racks: int, per_rack: int, rev: tuple[uuid.UUID, uuid.UUID],
                now: datetime, with_mappings: bool, x0: int = 1000) -> SiteWorld:
    site = Site(city_id=city.id, code=code, name=f"{code} name")
    db.add(site)
    await db.flush()
    building = Building(site_id=site.id, name=f"{code} Hall", code="H1")
    db.add(building)
    await db.flush()
    floor = Floor(building_id=building.id, name="Floor 1", level_number=1)
    db.add(floor)
    await db.flush()
    room = Room(floor_id=floor.id, name=f"{code} Room 1", code="R1")
    db.add(room)
    await db.flush()
    plan = FloorPlan(room_id=room.id, revision_number=1, status="active", room_width_mm=PLAN_WIDTH_MM, room_height_mm=PLAN_HEIGHT_MM)
    collector = Collector(id=uuid.uuid4(), name=f"collector-{code}", collector_type="central", status="active", secret_ciphertext="x",
                          secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=f"integration-{code}", integration_type="snmp", target_host="192.0.2.10", config={},
                              poll_interval_seconds=poll, site_id=site.id)
    db.add_all([plan, collector, integration])
    await db.flush()
    db.add(CollectorAssignment(collector_id=collector.id, integration_id=integration.id, effective_from=now - timedelta(days=1)))
    world = SiteWorld(code=code, site_id=site.id, room_id=room.id, plan_id=plan.id, integration_id=integration.id, collector_id=collector.id)
    for index in range(racks):
        tag = f"{code}-RACK-{index + 1:03d}"
        asset = ManagedAsset(asset_type="rack", asset_tag=tag, lifecycle_status="active")
        db.add(asset)
        await db.flush()
        db.add(Rack(id=asset.id, model_revision_id=rev[0], name=f"{code[-2:]}-R{index + 1:03d}"))
        await db.flush()
        db.add(RackPlacement(rack_id=asset.id, room_id=room.id, x_mm=x0 + (index % 10) * 800, y_mm=1000 + (index // 10) * 2400, rotation_deg=0,
                             effective_from=now - timedelta(days=1)))
        world.rack_ids.append(asset.id)
        for slot in range(per_rack):
            device = ManagedAsset(asset_type="equipment", asset_tag=f"{code}-EQ-{index + 1:03d}-{slot + 1}", lifecycle_status="active")
            db.add(device)
            await db.flush()
            db.add(Equipment(id=device.id, model_revision_id=rev[1], hostname=f"host-{code}-{index + 1:03d}-{slot + 1}"))
            await db.flush()
            await move_equipment(db, equipment_id=device.id, placement_type="rack_mounted", room_id=room.id, rack_id=asset.id,
                                 u_start=1 + slot * 4, u_end=3 + slot * 4, side="front")
            world.device_ids.append(device.id)
    await db.flush()
    if with_mappings:
        target = world.device_ids[0] if world.device_ids else None
        # Three existing mappings per integration: power in W (scale 1), temperature in degF, load in percent.
        for metric, unit, value, source in (("power_kw", "W", 350.0, "pdu-1.power"), ("temperature_c", "degF", 72.5, "probe-1.temp"),
                                            ("load_percent", "%", 41.0, "pdu-1.load")):
            mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, managed_asset_id=target, source_identifier=source,
                                               canonical_metric=metric, unit=unit, scale=1)
            db.add(mapping)
            await db.flush()
            await ingest_reading(db, collector_id=collector.id, integration_id=integration.id, dedup_key=f"seed-{mapping.id}",
                                 external_identifier=f"ext-{source}", source_identifier=source, occurred_at=now - timedelta(minutes=10),
                                 value=value, mapping_revision_id=mapping.current_revision_id)
            world.mapping_ids.append(mapping.id)
    await db.commit()
    return world


async def build_world(db: AsyncSession, *, sites: int = 3, racks_per_site: int = 4, devices_per_rack: int = 5, poll: int = 300) -> World:
    now = datetime.now(UTC)
    org = Organization(name=f"Org-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    country = Country(organization_id=org.id, name="Testland", iso_code="TL")
    db.add(country)
    await db.flush()
    city = City(country_id=country.id, name="Testville")
    db.add(city)
    rack_model = RackModel(manufacturer="Acme", model_name=f"R-{uuid.uuid4().hex[:6]}")
    equipment_model = EquipmentModel(manufacturer="Acme", model_name=f"E-{uuid.uuid4().hex[:6]}")
    db.add_all([rack_model, equipment_model])
    await db.flush()
    rack_rev = RackModelRevision(rack_model_id=rack_model.id, height_u=42, width_mm=600, depth_mm=1200)
    equipment_rev = EquipmentModelRevision(equipment_model_id=equipment_model.id, height_u=2, width_mm=480, depth_mm=700)
    db.add_all([rack_rev, equipment_rev])
    await db.commit()
    rev = (rack_rev.id, equipment_rev.id)
    demo = [
        await _site(db, city, f"DEMO-SITE-{n + 1:02d}", poll=poll, racks=racks_per_site, per_rack=devices_per_rack, rev=rev, now=now,
                    with_mappings=True)
        for n in range(sites)
    ]
    hrc = await _site(db, city, "HRC", poll=60, racks=2, per_rack=1, rev=rev, now=now, with_mappings=True)
    return World(sites=demo, hrc=hrc, rack_revision_id=rack_rev.id, equipment_revision_id=equipment_rev.id)
