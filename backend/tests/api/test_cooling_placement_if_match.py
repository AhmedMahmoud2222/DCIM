"""Issue #105 (review blocker B2): the cooling placement routes bind the real `If-Match` HTTP header.

They used to declare `if_match: str | None = None`, which FastAPI reads as a QUERY parameter, so a client sending the
header got no concurrency protection at all. Every test here sends the header; none uses `params={"if_match": ...}`.

Policy (identical to the existing rack and equipment placement routes): `If-Match` is optional on a placement write
and an absent header means an unconditional write; a present header must be an integer and must equal the CURRENT
placement version, otherwise 409. A header on an asset that has no current placement can never match (B3)."""

import pytest
from sqlalchemy import text

from tests.api._thermal_helpers import C, activate, make_sensor, make_unit, make_world, seed_plan


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin, site = await make_world(client, auth_headers)
    await seed_plan(db_session, site["room"])
    sensor = await make_sensor(client, admin, site["site"])
    unit = await make_unit(client, admin, site["site"])
    await activate(client, admin, unit["id"])
    return {"admin": admin, "site": site["site"], "room": site["room"], "db": db_session, "sensor": sensor, "unit": unit}


def target(world, kind):
    asset = world[kind]
    return asset["id"], f"{C}/{'sensors' if kind == 'sensor' else 'units'}/{asset['id']}/placement"


def body(world, x):
    return {"room_id": world["room"], "placement_type": "floor_standing", "x_mm": x, "y_mm": 1000}


async def snapshot(world, asset_id) -> dict:
    db = world["db"]
    placements = (await db.execute(text(
        "SELECT id::text, version, x_mm, effective_to IS NULL AS current FROM equipment_placement WHERE equipment_id = :i ORDER BY effective_from, version"
    ), {"i": asset_id})).all()
    audit = (await db.execute(text("SELECT count(*) FROM audit_log WHERE entity_id = :i"), {"i": asset_id})).scalar_one()
    outbox = (await db.execute(text("SELECT count(*) FROM outbox_event WHERE aggregate_id = :i"), {"i": asset_id})).scalar_one()
    return {"placements": [tuple(r) for r in placements], "audit": audit, "outbox": outbox}


@pytest.mark.parametrize("kind", ["sensor", "unit"])
async def test_place_move_unplace_with_the_header(client, world, kind):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    placed = await client.put(url, json=body(world, 1000), headers=admin)  # first placement: nothing to match yet, header omitted
    assert placed.status_code == 200 and placed.json()["version"] == 1
    moved = await client.put(url, json=body(world, 2000), headers={**admin, "If-Match": "1"})
    assert moved.status_code == 200 and moved.json()["version"] == 2
    quoted = await client.put(url, json=body(world, 3000), headers={**admin, "If-Match": '"2"'})  # quoted ETag-style token
    assert quoted.status_code == 200 and quoted.json()["version"] == 3
    assert (await client.delete(url, headers={**admin, "If-Match": "3"})).status_code == 204
    state = await snapshot(world, asset_id)
    assert [(v, c) for _, v, _, c in state["placements"]] == [(1, False), (2, False), (3, False)]


@pytest.mark.parametrize("kind", ["sensor", "unit"])
@pytest.mark.parametrize("verb", ["put", "delete"])
async def test_a_stale_header_conflicts_and_changes_nothing(client, world, kind, verb):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    assert (await client.put(url, json=body(world, 1000), headers=admin)).status_code == 200
    assert (await client.put(url, json=body(world, 2000), headers={**admin, "If-Match": "1"})).status_code == 200  # current is now 2
    before = await snapshot(world, asset_id)
    request = client.put(url, json=body(world, 4000), headers={**admin, "If-Match": "1"}) if verb == "put" else client.delete(url, headers={**admin, "If-Match": "1"})
    response = await request
    assert response.status_code == 409, response.text
    assert await snapshot(world, asset_id) == before  # current row, history, version, audit and outbox all untouched


@pytest.mark.parametrize("kind", ["sensor", "unit"])
@pytest.mark.parametrize("verb", ["put", "delete"])
@pytest.mark.parametrize("token", ["abc", "1.5", "", "1; 2"])
async def test_a_malformed_header_is_rejected_and_changes_nothing(client, world, kind, verb, token):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    assert (await client.put(url, json=body(world, 1000), headers=admin)).status_code == 200
    before = await snapshot(world, asset_id)
    headers = {**admin, "If-Match": token}
    response = await (client.put(url, json=body(world, 4000), headers=headers) if verb == "put" else client.delete(url, headers=headers))
    assert response.status_code == 400, response.text
    assert await snapshot(world, asset_id) == before


@pytest.mark.parametrize("kind", ["sensor", "unit"])
async def test_a_missing_header_is_an_unconditional_write_like_racks_and_equipment(client, world, kind):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    assert (await client.put(url, json=body(world, 1000), headers=admin)).status_code == 200
    assert (await client.put(url, json=body(world, 2000), headers=admin)).status_code == 200
    assert (await client.delete(url, headers=admin)).status_code == 204
    assert [v for _, v, _, _ in (await snapshot(world, asset_id))["placements"]] == [1, 2]


@pytest.mark.parametrize("kind", ["sensor", "unit"])
async def test_the_header_cannot_be_bypassed_by_omitting_or_moving_it_to_the_query(client, world, kind):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    assert (await client.put(url, json=body(world, 1000), headers=admin)).status_code == 200
    assert (await client.put(url, json=body(world, 2000), headers={**admin, "If-Match": "1"})).status_code == 200  # current is 2
    before = await snapshot(world, asset_id)
    # The stale header is what counts. A query parameter of the old accidental name matching the current version
    # changes nothing: the request is judged on the header alone and stays a conflict.
    smuggled = await client.put(url, json=body(world, 5000), headers={**admin, "If-Match": "1"}, params={"if_match": 2})
    assert smuggled.status_code == 409
    assert await snapshot(world, asset_id) == before
    # And the query parameter alone is no longer a concurrency mechanism: it is ignored, so the write is unconditional.
    ignored = await client.put(url, json=body(world, 3000), headers=admin, params={"if_match": 99})
    assert ignored.status_code == 200 and ignored.json()["version"] == 3


@pytest.mark.parametrize("kind", ["sensor", "unit"])
async def test_a_header_on_an_unplaced_asset_never_matches(client, world, kind):
    asset_id, url = target(world, kind)
    admin = world["admin"]
    before = await snapshot(world, asset_id)
    response = await client.put(url, json=body(world, 1000), headers={**admin, "If-Match": "1"})
    assert response.status_code == 409
    assert await snapshot(world, asset_id) == before
