"""Issue #105 follow-up (review finding F-1): only OPERATIONAL equipment is part of the current thermal-load population.

planned and reserved equipment has been allocated a place but is not physically present, so it dissipates no heat today and must
neither make an otherwise complete room incomplete nor add its nameplate figure to the load. installed (staged), active and
maintenance equipment is physically present and may be energised, so it stays in the population; with no power model it is unknown
demand, never zero. decommissioned and removed equipment is gone. Everything runs through the real HTTP/PostgreSQL/roll-up path."""

import pytest

from tests.api._phase2_helpers import create_rack
from tests.api._thermal_helpers import C, make_zone, relate
from tests.api.test_thermal_load_completeness import (  # noqa: F401  (fixture re-export)
    equipment,
    room_capacity,
    set_lifecycle,
    world,
)


async def test_1_planned_equipment_without_a_power_model_does_not_invalidate_a_complete_operational_room(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world, demand_kw=20.0)
    for _ in range(3):
        await equipment(world, powered=False, lifecycle="planned")
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "known" and load["thermal_kw"] == 50.0 and load["load_complete"] is True
    assert (load["placed_equipment_count"], load["modelled_equipment_count"], load["pending_equipment_count"]) == (2, 2, 3)
    assert z["headroom_kw"] == 150.0 and z["headroom_verified"] is True


async def test_2_reserved_equipment_without_a_power_model_does_not_invalidate_operational_headroom(world):  # noqa: F811
    await equipment(world, demand_kw=40.0)
    await equipment(world, powered=False, lifecycle="reserved")
    z = await room_capacity(world)
    assert z["thermal_load"]["state"] == "known" and z["thermal_load"]["pending_equipment_count"] == 1
    assert z["headroom_kw"] == 160.0 and z["headroom_verified"] is True


@pytest.mark.parametrize("lifecycle", ["installed", "active", "maintenance"])
async def test_3_physically_present_equipment_without_a_power_model_still_makes_the_room_incomplete(world, lifecycle):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world, powered=False, lifecycle=lifecycle)
    z = await room_capacity(world)
    load = z["thermal_load"]
    assert load["state"] == "incomplete" and load["thermal_kw"] is None and load["thermal_kw_lower_bound"] == 30.0
    assert load["unmodelled_equipment_count"] == 1 and load["placed_equipment_count"] == 2
    assert z["headroom_kw"] is None and z["headroom_verified"] is False


