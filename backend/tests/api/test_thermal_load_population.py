"""Issue #105 follow-up (P2): room thermal-load completeness is judged against the equipment PLACED in the room, not against
the equipment that already has a power model.

Before: equipment without a power-input node was invisible to the roll-up, so a room with 10 placed servers of which 2 were
modelled reported `state: known`, a numeric headroom and verified N+1. These tests drive the real path (equipment, placement,
power feeds, telemetry, roll-up, HTTP) and assert that unknown demand is counted, never zero."""

from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import C, make_zone, relate
from tests.api.test_thermal_load_completeness import (  # noqa: F401  (fixture re-export)
    _second_room,
    equipment,
    room_capacity,
    world,
)

NEW_KEYS = {
    "placed_equipment_count", "modelled_equipment_count", "unknown_demand_equipment_count", "unmodelled_equipment_count",
    "missing_demand_equipment_count", "unattributed_floor_equipment_count", "incomplete_reasons",
}


async def test_1_fully_modelled_room_is_known_with_verified_headroom_and_n_plus_1(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world, demand_kw=20.0)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "known" and load["load_complete"] is True and load["thermal_kw"] == 50.0
    assert (load["placed_equipment_count"], load["modelled_equipment_count"], load["unknown_demand_equipment_count"]) == (2, 2, 0)
    assert z["headroom_kw"] == 150.0 and z["headroom_verified"] is True and z["headroom_basis"].startswith("verified")
    assert z["redundancy"]["state"] == "redundant" and z["redundancy"]["pools"][0]["verification_unavailable_reason"] is None


async def test_2_ten_servers_two_modelled_is_incomplete_with_the_known_subtotal_kept(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world, demand_kw=20.0)
    for _ in range(8):
        await equipment(world, powered=False)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["load_complete"] is False
    assert load["thermal_kw"] is None and load["electrical_kw"] is None  # the subtotal is never presented as the room load
    assert load["thermal_kw_lower_bound"] == 50.0 and "Not a load" in load["lower_bound_note"]
    assert (load["placed_equipment_count"], load["modelled_equipment_count"]) == (10, 2)
    assert (load["unmodelled_equipment_count"], load["missing_demand_equipment_count"], load["unknown_demand_equipment_count"]) == (8, 0, 8)
    assert load["incomplete_reasons"] == ["equipment_without_power_model"] and "8 of 10" in load["basis"]
    assert z["headroom_kw"] is None and z["utilization_pct"] is None and z["headroom_verified"] is False
    assert z["headroom_upper_bound_kw"] == 150.0 and z["headroom_basis"].startswith("unverified")  # at most: the real load is >= 50 kW
    assert z["level"] == "unknown"
    assert z["redundancy"]["state"] == "redundant_unverified"
    pool = z["redundancy"]["pools"][0]
    assert pool["n_plus_1_verified"] is None and pool["verification_unavailable_reason"] == "incomplete_load"


async def test_3_one_placed_server_without_a_power_model_is_unknown_not_zero(world):  # noqa: F811
    await equipment(world, powered=False)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["thermal_kw"] is None and load["electrical_kw"] is None
    assert load["unmodelled_equipment_count"] == 1 and load["thermal_kw_lower_bound"] == 0.0
    assert z["headroom_kw"] is None and z["level"] == "unknown" and z["redundancy"]["state"] == "redundant_unverified"


async def test_4_equipment_in_another_room_never_changes_this_rooms_completeness(world):  # noqa: F811
    other = await _second_room(world)
    await equipment(world, demand_kw=40.0)
    for _ in range(3):
        await equipment(world, room=other, powered=False)
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["placed_equipment_count"] == 1


async def test_5_rack_contained_equipment_is_attributed_to_the_racks_room_once(world):  # noqa: F811
    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    other = await _second_room(world)
    here = await create_rack(client, engineer, world["auth_headers"], room_id=world["room"], x_mm=1000, y_mm=1000, rotation_deg=0)
    there = await create_rack(client, engineer, world["auth_headers"], room_id=other, x_mm=1000, y_mm=1000, rotation_deg=0)

    async def mount(rack, u, demand, powered=True, room=None):
        item = await equipment(world, demand_kw=demand, powered=powered, room=room)
        moved = await client.post(
            f"/api/v1/equipment/{item['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": room or world["room"], "rack_id": rack["id"], "u_start": u, "u_end": u + 2, "side": "front"},
            headers=engineer,
        )
        assert moved.status_code == 200, moved.text
        return item

    await mount(here, 1, 25.0)
    await mount(there, 1, None, powered=False, room=other)  # a rack in another room: not part of this room
    assert (await room_capacity(world))["thermal_load"]["state"] == "known"
    await mount(here, 5, None, powered=False)  # unmodelled item inside a rack of this room
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["placed_equipment_count"] == 2 and load["unmodelled_equipment_count"] == 1
    assert load["thermal_kw_lower_bound"] == 25.0
    assert admin  # the admin headers drove the setup helpers


