"""Direct DB-level verification of Phase 2's placement invariants (ARCHITECTURE_REVIEW.md
§7/§7a/§7b/§7c/§8; migration 0004) — bypasses the API/application layer entirely and
inserts through the ORM so each constraint is proven to be enforced by PostgreSQL itself,
not merely by application-level validation that could drift out of sync with the schema.
Mirrors tests/integration/test_db_constraints.py's style for the Phase 1 tables."""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy.exc
from sqlalchemy.dialects.postgresql import Range

from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.spatial.models import FloorPlan, SpatialLayer, SpatialObject


async def _make_room(db_session, *, code: str | None = None) -> Room:
    org = Organization(name=f"Org-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    await db_session.flush()
    country = Country(organization_id=org.id, name="Testland")
    db_session.add(country)
    await db_session.flush()
    city = City(country_id=country.id, name="Testville")
    db_session.add(city)
    await db_session.flush()
    site = Site(city_id=city.id, code=f"S-{uuid.uuid4().hex[:6]}", name="Site")
    db_session.add(site)
    await db_session.flush()
    building = Building(site_id=site.id, code="A", name="Building A")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, name="Floor 1", level_number=1)
    db_session.add(floor)
    await db_session.flush()
    room = Room(floor_id=floor.id, code=code or f"R-{uuid.uuid4().hex[:6]}", name="Room")
    db_session.add(room)
    await db_session.flush()
    return room


async def _make_rack(db_session, *, height_u: int = 42) -> Rack:
    asset = ManagedAsset(asset_type="rack", asset_tag=f"RACK-{uuid.uuid4().hex[:8]}")
    db_session.add(asset)
    await db_session.flush()
    from app.domain.catalog.models import RackModel

    model = RackModel(manufacturer="Acme", model_name=f"RM-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()
    revision = RackModelRevision(rack_model_id=model.id, height_u=height_u, width_mm=600, depth_mm=1000)
    db_session.add(revision)
    await db_session.flush()
    rack = Rack(id=asset.id, model_revision_id=revision.id, name="Rack")
    db_session.add(rack)
    await db_session.flush()
    return rack


async def _make_equipment(db_session) -> Equipment:
    asset = ManagedAsset(asset_type="equipment", asset_tag=f"EQ-{uuid.uuid4().hex[:8]}")
    db_session.add(asset)
    await db_session.flush()
    from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision

    model = EquipmentModel(manufacturer="Acme", model_name=f"EM-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()
    revision = EquipmentModelRevision(equipment_model_id=model.id)
    db_session.add(revision)
    await db_session.flush()
    equipment = Equipment(id=asset.id, model_revision_id=revision.id)
    db_session.add(equipment)
    await db_session.flush()
    return equipment


# ------------------------------------------------------------- CHECK constraints


async def test_side_must_be_null_or_an_allowed_value(db_session):
    room = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="floor_standing", room_id=room.id, side="diagonal",
            effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_placement_type_must_be_an_allowed_value(db_session):
    room = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="levitating", room_id=room.id, effective_from=datetime.now(UTC)
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_rack_mounted_with_null_side_is_rejected_the_v1_3_f1_correction(db_session):
    """The exact regression this migration must never reintroduce: a NULL side on a
    rack_mounted row would otherwise bypass both partial exclusion constraints (their
    WHERE clauses are keyed off occupies_front/occupies_rear, which the CHECK constraint
    below and the COALESCE-based generated columns together prevent from ever being
    silently false-but-unconstrained for a rack_mounted row)."""
    room = await _make_room(db_session)
    rack = await _make_rack(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(1, 3, bounds="[)"), side=None, effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_rack_mounted_with_null_rack_id_is_rejected(db_session):
    room = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="rack_mounted", room_id=room.id, rack_id=None,
            u_range=Range(1, 3, bounds="[)"), side="front", effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_rack_mounted_with_null_u_range_is_rejected(db_session):
    room = await _make_room(db_session)
    rack = await _make_rack(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=None, side="front", effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_floor_standing_with_null_side_and_null_rack_succeeds(db_session):
    """The corollary of F1: a non-rack_mounted placement legitimately has side/rack_id/
    u_range all NULL, and this must NOT be rejected — the CHECK constraint is scoped to
    placement_type='rack_mounted' specifically, never a blanket NOT NULL."""
    room = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    placement = EquipmentPlacement(
        equipment_id=equipment.id, placement_type="floor_standing", room_id=room.id, effective_from=datetime.now(UTC)
    )
    db_session.add(placement)
    await db_session.commit()
    assert placement.occupies_front is False
    assert placement.occupies_rear is False


# ------------------------------------------------------------- Exclusion constraints


async def test_same_side_same_u_range_overlap_is_rejected_by_the_db(db_session):
    room = await _make_room(db_session)
    rack = await _make_rack(db_session)
    eq_1 = await _make_equipment(db_session)
    eq_2 = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_1.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(1, 5, bounds="[)"), side="front", effective_from=datetime.now(UTC),
        )
    )
    await db_session.commit()

    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_2.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(3, 7, bounds="[)"), side="front", effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_front_and_rear_at_the_identical_u_range_both_succeed(db_session):
    room = await _make_room(db_session)
    rack = await _make_rack(db_session)
    eq_front = await _make_equipment(db_session)
    eq_rear = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_front.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(1, 5, bounds="[)"), side="front", effective_from=datetime.now(UTC),
        )
    )
    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_rear.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(1, 5, bounds="[)"), side="rear", effective_from=datetime.now(UTC),
        )
    )
    await db_session.commit()  # must not raise


