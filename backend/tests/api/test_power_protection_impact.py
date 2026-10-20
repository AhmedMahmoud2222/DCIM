"""Issue #102 area F: failure impact follows the protection topology and stays read-only."""

import pytest
from sqlalchemy import text

from tests.api._phase3_helpers import connection_body, create_room_and_site, create_ups
from tests.api.test_impact import _instantiate, _make_published_equipment_revision

P = "/api/v1/power"


async def dual_fed_server(client, auth_headers):
    h = await auth_headers("Administrator")
    room_id, site_id = await create_room_and_site(client, auth_headers)
    revision = await _make_published_equipment_revision(client, h, port_count=0, psu_quantity=2)
    server = await _instantiate(client, h, revision["id"])
    inlets = server["power_inlets"]
    assert len(inlets) == 2
    chain = {}
    for side, inlet in zip(("A", "B"), inlets, strict=True):
        ups = await create_ups(client, h, room_id)
        brk = (
            await client.post(
                f"{P}/protection-devices",
                json={
                    "housing_asset_id": ups["managed_asset_id"], "site_id": site_id, "label": f"BRK-{side}", "rating_a": 32,
                    "voltage_v": 230, "poles": 1, "phase_config": "single",
                },
                headers=h,
            )
        ).json()
        for src, dst in ((ups["id"], brk["id"]), (brk["id"], inlet["power_node_id"])):
            r = await client.post(f"{P}/connections", json=connection_body(src, dst, feed_label=side), headers=h)
            assert r.status_code == 201, r.text
        chain[side] = {"ups": ups["id"], "brk": brk["id"]}
    return h, server, chain


async def simulate(client, h, node_id):
    r = await client.post("/api/v1/impact/simulate", json={"target_type": "power_node", "target_id": node_id}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_breaker_failure_degrades_dual_fed_server_and_stays_downstream(client, auth_headers):
    h, server, chain = await dual_fed_server(client, auth_headers)
    body = await simulate(client, h, chain["A"]["brk"])
    items = body["directly_impacted"] + body["indirectly_impacted"]
    assert [i["equipment_id"] for i in items] == [server["id"]]
    assert items[0]["impact_type"] == "degraded_redundancy" and body["lost_redundancy_paths"]
    # upstream UPS and the other feed are not part of a downstream failure
    upstream = await simulate(client, h, chain["B"]["brk"])
    assert [i["impact_type"] for i in upstream["directly_impacted"] + upstream["indirectly_impacted"]] == ["degraded_redundancy"]
    ups_fail = await simulate(client, h, chain["A"]["ups"])
    assert [i["impact_type"] for i in ups_fail["directly_impacted"] + ups_fail["indirectly_impacted"]] == ["degraded_redundancy"]


@pytest.mark.asyncio
async def test_other_feed_already_tripped_turns_degraded_into_outage(client, auth_headers):
    h, server, chain = await dual_fed_server(client, auth_headers)
    r = await client.post(
        f"{P}/protection-devices/{chain['B']['brk']}/state", json={"state": "tripped"}, headers={**h, "If-Match": "1"}
    )
    assert r.status_code == 200
    body = await simulate(client, h, chain["A"]["brk"])
    item = (body["directly_impacted"] + body["indirectly_impacted"])[0]
    assert item["impact_type"] == "power_loss" and "BRK-B" in item["message"]
    assert body["lost_redundancy_paths"] == []


@pytest.mark.asyncio
async def test_unknown_state_is_not_treated_as_a_cut(client, auth_headers):
    h, server, chain = await dual_fed_server(client, auth_headers)
    await client.post(
        f"{P}/protection-devices/{chain['B']['brk']}/state", json={"state": "unknown"}, headers={**h, "If-Match": "1"}
    )
    body = await simulate(client, h, chain["A"]["brk"])
    assert (body["directly_impacted"] + body["indirectly_impacted"])[0]["impact_type"] == "degraded_redundancy"


@pytest.mark.asyncio
async def test_simulation_writes_nothing(client, auth_headers, db_session):
    h, server, chain = await dual_fed_server(client, auth_headers)
    tables = ("power_connection", "protection_device", "power_node", "audit_log", "outbox_event")
    sql = " union all ".join(f"select '{t}', count(*), coalesce(md5(string_agg(x::text, '|' order by x::text)), '') from {t} x" for t in tables)
    before = (await db_session.execute(text(sql))).all()
    await simulate(client, h, chain["A"]["brk"])
    await db_session.rollback()
    after = (await db_session.execute(text(sql))).all()
    audit_delta = {a[0]: a[1] - b[1] for a, b in zip(after, before, strict=True)}
    assert audit_delta["power_connection"] == audit_delta["protection_device"] == audit_delta["power_node"] == audit_delta["outbox_event"] == 0
    assert [a for a in after if a[0] != "audit_log"] == [b for b in before if b[0] != "audit_log"]