async def test_4_active_equipment_without_a_demand_figure_remains_unknown_demand(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    await equipment(world, lifecycle="active")  # modelled (has a feed) but never reported demand
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "incomplete" and load["missing_demand_equipment_count"] == 1 and load["unmodelled_equipment_count"] == 0


async def test_5_mixed_populations_count_each_item_once_by_lifecycle(world):  # noqa: F811
    await equipment(world, demand_kw=20.0, lifecycle="installed")
    await equipment(world, demand_kw=30.0, lifecycle="active")
    await equipment(world, demand_kw=10.0, lifecycle="maintenance")
    for lifecycle in ("planned", "planned", "reserved"):
        await equipment(world, powered=False, lifecycle=lifecycle)
    # planned/reserved items that DO have a power model: a nameplate-sized or measured figure must not become heat today
    await equipment(world, demand_kw=500.0, lifecycle="planned")
    await equipment(world, lifecycle="reserved")  # modelled, no demand: not an unknown either
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "known" and load["thermal_kw"] == 60.0
    assert (load["placed_equipment_count"], load["modelled_equipment_count"], load["pending_equipment_count"]) == (3, 3, 5)
    assert load["unknown_demand_equipment_count"] == 0


async def test_5b_decommissioned_equipment_is_never_counted_even_when_modelled(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    gone = await equipment(world, demand_kw=400.0)
    await set_lifecycle(world, gone["id"], "decommissioned_from_active")
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "known" and load["thermal_kw"] == 30.0 and load["placed_equipment_count"] == 1


async def test_6_rack_contained_and_floor_standing_equipment_follow_the_same_policy(world):  # noqa: F811
    client, admin = world["client"], world["admin"]
    engineer = await world["auth_headers"]("Engineer")
    rack = await create_rack(client, engineer, world["auth_headers"], room_id=world["room"], x_mm=1000, y_mm=1000, rotation_deg=0)

    async def mount(u, demand, *, powered=True, lifecycle="active"):
        item = await equipment(world, demand_kw=demand, powered=powered, lifecycle=lifecycle)
        moved = await client.post(
            f"/api/v1/equipment/{item['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": world["room"], "rack_id": rack["id"], "u_start": u, "u_end": u + 2, "side": "front"},
            headers=engineer,
        )
        assert moved.status_code == 200, moved.text
        return item

    await mount(1, 25.0)
    await mount(5, None, powered=False, lifecycle="planned")  # rack-mounted, planned: ignored
    await equipment(world, powered=False, lifecycle="reserved")  # floor-standing, reserved: ignored
    zone_row = await make_zone(client, admin, world["room"], name="Row", geometry_type="rect", x_mm=0, y_mm=0, width_mm=3000, height_mm=3000)
    for unit in (await client.get(f"{C}/units", headers=admin)).json()["items"]:
        await relate(client, admin, unit["id"], zone_row["id"])

    async def zones():
        body = (await client.get(f"{C}/rooms/{world['room']}/capacity", headers=admin)).json()["zones"]
        return next(z for z in body if z["name"] == "Whole hall"), next(z for z in body if z["name"] == "Row")

    whole, row = await zones()
    assert whole["thermal_load"]["state"] == "known" and whole["thermal_load"]["thermal_kw"] == 25.0
    assert row["thermal_load"]["state"] == "known" and row["thermal_load"]["thermal_kw"] == 25.0  # planned floor item does not count as unattributable
    assert row["thermal_load"]["pending_equipment_count"] == 1
    # the same two positions with PHYSICALLY PRESENT equipment make both zones incomplete
    await mount(9, None, powered=False, lifecycle="installed")
    await equipment(world, powered=False, lifecycle="installed")
    whole, row = await zones()
    assert whole["thermal_load"]["state"] == "incomplete" and whole["thermal_load"]["unmodelled_equipment_count"] == 2
    assert row["thermal_load"]["state"] == "incomplete"
    assert {"equipment_without_power_model", "floor_equipment_not_attributable_to_a_zone_area"} <= set(row["thermal_load"]["incomplete_reasons"])


async def test_7_n_plus_1_is_not_verified_while_installed_demand_is_incomplete(world):  # noqa: F811
    await equipment(world, demand_kw=1.0)
    await equipment(world, powered=False, lifecycle="installed")
    z = await room_capacity(world)
    pool = z["redundancy"]["pools"][0]
    assert z["redundancy"]["state"] == "redundant_unverified" and pool["n_plus_1_verified"] is None
    assert pool["verification_unavailable_reason"] == "incomplete_load"


async def test_8_n_plus_1_is_not_downgraded_only_because_planned_or_reserved_equipment_exists(world):  # noqa: F811
    await equipment(world, demand_kw=1.0)
    for lifecycle in ("planned", "reserved"):
        await equipment(world, powered=False, lifecycle=lifecycle)
    z = await room_capacity(world)
    pool = z["redundancy"]["pools"][0]
    assert z["redundancy"]["state"] == "redundant" and pool["n_plus_1_verified"] is True and pool["verification_unavailable_reason"] is None
    assert z["level"] == "ok"


async def test_9_the_exceptions_view_applies_the_same_policy(world):  # noqa: F811
    await equipment(world, demand_kw=10.0)
    await equipment(world, powered=False, lifecycle="planned")
    body = (await world["client"].get(f"{C}/rooms/{world['room']}/exceptions", headers=world["admin"])).json()
    assert not [i for i in body["items"] if i["type"] == "capacity_inputs_incomplete"]
    await equipment(world, powered=False, lifecycle="installed")
    body = (await world["client"].get(f"{C}/rooms/{world['room']}/exceptions", headers=world["admin"])).json()
    item = next(i for i in body["items"] if i["type"] == "capacity_inputs_incomplete")
    assert item["unknown_demand_equipment_count"] == 1


async def test_a_room_with_only_planned_or_reserved_equipment_is_unknown_with_that_reason(world):  # noqa: F811
    await equipment(world, powered=False, lifecycle="planned")
    await equipment(world, powered=False, lifecycle="reserved")
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "unknown" and load["thermal_kw"] is None and load["pending_equipment_count"] == 2
    assert load["incomplete_reasons"] == ["only_planned_or_reserved_equipment_placed"]


async def test_installing_a_planned_item_brings_it_into_the_population(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    item = await equipment(world, powered=False, lifecycle="planned")
    assert (await room_capacity(world))["thermal_load"]["state"] == "known"
    await set_lifecycle(world, item["id"], "installed")
    load = (await room_capacity(world))["thermal_load"]
    assert load["state"] == "incomplete" and load["unmodelled_equipment_count"] == 1 and load["pending_equipment_count"] == 0


async def test_10_response_contract_is_additive(world):  # noqa: F811
    await equipment(world, demand_kw=30.0)
    z = await room_capacity(world)
    for key in ("state", "load_complete", "electrical_kw", "thermal_kw", "electrical_kw_lower_bound", "thermal_kw_lower_bound", "quality", "rack_count", "basis",
                "placed_equipment_count", "modelled_equipment_count", "unknown_demand_equipment_count", "unmodelled_equipment_count",
                "missing_demand_equipment_count", "unattributed_floor_equipment_count", "incomplete_reasons"):
        assert key in z["thermal_load"], key
    assert z["thermal_load"]["pending_equipment_count"] == 0
