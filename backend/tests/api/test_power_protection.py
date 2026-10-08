"""Issue #102 area A: protection devices as nodes of the existing power graph."""

import uuid

import pytest
from sqlalchemy import text

from tests.api._phase3_helpers import connection_body, create_pdu, create_room_and_site, create_ups

P = "/api/v1/power"


def device_body(ups_asset_id, site_id, **extra):
    body = {
        "housing_asset_id": ups_asset_id, "site_id": site_id, "label": "CB-1", "device_type": "breaker",
        "rating_a": 63, "voltage_v": 230, "poles": 1, "phase_config": "single",
    }
    body.update(extra)
    return body


async def make_ups(client, headers, room_id):
    node = await create_ups(client, headers, room_id)
    return node["id"], node["managed_asset_id"]


@pytest.mark.asyncio
async def test_create_get_list_and_rated_kw(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    room_id, site_id = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, h, room_id)
    r = await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_id), headers=h)
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["state"] == "closed" and dev["rated_kw"] == 14.49 and dev["version"] == 1
    three = await client.post(
        f"{P}/protection-devices",
        json=device_body(ups_asset, site_id, label="CB-3", rating_a=100, voltage_v=400, poles=3, phase_config="three"),
        headers=h,
    )
    assert three.status_code == 201 and three.json()["rated_kw"] == 69.282
    got = await client.get(f"{P}/protection-devices/{dev['id']}", headers=h)
    assert got.json()["id"] == dev["id"]
    listed = await client.get(f"{P}/protection-devices", params={"site_id": site_id}, headers=h)
    assert listed.json()["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"rating_a": 0}, {"rating_a": 7000}, {"voltage_v": 5}, {"voltage_v": 5000},
        {"poles": 3, "phase_config": "single"}, {"poles": 1, "phase_config": "three"},
        {"device_type": "magic"}, {"status": "weird"}, {"phase_config": "four"},
    ],
)
async def test_unsafe_shapes_rejected(client, auth_headers, extra):
    h = await auth_headers("DCIM Manager")
    room_id, site_id = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, h, room_id)
    r = await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_id, **extra), headers=h)
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_housing_in_other_site_rejected(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    _, site_b = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, h, room_a)
    r = await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_b), headers=h)
    assert r.status_code == 422 and "different site" in r.text
    r = await client.post(f"{P}/protection-devices", json=device_body(str(uuid.uuid4()), site_a), headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_links_stay_in_site_and_reject_cycles_and_self_links(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    room_b, _ = await create_room_and_site(client, auth_headers)
    ups_a, ups_a_asset = await make_ups(client, h, room_a)
    ups_b, _ = await make_ups(client, h, room_b)
    dev = (await client.post(f"{P}/protection-devices", json=device_body(ups_a_asset, site_a), headers=h)).json()
    pdu = await create_pdu(client, h)

    ok = await client.post(f"{P}/connections", json=connection_body(ups_a, dev["id"]), headers=h)
    assert ok.status_code == 201, ok.text
    ok = await client.post(f"{P}/connections", json=connection_body(dev["id"], pdu["id"]), headers=h)
    assert ok.status_code == 201, ok.text
    # cross-site upstream
    bad = await client.post(f"{P}/connections", json=connection_body(ups_b, dev["id"], feed_label="B"), headers=h)
    assert bad.status_code == 422 and "different site" in bad.text
    # self link and cycle
    assert (await client.post(f"{P}/connections", json=connection_body(dev["id"], dev["id"]), headers=h)).status_code in (409, 422)
    cyc = await client.post(f"{P}/connections", json=connection_body(pdu["id"], ups_a), headers=h)
    assert cyc.status_code == 422
    # the device lists its neighbours
    got = (await client.get(f"{P}/protection-devices/{dev['id']}", headers=h)).json()
    assert got["upstream_node_ids"] == [ups_a] and got["downstream_node_ids"] == [pdu["id"]]


@pytest.mark.asyncio
async def test_unsafe_phase_and_rating_combinations(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    ups_a, ups_a_asset = await make_ups(client, h, room_a)
    dev = (await client.post(f"{P}/protection-devices", json=device_body(ups_a_asset, site_a), headers=h)).json()
    over = await client.post(f"{P}/connections", json=connection_body(ups_a, dev["id"], rated_current_a=100), headers=h)
    assert over.status_code == 422 and "exceeds" in over.text
    phase = await client.post(f"{P}/connections", json=connection_body(ups_a, dev["id"], phase="three"), headers=h)
    assert phase.status_code == 422
    conn = await client.post(f"{P}/connections", json=connection_body(ups_a, dev["id"], rated_current_a=40, phase="single"), headers=h)
    assert conn.status_code == 201
    # lowering the rating below an attached connection is refused
    low = await client.patch(f"{P}/protection-devices/{dev['id']}", json={"rating_a": 16}, headers={**h, "If-Match": "1"})
    assert low.status_code == 422
    # raising connection current beyond the rating is refused on update
    upd = await client.patch(
        f"{P}/connections/{conn.json()['id']}", json={"rated_current_a": 90}, headers={**h, "If-Match": "1"}
    )
    assert upd.status_code == 422


@pytest.mark.asyncio
async def test_state_changes_versioned_audited_and_unknown_kept(client, auth_headers, db_session):
    h = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, h, room_a)
    dev = (await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_a), headers=h)).json()
    no_header = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "open"}, headers=h)
    assert no_header.status_code == 428
    r = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "tripped"}, headers={**h, "If-Match": "1"})
    assert r.status_code == 200 and r.json()["state"] == "tripped" and r.json()["version"] == 2
    stale = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "closed"}, headers={**h, "If-Match": "1"})
    assert stale.status_code == 409
    r = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "unknown"}, headers={**h, "If-Match": "2"})
    assert r.json()["state"] == "unknown"
    bad = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "melted"}, headers={**h, "If-Match": "3"})
    assert bad.status_code == 422
    audit = (
        await db_session.execute(
            text("select count(*) from audit_log where action = 'power.protection_device.state'")
        )
    ).scalar_one()
    assert audit == 2
    outbox = (
        await db_session.execute(text("select count(*) from outbox_event where event_type = 'ProtectionDeviceStateChanged'"))
    ).scalar_one()
    assert outbox == 2


