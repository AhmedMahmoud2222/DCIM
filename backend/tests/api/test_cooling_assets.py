"""Issue #105: cooling units, groups, sensors, zones, relations and placement."""

import uuid

import pytest
from sqlalchemy import text

from tests.api._thermal_helpers import C, activate, make_group, make_sensor, make_unit, make_world, make_zone, relate, seed_plan


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    return {"admin": admin, "site": site["site"], "room": site["room"], "auth_headers": auth_headers}


def match(version: int) -> dict:
    return {"If-Match": str(version)}


async def test_unit_is_a_managed_asset_subtype_with_shared_identity(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"], unit_kind="crac", rated_cooling_capacity_kw=120.5)
    asset = (await client.get(f"/api/v1/managed-assets/{unit['id']}", headers=world["admin"])).json()
    assert asset["asset_type"] == "crac" and asset["id"] == unit["id"] and asset["lifecycle_status"] == "planned"
    assert unit["unit_kind"] == "crac" and unit["version"] == 1
    row = (await db_session.execute(text("SELECT id, unit_kind FROM cooling_unit WHERE id = :i"), {"i": unit["id"]})).one()
    assert str(row.id) == unit["id"]
    # a CRAH and a chiller are separate asset types on the same table
    crah = await make_unit(client, world["admin"], world["site"], unit_kind="crah")
    chiller = await make_unit(client, world["admin"], world["site"], unit_kind="chiller")
    kinds = {u["unit_kind"] for u in (await client.get(f"{C}/units", headers=world["admin"])).json()["items"]}
    assert kinds == {"crac", "crah", "chiller"} and crah["id"] != chiller["id"]


async def test_unknown_engineering_inputs_stay_null_never_zero(client, world):
    unit = await make_unit(client, world["admin"], world["site"])
    for field in ("rated_cooling_capacity_kw", "configured_cooling_capacity_kw", "airflow_capacity_m3_s", "supply_air_target_c", "return_air_design_c",
                  "humidity_min_percent", "humidity_max_percent", "supply_direction_deg", "cooling_group_id"):
        assert unit[field] is None, field
    fetched = (await client.get(f"{C}/units/{unit['id']}", headers=world["admin"])).json()
    assert fetched["rated_cooling_capacity_kw"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"rated_cooling_capacity_kw": 0}, {"rated_cooling_capacity_kw": -5}, {"configured_cooling_capacity_kw": 0}, {"airflow_capacity_m3_s": -1},
        {"humidity_min_percent": 101}, {"humidity_min_percent": 60, "humidity_max_percent": 40}, {"supply_direction_deg": 360},
        {"rated_cooling_capacity_kw": 50, "configured_cooling_capacity_kw": 60}, {"unit_kind": "boiler"}, {"operating_status": "exploded"},
        {"supply_air_target_c": 500},
    ],
)
async def test_capacity_and_range_validation(client, world, bad):
    body = {"unit_kind": "crah", "asset_tag": f"CU-{uuid.uuid4().hex[:6]}", "site_id": world["site"], "name": "Bad", **bad}
    response = await client.post(f"{C}/units", json=body, headers=world["admin"])
    assert response.status_code == 422, response.text


async def test_derated_capacity_may_be_below_rated(client, world):
    unit = await make_unit(client, world["admin"], world["site"], rated_cooling_capacity_kw=100, configured_cooling_capacity_kw=80)
    assert (unit["rated_cooling_capacity_kw"], unit["configured_cooling_capacity_kw"]) == (100.0, 80.0)


async def test_update_requires_if_match_and_bumps_version(client, world):
    unit = await make_unit(client, world["admin"], world["site"])
    assert (await client.patch(f"{C}/units/{unit['id']}", json={"name": "X"}, headers=world["admin"])).status_code == 428
    ok = await client.patch(f"{C}/units/{unit['id']}", json={"name": "Renamed", "rated_cooling_capacity_kw": 90}, headers={**world["admin"], **match(1)})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["name"] == "Renamed"
    stale = await client.patch(f"{C}/units/{unit['id']}", json={"name": "Again"}, headers={**world["admin"], **match(1)})
    assert stale.status_code == 409
    cleared = await client.patch(f"{C}/units/{unit['id']}", json={"rated_cooling_capacity_kw": None}, headers={**world["admin"], **match(2)})
    assert cleared.status_code == 200 and cleared.json()["rated_cooling_capacity_kw"] is None  # back to unknown, not zero