async def test_6_an_empty_room_is_unknown_not_a_verified_zero(world):  # noqa: F811
    """No per-room attestation that the inventory is complete exists in the domain model, so 'nothing placed' cannot be told
    apart from 'nobody has inventoried this room'. It stays unknown; no headroom or N+1 is certified from it."""
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "unknown" and load["thermal_kw"] is None and load["incomplete_reasons"] == ["no_equipment_placed"]
    assert load["placed_equipment_count"] == 0
    assert z["headroom_kw"] is None and z["headroom_verified"] is False and z["redundancy"]["state"] == "redundant_unverified"


async def test_7_redundant_power_inputs_are_not_double_counted(world):  # noqa: F811
    item = await equipment(world, demand_kw=40.0)
    # second (B) feed for the same equipment, fed from the same intake
    client, admin = world["client"], world["admin"]
    feed = await client.post("/api/v1/power/equipment-feeds", json={"equipment_asset_id": item["id"], "label": "B"}, headers=admin)
    assert feed.status_code == 201, feed.text
    from datetime import UTC, datetime

    from app.domain.power.models import PowerConnection

    world["db"].add(PowerConnection(
        source_node_id=world["intake"], target_node_id=feed.json()["id"], connection_type="feed", feed_label="B",
        status="active", version=1, effective_from=datetime.now(UTC),
    ))
    await world["db"].commit()
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "known" and load["thermal_kw"] == 40.0  # one machine, two inputs, one demand
    assert (load["placed_equipment_count"], load["modelled_equipment_count"]) == (1, 1)


async def test_8_missing_demand_is_unknown_and_stale_demand_keeps_its_existing_treatment(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world)  # modelled (has a feed) but never reported demand
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["missing_demand_equipment_count"] == 1 and load["unmodelled_equipment_count"] == 0
    assert load["incomplete_reasons"] == ["equipment_without_demand"] and load["thermal_kw_lower_bound"] == 30.0


async def test_8b_stale_demand_is_last_known_demand_with_its_quality_label(world):  # noqa: F811
    """Unchanged #102 behaviour: a stale reading is the last known demand, so the load is known but says it is stale."""
    stale = await equipment(world)
    await world["seed"].reading(stale["id"], "power_kw", 10.0, age_seconds=7200)
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["thermal_kw"] == 10.0 and z["thermal_load"]["quality"] == "stale"


async def test_9_incomplete_demand_blocks_n_plus_1_even_when_known_demand_is_far_below_capacity(world):  # noqa: F811
    await equipment(world, demand_kw=1.0)  # 1 kW known against 2 x 100 kW
    await equipment(world, powered=False)
    z = await room_capacity(world)
    assert z["thermal_load"]["thermal_kw_lower_bound"] == 1.0
    pool = z["redundancy"]["pools"][0]
    assert z["redundancy"]["state"] == "redundant_unverified" and pool["n_plus_1_verified"] is None
    assert pool["verification_unavailable_reason"] == "incomplete_load"
    body = (await world["client"].get(f"{C}/rooms/{world['room']}/exceptions", headers=world["admin"])).json()
    item = next(i for i in body["items"] if i["type"] == "capacity_inputs_incomplete")
    assert item["unknown_demand_equipment_count"] == 1 and item["incomplete_reasons"] == ["equipment_without_power_model"]