@pytest.mark.asyncio
async def test_retired_device_cannot_change_state(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, h, room_a)
    dev = (await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_a), headers=h)).json()
    assert (await client.post(f"{P}/nodes/{dev['id']}/retire", headers=h)).status_code == 200
    r = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "open"}, headers={**h, "If-Match": "1"})
    assert r.status_code == 422
    assert (await client.get(f"{P}/protection-devices", headers=h)).json()["total"] == 0
    assert (await client.get(f"{P}/protection-devices", params={"include_retired": "true"}, headers=h)).json()["total"] == 1


@pytest.mark.asyncio
async def test_permissions_and_unknown_ids(client, auth_headers):
    mgr = await auth_headers("DCIM Manager")
    room_a, site_a = await create_room_and_site(client, auth_headers)
    _, ups_asset = await make_ups(client, mgr, room_a)
    dev = (await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_a), headers=mgr)).json()
    viewer = await auth_headers("Viewer")
    assert (await client.get(f"{P}/protection-devices/{dev['id']}", headers=viewer)).status_code == 200
    w = await client.post(f"{P}/protection-devices", json=device_body(ups_asset, site_a), headers=viewer)
    assert w.status_code == 403
    s = await client.post(f"{P}/protection-devices/{dev['id']}/state", json={"state": "open"}, headers={**viewer, "If-Match": "1"})
    assert s.status_code == 403
    assert (await client.get(f"{P}/protection-devices/{uuid.uuid4()}", headers=mgr)).status_code == 404
    anon = await client.get(f"{P}/protection-devices")
    assert anon.status_code == 401