async def test_side_both_overlaps_an_existing_front_placement(db_session):
    """side='both' occupies front AND rear — it must conflict with an existing 'front'
    placement in the same U-range even though the literal side string differs."""
    room = await _make_room(db_session)
    rack = await _make_rack(db_session)
    eq_front = await _make_equipment(db_session)
    eq_both = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_front.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(1, 5, bounds="[)"), side="front", effective_from=datetime.now(UTC),
        )
    )
    await db_session.commit()

    db_session.add(
        EquipmentPlacement(
            equipment_id=eq_both.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
            u_range=Range(2, 4, bounds="[)"), side="both", effective_from=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_equipment_cannot_have_two_simultaneous_current_placements(db_session):
    """§7b's one-timeline-per-asset guarantee: two effective_to IS NULL rows for the same
    equipment_id must never coexist, even in different rooms with no rack involvement."""
    room_a = await _make_room(db_session)
    room_b = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="floor_standing", room_id=room_a.id, effective_from=datetime.now(UTC)
        )
    )
    await db_session.commit()

    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="floor_standing", room_id=room_b.id, effective_from=datetime.now(UTC)
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_rack_cannot_have_two_simultaneous_current_placements(db_session):
    room_a = await _make_room(db_session)
    room_b = await _make_room(db_session)
    rack = await _make_rack(db_session)
    db_session.add(RackPlacement(rack_id=rack.id, room_id=room_a.id, effective_from=datetime.now(UTC)))
    await db_session.commit()

    db_session.add(RackPlacement(rack_id=rack.id, room_id=room_b.id, effective_from=datetime.now(UTC)))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_a_historical_closed_placement_does_not_block_a_new_current_one(db_session):
    """The exclusion constraint is scoped to the open-ended timeline overlap, not a
    blanket one-row-ever-per-asset rule — retiring then re-placing must work."""
    room = await _make_room(db_session)
    equipment = await _make_equipment(db_session)
    first = EquipmentPlacement(
        equipment_id=equipment.id, placement_type="floor_standing", room_id=room.id, effective_from=datetime.now(UTC)
    )
    db_session.add(first)
    await db_session.commit()

    first.effective_to = datetime.now(UTC)
    await db_session.commit()

    db_session.add(
        EquipmentPlacement(
            equipment_id=equipment.id, placement_type="floor_standing", room_id=room.id, effective_from=datetime.now(UTC)
        )
    )
    await db_session.commit()  # must not raise


# ------------------------------------------------------------- Room-consistency trigger


async def test_placement_room_must_match_its_spatial_objects_floor_plans_room(db_session):
    """§8's cross-table invariant, enforced via the trigger added in migration 0004: a
    RackPlacement/EquipmentPlacement pointing at a SpatialObject drawn on a FloorPlan for
    a *different* room must be rejected — the two records would otherwise disagree about
    which room the asset is actually in."""
    room_a = await _make_room(db_session)
    room_b = await _make_room(db_session)
    floor_plan = FloorPlan(room_id=room_a.id, revision_number=1, status="draft")
    db_session.add(floor_plan)
    await db_session.flush()
    layer = SpatialLayer(floor_plan_id=floor_plan.id, name="Racks", layer_type="racks")
    db_session.add(layer)
    await db_session.flush()
    spatial_object = SpatialObject(spatial_layer_id=layer.id, object_type="rack", geometry_type="rect", x_mm=0, y_mm=0)
    db_session.add(spatial_object)
    await db_session.flush()

    rack = await _make_rack(db_session)
    db_session.add(
        RackPlacement(
            rack_id=rack.id, room_id=room_b.id, spatial_object_id=spatial_object.id, effective_from=datetime.now(UTC)
        )
    )
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.commit()
    await db_session.rollback()


async def test_placement_room_matching_its_spatial_objects_room_succeeds(db_session):
    room = await _make_room(db_session)
    floor_plan = FloorPlan(room_id=room.id, revision_number=1, status="draft")
    db_session.add(floor_plan)
    await db_session.flush()
    layer = SpatialLayer(floor_plan_id=floor_plan.id, name="Racks", layer_type="racks")
    db_session.add(layer)
    await db_session.flush()
    spatial_object = SpatialObject(spatial_layer_id=layer.id, object_type="rack", geometry_type="rect", x_mm=0, y_mm=0)
    db_session.add(spatial_object)
    await db_session.flush()

    rack = await _make_rack(db_session)
    db_session.add(
        RackPlacement(rack_id=rack.id, room_id=room.id, spatial_object_id=spatial_object.id, effective_from=datetime.now(UTC))
    )
    await db_session.commit()  # must not raise


# ------------------------------------------------------------- FloorPlan partial unique index


async def test_only_one_active_floor_plan_per_room_is_db_enforced(db_session):
    room = await _make_room(db_session)
    first = FloorPlan(room_id=room.id, revision_number=1, status="active")
    db_session.add(first)
    await db_session.commit()

    second = FloorPlan(room_id=room.id, revision_number=2, status="active")
    db_session.add(second)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_two_draft_floor_plans_for_the_same_room_are_allowed(db_session):
    room = await _make_room(db_session)
    db_session.add(FloorPlan(room_id=room.id, revision_number=1, status="draft"))
    await db_session.commit()
    db_session.add(FloorPlan(room_id=room.id, revision_number=2, status="draft"))
    await db_session.commit()  # must not raise — the partial index only constrains status='active'


async def test_floor_plan_room_and_revision_number_must_be_unique_together(db_session):
    room = await _make_room(db_session)
    db_session.add(FloorPlan(room_id=room.id, revision_number=1, status="draft"))
    await db_session.commit()
    db_session.add(FloorPlan(room_id=room.id, revision_number=1, status="draft"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()
