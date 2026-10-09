"""Issue #105 (review blocker B4): unknown electrical demand is never a known 0 kW thermal load.

The power roll-up returns `load_kw = 0` with `quality = "missing"` for equipment whose demand is unknown, because
missing demand adds nothing to the sum. Reading that as "the room draws 0 kW" would certify full headroom and N+1 for
a room whose IT load nobody knows. These tests drive the REAL path: equipment, power feeds, a power source and
`power_kw` telemetry feed `rollup_for_site`, whose result feeds the HTTP capacity endpoint. Nothing is monkeypatched."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.domain.power.models import PowerConnection, PowerNode
from tests.api._phase2_helpers import create_equipment
from tests.api._thermal_helpers import C, TelemetrySeeder, activate, make_unit, make_world, make_zone, relate, seed_plan


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    other = await auth_headers("Administrator")
    zone = await make_zone(client, admin, site["room"], name="Whole hall")
    for i in range(2):
        unit = await make_unit(client, admin, site["site"], rated_cooling_capacity_kw=100, name=f"CRAH-{i}")
        await activate(client, admin, unit["id"])
        await relate(client, admin, unit["id"], zone["id"])
    intake = PowerNode(node_type="utility_intake", label="Utility")
    db_session.add(intake)
    await db_session.commit()
    return {
        "admin": admin, "other": other, "site": site, "room": site["room"], "db": db_session, "client": client,
        "auth_headers": auth_headers, "intake": intake.id, "seed": TelemetrySeeder(db_session), "zone": zone,
    }


async def equipment(world, *, room: str | None = None, powered: bool = True, demand_kw: float | None = None) -> dict:
    """A piece of equipment placed in a room, optionally fed by a live power path and optionally reporting demand."""
    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    item = await create_equipment(client, engineer, world["auth_headers"])
    moved = await client.post(f"/api/v1/equipment/{item['id']}/move", json={"placement_type": "floor_standing", "room_id": room or world["room"]}, headers=engineer)
    assert moved.status_code == 200, moved.text
    if powered:
        feed = await client.post("/api/v1/power/equipment-feeds", json={"equipment_asset_id": item["id"], "label": "A"}, headers=admin)
        assert feed.status_code == 201, feed.text
        db = world["db"]
        db.add(PowerConnection(
            source_node_id=world["intake"], target_node_id=feed.json()["id"], connection_type="feed", feed_label="single",
            status="active", version=1, effective_from=datetime.now(UTC),
        ))
        await db.commit()
        item["feed_id"] = feed.json()["id"]
    if demand_kw is not None:
        await world["seed"].reading(item["id"], "power_kw", demand_kw)
    return item


async def room_capacity(world) -> dict:
    response = await world["client"].get(f"{C}/rooms/{world['room']}/capacity", headers=world["admin"])
    assert response.status_code == 200, response.text
    zone = response.json()["zones"][0]
    return zone


def assert_load_is_unknown(z: dict) -> None:
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["load_complete"] is False
    assert load["electrical_kw"] is None and load["thermal_kw"] is None  # an incomplete load is never a number
    assert z["headroom_kw"] is None and z["utilization_pct"] is None  # no authoritative headroom
    assert z["level"] == "unknown"
    pools = z["redundancy"]["pools"]
    assert z["redundancy"]["state"] == "redundant_unverified"  # two healthy units, but N+1 cannot be verified
    assert all(p["n_plus_1_verified"] is None for p in pools)


async def test_all_demand_missing_is_unknown_load_not_zero(world):
    await equipment(world)
    await equipment(world)
    z = await room_capacity(world)
    assert_load_is_unknown(z)
    load = z["thermal_load"]
    assert load["missing_load_rack_count"] == 2
    assert load["electrical_kw_lower_bound"] == 0.0  # nothing is known, and it is labelled as a bound
    assert "Not a load" in load["lower_bound_note"] and load["quality"] == "missing"
    # the underlying roll-up really did report 0 / missing: this is the value the old code mistook for a known zero
    from app.application.power_rollup_loader import rollup_for_site

    roll = await rollup_for_site(world["db"], uuid.UUID(world["site"]["site"]), datetime.now(UTC))
    room = next(iter(roll.rooms.values()))
    assert room.load_kw == 0 and room.quality == "missing" and room.missing_demand_count == 2


async def test_one_known_one_missing_exposes_only_a_labelled_lower_bound(world):
    await equipment(world, demand_kw=30.0)
    await equipment(world)
    z = await room_capacity(world)
    assert_load_is_unknown(z)
    load = z["thermal_load"]
    assert load["electrical_kw_lower_bound"] == 30.0 and load["thermal_kw_lower_bound"] == 30.0
    assert load["missing_load_rack_count"] == 1 and load["quality"] == "mixed"
    assert "Known demand only" in load["lower_bound_note"]


async def test_all_demand_known_gives_a_complete_load_headroom_and_verified_n_plus_1(world):
    await equipment(world, demand_kw=30.0)
    await equipment(world, demand_kw=20.0)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "known" and load["load_complete"] is True
    assert load["electrical_kw"] == 50.0 and load["thermal_kw"] == 50.0 and load["electrical_kw_lower_bound"] is None
    assert z["headroom_kw"] == 150.0 and z["utilization_pct"] == 25.0 and z["level"] == "ok"
    assert z["redundancy"]["state"] == "redundant" and z["redundancy"]["pools"][0]["n_plus_1_verified"] is True


async def test_a_known_zero_demand_is_a_genuine_zero_load(world):
    await equipment(world, demand_kw=0.0)
    await equipment(world, demand_kw=0.0)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "known" and load["electrical_kw"] == 0.0 and load["thermal_kw"] == 0.0
    assert z["headroom_kw"] == 200.0 and z["level"] == "ok" and z["redundancy"]["state"] == "redundant"


async def test_equipment_without_a_power_feed_is_unknown_demand_not_zero_and_other_rooms_do_not_count(world, client):
    """Superseded expectation: an unfed item used to be silently ignored, so 40 kW of 2 placed items read as a complete load."""
    other_room = await _second_room(world)
    await equipment(world, demand_kw=40.0)
    await equipment(world, room=other_room)  # another room: not this room's population at all
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["electrical_kw"] == 40.0
    await equipment(world, powered=False)  # placed here, no power-input model: its demand is unknown
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "incomplete" and z["thermal_load"]["thermal_kw"] is None
    assert z["thermal_load"]["thermal_kw_lower_bound"] == 40.0 and z["thermal_load"]["unmodelled_equipment_count"] == 1
    assert z["headroom_kw"] is None and z["redundancy"]["state"] == "redundant_unverified"


async def test_a_retired_power_feed_makes_its_equipment_unmodelled_and_decommissioning_removes_it_from_the_population(world):
    await equipment(world, demand_kw=40.0)
    gone = await equipment(world, demand_kw=10.0)
    assert (await room_capacity(world))["thermal_load"]["state"] == "known"
    await world["db"].execute(text("UPDATE power_node SET retired_at = now() WHERE id = :i"), {"i": gone["feed_id"]})
    await world["db"].commit()
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "incomplete" and z["thermal_load"]["unmodelled_equipment_count"] == 1  # still placed, now unmodelled
    for status in ("installed", "active", "decommissioned"):
        r = await world["client"].post(f"/api/v1/managed-assets/{gone['id']}/lifecycle-transition", json={"to_status": status}, headers=world["admin"])
        assert r.status_code == 200, r.text
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["electrical_kw"] == 40.0  # a decommissioned item is not heat load


async def test_incomplete_demand_never_certifies_headroom_or_n_plus_1_even_when_capacity_is_huge(world):
    for unit in (await world["client"].get(f"{C}/units", headers=world["admin"])).json()["items"]:
        r = await world["client"].patch(f"{C}/units/{unit['id']}", json={"rated_cooling_capacity_kw": 5000}, headers={**world["admin"], "If-Match": str(unit["version"])})
        assert r.status_code == 200, r.text
    await equipment(world, demand_kw=1.0)
    await equipment(world)
    z = await room_capacity(world)
    assert_load_is_unknown(z)
    assert z["available_kw"] == 10000.0  # capacity is known; only the load is not
    body = (await world["client"].get(f"{C}/rooms/{world['room']}/exceptions", headers=world["admin"])).json()
    assert any(i["type"] == "capacity_inputs_incomplete" for i in body["items"])


async def test_a_rack_with_one_unknown_demand_makes_a_rectangular_zone_load_incomplete(world):
    """The rack scope has the same defect: quality is 'mixed', not 'missing', when only one item lacks demand."""
    from tests.api._phase2_helpers import create_rack

    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    rack = await create_rack(client, engineer, world["auth_headers"], room_id=world["room"], x_mm=1000, y_mm=1000, rotation_deg=0)
    for u, demand in ((1, 25.0), (5, None)):
        item = await equipment(world, demand_kw=demand)
        moved = await client.post(
            f"/api/v1/equipment/{item['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": world["room"], "rack_id": rack["id"], "u_start": u, "u_end": u + 2, "side": "front"},
            headers=engineer,
        )
        assert moved.status_code == 200, moved.text
    aisle = await make_zone(client, admin, world["room"], name="Row", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=3000)
    for unit in (await client.get(f"{C}/units", headers=admin)).json()["items"]:
        await relate(client, admin, unit["id"], aisle["id"])
    zones = (await client.get(f"{C}/rooms/{world['room']}/capacity", headers=admin)).json()["zones"]
    row = next(z for z in zones if z["name"] == "Row")
    load = row["thermal_load"]
    assert load["state"] == "incomplete" and load["thermal_kw"] is None and load["thermal_kw_lower_bound"] == 25.0
    assert row["headroom_kw"] is None and row["redundancy"]["state"] == "redundant_unverified"


async def _second_room(world) -> str:
    from tests.api._phase2_helpers import create_room

    return await create_room(world["client"], world["auth_headers"])