async def test_patch_cannot_make_configured_exceed_rated(client, world):
    unit = await make_unit(client, world["admin"], world["site"], rated_cooling_capacity_kw=50)
    response = await client.patch(f"{C}/units/{unit['id']}", json={"configured_cooling_capacity_kw": 70}, headers={**world["admin"], **match(1)})
    assert response.status_code == 422


async def test_unit_placement_uses_calibrated_coordinates_and_keeps_history(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"])
    place = lambda body, v=None: client.put(f"{C}/units/{unit['id']}/placement", json=body, headers={**world["admin"], **({"If-Match": str(v)} if v else {})})  # noqa: E731
    first = await place({"room_id": world["room"], "x_mm": 500, "y_mm": 600, "rotation_deg": 90})
    assert first.status_code == 200 and first.json()["position_calibration_id"] is not None and first.json()["version"] == 1
    moved = await place({"room_id": world["room"], "x_mm": 900, "y_mm": 1000}, "1")
    assert moved.status_code == 200
    stale = await place({"room_id": world["room"], "x_mm": 1, "y_mm": 1}, "1")
    assert stale.status_code == 409
    rows = (await db_session.execute(text("SELECT x_mm, y_mm, effective_to IS NULL AS current FROM equipment_placement WHERE equipment_id = :i ORDER BY effective_from"), {"i": unit["id"]})).all()
    assert [(r.x_mm, r.y_mm, r.current) for r in rows] == [(500, 600, False), (900, 1000, True)]  # close-then-open, old coordinates preserved


async def test_placement_validation(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"])
    url = f"{C}/units/{unit['id']}/placement"
    outside = await client.put(url, json={"room_id": world["room"], "x_mm": 9000, "y_mm": 100}, headers=world["admin"])
    assert outside.status_code == 422 and outside.json()["title"] == "Outside Room Boundary"
    half = await client.put(url, json={"room_id": world["room"], "x_mm": 100}, headers=world["admin"])
    assert half.status_code == 422
    rack_mounted = await client.put(url, json={"room_id": world["room"], "placement_type": "rack_mounted", "rack_id": str(uuid.uuid4()), "u_start": 1, "u_end": 2, "side": "front"}, headers=world["admin"])
    assert rack_mounted.status_code == 422  # air handlers are not rack mounted
    other_admin = world["admin"]
    other = await make_world(client, world["auth_headers"])
    cross = await client.put(url, json={"room_id": other[1]["room"]}, headers=other_admin)
    assert cross.status_code == 422 and cross.json()["title"] == "Cross-Site Placement"


async def test_position_requires_a_calibrated_active_floor_plan(client, world, db_session):
    admin2, site2 = await make_world(client, world["auth_headers"])
    await seed_plan(db_session, site2["room"], calibrated=False)
    sensor = await make_sensor(client, admin2, site2["site"])
    response = await client.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": site2["room"], "x_mm": 100, "y_mm": 100}, headers=admin2)
    assert response.status_code == 422 and response.json()["title"] == "Calibrated Floor Plan Required"
    # association without coordinates is allowed and is never presented as a position
    ok = await client.put(f"{C}/sensors/{sensor['id']}/placement", json={"room_id": site2["room"]}, headers=admin2)
    assert ok.status_code == 200 and ok.json()["x_mm"] is None


async def test_database_rejects_a_placement_in_another_sites_room(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"])
    _, other = await make_world(client, world["auth_headers"])
    with pytest.raises(Exception, match="different site"):
        await db_session.execute(
            text("INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1)"),
            {"e": unit["id"], "r": other["room"]},
        )
    await db_session.rollback()


async def test_retire_unit_is_refused_while_related_to_a_zone_and_then_closes_placement(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"])
    await activate(client, world["admin"], unit["id"])
    await client.put(f"{C}/units/{unit['id']}/placement", json={"room_id": world["room"], "x_mm": 100, "y_mm": 100}, headers=world["admin"])
    zone = await make_zone(client, world["admin"], world["room"])
    relation = await relate(client, world["admin"], unit["id"], zone["id"])
    refused = await client.post(f"{C}/units/{unit['id']}/retire", headers={**world["admin"], **match(1)})
    assert refused.status_code == 409
    # the generic lifecycle endpoint cannot bypass it either (database trigger)
    bypass = await client.post(f"/api/v1/managed-assets/{unit['id']}/lifecycle-transition", json={"to_status": "decommissioned"}, headers=world["admin"])
    assert bypass.status_code == 409, bypass.text
    assert (await client.delete(f"{C}/relations/{relation['id']}", headers=world["admin"])).status_code == 204
    done = await client.post(f"{C}/units/{unit['id']}/retire", headers={**world["admin"], **match(1)})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["lifecycle_status"] == "decommissioned" and body["operating_status"] == "offline" and body["placement"] is None
    assert (await client.patch(f"{C}/units/{unit['id']}", json={"name": "x"}, headers={**world["admin"], **match(2)})).status_code == 409


async def test_groups_share_a_site_with_their_units_and_cannot_retire_with_members(client, world):
    group = await make_group(client, world["admin"], world["site"])
    unit = await make_unit(client, world["admin"], world["site"], cooling_group_id=group["id"])
    assert unit["cooling_group_id"] == group["id"]
    assert (await client.get(f"{C}/groups/{group['id']}", headers=world["admin"])).json()["member_ids"] == [unit["id"]]
    dup = await client.post(f"{C}/groups", json={"site_id": world["site"], "name": group["name"]}, headers=world["admin"])
    assert dup.status_code == 409
    refused = await client.post(f"{C}/groups/{group['id']}/retire", headers={**world["admin"], **match(1)})
    assert refused.status_code == 409
    _, other = await make_world(client, world["auth_headers"])
    other_site_unit = {"unit_kind": "crah", "asset_tag": "X1", "site_id": other["site"], "name": "x", "cooling_group_id": group["id"]}
    cross = await client.post(f"{C}/units", json=other_site_unit, headers=world["admin"])
    assert cross.status_code == 422 and cross.json()["title"] == "Cross-Site Group"
    cleared = await client.patch(f"{C}/units/{unit['id']}", json={"cooling_group_id": None}, headers={**world["admin"], **match(1)})
    assert cleared.status_code == 200
    assert (await client.post(f"{C}/groups/{group['id']}/retire", headers={**world["admin"], **match(1)})).status_code == 200


async def test_sensor_is_a_managed_asset_and_metric_mapping_is_validated_against_its_kind(client, world):
    sensor = await make_sensor(client, world["admin"], world["site"], sensor_kind="humidity")
    asset = (await client.get(f"/api/v1/managed-assets/{sensor['id']}", headers=world["admin"])).json()
    assert asset["asset_type"] == "sensor" and sensor["allowed_metrics"] == ["humidity_percent"]
    # airflow direction only makes sense for airflow sensors
    bad = await client.post(f"{C}/sensors", json={"asset_tag": "S2", "site_id": world["site"], "name": "t", "sensor_kind": "temperature", "flow_direction_deg": 10}, headers=world["admin"])
    assert bad.status_code == 422
    from app.domain.integration.models import Integration  # noqa: F401

    integration = (await client.post("/api/v1/integrations", json={"name": f"i-{uuid.uuid4().hex[:6]}", "integration_type": "snmp", "target_host": "192.0.2.5", "config": {}}, headers=world["admin"]))
    if integration.status_code == 201:
        wrong = await client.post("/api/v1/telemetry/mappings", json={"integration_id": integration.json()["id"], "managed_asset_id": sensor["id"], "source_identifier": "oid1", "canonical_metric": "temperature_c", "unit": "degC", "scale": 1}, headers=world["admin"])
        assert wrong.status_code == 422 and wrong.json()["title"] == "Metric not supported by sensor"
        right = await client.post("/api/v1/telemetry/mappings", json={"integration_id": integration.json()["id"], "managed_asset_id": sensor["id"], "source_identifier": "oid2", "canonical_metric": "humidity_percent", "unit": "%", "scale": 1}, headers=world["admin"])
        assert right.status_code == 201, right.text
        detail = (await client.get(f"{C}/sensors/{sensor['id']}", headers=world["admin"])).json()
        assert [m["canonical_metric"] for m in detail["mappings"]] == ["humidity_percent"]


async def test_sensor_placement_history_and_rack_mounting(client, world, db_session):
    sensor = await make_sensor(client, world["admin"], world["site"], world["room"], 1000, 1000)
    url = f"{C}/sensors/{sensor['id']}/placement"
    moved = await client.put(url, json={"room_id": world["room"], "x_mm": 2000, "y_mm": 1500}, headers={**world["admin"], "If-Match": "1"})
    assert moved.status_code == 200
    rows = (await db_session.execute(text("SELECT x_mm, y_mm FROM equipment_placement WHERE equipment_id = :i ORDER BY effective_from"), {"i": sensor["id"]})).all()
    assert [(r.x_mm, r.y_mm) for r in rows] == [(1000, 1000), (2000, 1500)]
    from tests.api.test_user_groups import _make_rack

    rack = await _make_rack(client, world["admin"], world["auth_headers"], world["room"])
    mounted = await client.put(url, json={"room_id": world["room"], "placement_type": "rack_mounted", "rack_id": rack, "u_start": 3, "u_end": 4, "side": "front"}, headers=world["admin"])
    assert mounted.status_code == 200, mounted.text
    assert (mounted.json()["rack_id"], mounted.json()["u_start"], mounted.json()["x_mm"]) == (rack, 3, None)  # position comes from the rack, not stored
    other = await make_sensor(client, world["admin"], world["site"])
    clash = await client.put(f"{C}/sensors/{other['id']}/placement", json={"room_id": world["room"], "placement_type": "rack_mounted", "rack_id": rack, "u_start": 3, "u_end": 4, "side": "front"}, headers=world["admin"])
    assert clash.status_code == 409  # same U, same face
    too_high = await client.put(f"{C}/sensors/{other['id']}/placement", json={"room_id": world["room"], "placement_type": "rack_mounted", "rack_id": rack, "u_start": 80, "u_end": 81, "side": "front"}, headers=world["admin"])
    assert too_high.status_code == 422
    with_xy = await client.put(url, json={"room_id": world["room"], "placement_type": "rack_mounted", "rack_id": rack, "u_start": 5, "u_end": 6, "side": "front", "x_mm": 1, "y_mm": 1}, headers=world["admin"])
    assert with_xy.status_code == 422
    cleared = await client.delete(url, headers=world["admin"])
    assert cleared.status_code == 204
    assert (await client.delete(url, headers=world["admin"])).status_code == 404


async def test_zone_geometry_is_validated_against_the_room_boundary(client, world):
    admin, room = world["admin"], world["room"]
    ok = await make_zone(client, admin, room, zone_kind="cold_aisle", geometry_type="rect", x_mm=500, y_mm=500, width_mm=1200, height_mm=3000)
    assert ok["geometry_validation"] == "within_boundary" and ok["authority"] == "operator_configured"
    for body in (
        {"geometry_type": "rect", "x_mm": 5000, "y_mm": 500, "width_mm": 2000, "height_mm": 1000},  # leaves the 6000 mm room
        {"geometry_type": "rect", "x_mm": 100, "y_mm": 100, "width_mm": 0, "height_mm": 1000},
        {"geometry_type": "polygon", "points": [[0, 0], [1000, 1000], [1000, 0], [0, 1000]]},  # bow-tie
        {"geometry_type": "polygon", "points": [[0, 0], [10, 10]]},
        {"geometry_type": "polygon", "points": [[100, 100], [2000, 100], [2000, 2000]], "x_mm": 5},
    ):
        response = await client.post(f"{C}/zones", json={"room_id": room, "name": f"z-{uuid.uuid4().hex[:4]}", "zone_kind": "hot_aisle", **body}, headers=admin)
        assert response.status_code == 422, (body, response.text)
    no_geometry = await client.post(f"{C}/zones", json={"room_id": room, "name": "n", "zone_kind": "hot_aisle"}, headers=admin)
    assert no_geometry.status_code == 422  # only a served zone may cover the whole room
    bad_containment = await client.post(f"{C}/zones", json={"room_id": room, "name": "c", "zone_kind": "served_zone", "containment": "contained"}, headers=admin)
    assert bad_containment.status_code == 422
    whole = await make_zone(client, admin, room, name="whole room")
    assert whole["geometry_type"] is None and whole["geometry_validation"] == "no_geometry"
    dup = await client.post(f"{C}/zones", json={"room_id": room, "name": "whole room", "zone_kind": "served_zone"}, headers=admin)
    assert dup.status_code == 409


async def test_containment_elements_belong_to_a_contained_aisle_and_stay_inside_the_room(client, world):
    admin, room = world["admin"], world["room"]
    aisle = await make_zone(client, admin, room, zone_kind="hot_aisle", containment="contained", geometry_type="rect", x_mm=2000, y_mm=500, width_mm=1200, height_mm=3000)
    add = await client.post(f"{C}/zones/{aisle['id']}/containment-elements", json={"element_kind": "boundary", "x1_mm": 2000, "y1_mm": 500, "x2_mm": 2000, "y2_mm": 3500}, headers={**admin, **match(1)})
    assert add.status_code == 201, add.text
    opening = await client.post(f"{C}/zones/{aisle['id']}/containment-elements", json={"element_kind": "opening", "x1_mm": 2000, "y1_mm": 1000, "x2_mm": 2000, "y2_mm": 1500}, headers={**admin, **match(2)})
    assert opening.status_code == 201
    outside = await client.post(f"{C}/zones/{aisle['id']}/containment-elements", json={"element_kind": "boundary", "x1_mm": 0, "y1_mm": 0, "x2_mm": 9000, "y2_mm": 0}, headers={**admin, **match(3)})
    assert outside.status_code == 422
    stale = await client.post(f"{C}/zones/{aisle['id']}/containment-elements", json={"element_kind": "boundary", "x1_mm": 100, "y1_mm": 100, "x2_mm": 200, "y2_mm": 100}, headers={**admin, **match(1)})
    assert stale.status_code == 409
    plain = await make_zone(client, admin, room, zone_kind="cold_aisle", geometry_type="rect", x_mm=4000, y_mm=500, width_mm=1000, height_mm=3000)
    not_contained = await client.post(f"{C}/zones/{plain['id']}/containment-elements", json={"element_kind": "boundary", "x1_mm": 4000, "y1_mm": 500, "x2_mm": 4000, "y2_mm": 900}, headers={**admin, **match(1)})
    assert not_contained.status_code == 422
    fetched = (await client.get(f"{C}/zones/{aisle['id']}", headers=admin)).json()
    assert [e["element_kind"] for e in fetched["elements"]] == ["boundary", "opening"]


async def test_relationship_semantics_and_zone_kind_rules(client, world):
    admin, room = world["admin"], world["room"]
    unit = await make_unit(client, admin, world["site"])
    served = await make_zone(client, admin, room)
    cold = await make_zone(client, admin, room, zone_kind="cold_aisle", geometry_type="rect", x_mm=100, y_mm=100, width_mm=800, height_mm=800)
    hot = await make_zone(client, admin, room, zone_kind="hot_aisle", geometry_type="rect", x_mm=2000, y_mm=100, width_mm=800, height_mm=800)
    assert (await relate(client, admin, unit["id"], served["id"], "serves", "authoritative"))["semantics"] == "authoritative"
    await relate(client, admin, unit["id"], cold["id"], "supplies")
    await relate(client, admin, unit["id"], hot["id"], "returns_from", "modelled")
    wrong = await client.post(f"{C}/relations", json={"cooling_unit_id": unit["id"], "thermal_zone_id": hot["id"], "relation_kind": "serves"}, headers=admin)
    assert wrong.status_code == 422
    dup = await client.post(f"{C}/relations", json={"cooling_unit_id": unit["id"], "thermal_zone_id": served["id"], "relation_kind": "serves"}, headers=admin)
    assert dup.status_code == 409
    bad_semantics = await client.post(f"{C}/relations", json={"cooling_unit_id": unit["id"], "thermal_zone_id": cold["id"], "relation_kind": "supplies", "semantics": "guessed"}, headers=admin)
    assert bad_semantics.status_code == 422
    assert len((await client.get(f"{C}/relations", params={"cooling_unit_id": unit["id"]}, headers=admin)).json()) == 3
    refused = await client.post(f"{C}/zones/{served['id']}/retire", headers={**admin, **match(1)})
    assert refused.status_code == 409  # referenced by a unit
    _, other = await make_world(client, world["auth_headers"])
    foreign_unit = await make_unit(client, admin, other["site"])
    cross = await client.post(f"{C}/relations", json={"cooling_unit_id": foreign_unit["id"], "thermal_zone_id": served["id"], "relation_kind": "serves"}, headers=admin)
    assert cross.status_code == 422 and cross.json()["title"] == "Cross-Site Relationship"


async def test_every_mutation_is_audited(client, world, db_session):
    unit = await make_unit(client, world["admin"], world["site"])
    await client.patch(f"{C}/units/{unit['id']}", json={"name": "Audited"}, headers={**world["admin"], **match(1)})
    zone = await make_zone(client, world["admin"], world["room"])
    actions = {r.action for r in (await db_session.execute(text("SELECT action FROM audit_log"))).all()}
    assert {"cooling.unit.create", "cooling.unit.update", "cooling.zone.create"} <= actions
    assert zone["id"]