async def test_10_existing_response_fields_are_unchanged_and_new_ones_are_additive(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    z = await room_capacity(world)
    for key in ("zone_id", "name", "geometry", "units", "installed_rated_kw", "available_kw", "available_complete", "thermal_load", "headroom_kw", "utilization_pct", "level", "redundancy", "plant"):
        assert key in z, key
    for key in ("state", "load_complete", "electrical_kw", "thermal_kw", "electrical_kw_lower_bound", "thermal_kw_lower_bound", "quality", "rack_count", "basis", "assumption"):
        assert key in z["thermal_load"], key
    assert NEW_KEYS <= set(z["thermal_load"]) and {"headroom_verified", "headroom_upper_bound_kw", "headroom_basis"} <= set(z)


async def test_decommissioned_equipment_and_non_it_assets_are_not_part_of_the_population(world):  # noqa: F811
    from tests.api._thermal_helpers import make_sensor

    client, admin = world["client"], world["admin"]
    await equipment(world, demand_kw=40.0)
    ghost = await equipment(world, powered=False)
    sensor = await make_sensor(client, admin, world["site"]["site"], world["room"], 1000, 1000)  # sensors share equipment_placement
    assert sensor["id"]
    assert (await room_capacity(world))["thermal_load"]["placed_equipment_count"] == 2  # the sensor and the cooling units are not IT load
    for status in ("installed", "active", "decommissioned"):
        r = await client.post(f"/api/v1/managed-assets/{ghost['id']}/lifecycle-transition", json={"to_status": status}, headers=admin)
        assert r.status_code == 200, r.text
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "known" and load["placed_equipment_count"] == 1


async def test_superseded_placements_do_not_count(world):  # noqa: F811
    other = await _second_room(world)
    mover = await equipment(world, powered=False)
    assert (await room_capacity(world))["thermal_load"]["unmodelled_equipment_count"] == 1
    engineer = await world["auth_headers"]("Engineer")
    moved = await world["client"].post(f"/api/v1/equipment/{mover['id']}/move", json={"placement_type": "floor_standing", "room_id": other}, headers=engineer)
    assert moved.status_code == 200, moved.text
    await equipment(world, demand_kw=10.0)
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["placed_equipment_count"] == 1  # the closed placement is history


async def test_rectangular_zone_counts_unmodelled_rack_equipment_and_unattributable_floor_equipment(world):  # noqa: F811
    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    rack = await create_rack(client, engineer, world["auth_headers"], room_id=world["room"], x_mm=1000, y_mm=1000, rotation_deg=0)
    row = await make_zone(client, admin, world["room"], name="Row", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=3000)
    for unit in (await client.get(f"{C}/units", headers=admin)).json()["items"]:
        await relate(client, admin, unit["id"], row["id"])

    async def mount(u, demand, powered=True):
        item = await equipment(world, demand_kw=demand, powered=powered)
        moved = await client.post(
            f"/api/v1/equipment/{item['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": world["room"], "rack_id": rack["id"], "u_start": u, "u_end": u + 2, "side": "front"},
            headers=engineer,
        )
        assert moved.status_code == 200, moved.text

    async def row_load():
        zones = (await client.get(f"{C}/rooms/{world['room']}/capacity", headers=admin)).json()["zones"]
        return next(z for z in zones if z["name"] == "Row")

    await mount(1, 25.0)
    assert (await row_load())["thermal_load"]["state"] == "known"
    await mount(5, None, powered=False)  # modelled rack, but one of its servers has no power model
    z = await row_load()
    assert z["thermal_load"]["state"] == "incomplete" and z["thermal_load"]["unmodelled_equipment_count"] == 1
    assert z["thermal_load"]["thermal_kw_lower_bound"] == 25.0 and z["headroom_kw"] is None
    assert "equipment_without_power_model" in z["thermal_load"]["incomplete_reasons"]


async def test_floor_standing_equipment_makes_a_geometry_zone_incomplete(world):  # noqa: F811
    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    await create_rack(client, engineer, world["auth_headers"], room_id=world["room"], x_mm=1000, y_mm=1000, rotation_deg=0)
    row = await make_zone(client, admin, world["room"], name="Row", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=3000)
    for unit in (await client.get(f"{C}/units", headers=admin)).json()["items"]:
        await relate(client, admin, unit["id"], row["id"])
    await equipment(world, demand_kw=10.0)  # floor-standing, modelled: no zone rectangle can claim it
    zones = (await client.get(f"{C}/rooms/{world['room']}/capacity", headers=admin)).json()["zones"]
    z = next(z for z in zones if z["name"] == "Row")
    assert z["thermal_load"]["unattributed_floor_equipment_count"] == 1
    assert z["thermal_load"]["state"] == "incomplete" and "floor_equipment_not_attributable_to_a_zone_area" in z["thermal_load"]["incomplete_reasons"]
    assert z["headroom_kw"] is None
