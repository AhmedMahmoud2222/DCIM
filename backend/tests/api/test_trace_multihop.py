"""Issue #101 B5: end-to-end trace through patch panels via authoritative pass-through relationships."""

import pytest
from sqlalchemy import text

from app.application.network import trace_service
from tests.api import _pass_through_helpers as h
from tests.api._cable_helpers import if_match


async def get_trace(client, headers, port_id: str) -> dict:
    response = await client.get(h.trace_url(port_id), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_switch_to_switch_through_one_patch_panel_is_traced_in_order_both_ways(client, auth_headers):
    headers = await auth_headers("Administrator")
    chain = await h.build_chain(client, headers, panels=1)
    forward = await get_trace(client, headers, chain["sw_a"]["port_by_name"]["Eth1/1"])
    assert forward["format"] == "trace.v2" and forward["terminated"] == "end_of_path" and forward["hop_count"] == 2
    assert h.hops(forward) == [("chain-pp1", "Front01"), ("chain-swb", "Eth1/24")]
    first, second = forward["path"]
    assert first["link"]["kind"] == "cable" and first["link"]["cable"]["label"] == "chain-C0"
    assert first["pass_through"]["to"]["port_name"] == "Rear01" and first["pass_through"]["to"]["equipment_hostname"] == "chain-pp1"
    assert first["pass_through"]["label"] == "chain-PT0"
    assert second["link"]["cable"]["label"] == "chain-C1" and second["pass_through"] is None

    reverse = await get_trace(client, headers, chain["sw_b"]["port_by_name"]["Eth1/24"])
    assert reverse["terminated"] == "end_of_path"
    assert h.hops(reverse) == [("chain-pp1", "Rear01"), ("chain-swa", "Eth1/1")]
    assert reverse["path"][0]["pass_through"]["to"]["port_name"] == "Front01"
    assert [p["link"]["cable"]["label"] for p in reverse["path"]] == ["chain-C1", "chain-C0"]


@pytest.mark.parametrize("panels", [2, 5])
async def test_path_length_is_not_hardcoded_to_one_patch_panel(client, auth_headers, panels):
    headers = await auth_headers("Administrator")
    chain = await h.build_chain(client, headers, panels=panels, prefix=f"n{panels}")
    trace = await get_trace(client, headers, chain["sw_a"]["port_by_name"]["Eth1/1"])
    expected = [x for i in range(1, panels + 1) for x in ((f"n{panels}-pp{i}", "Front01"),)]
    assert trace["terminated"] == "end_of_path" and trace["hop_count"] == panels + 1
    arrivals = h.hops(trace)
    assert arrivals[:-1] == expected and arrivals[-1] == (f"n{panels}-swb", "Eth1/24")
    assert [p["pass_through"] is not None for p in trace["path"]] == [True] * panels + [False]
    back = await get_trace(client, headers, chain["sw_b"]["port_by_name"]["Eth1/24"])
    assert back["hop_count"] == panels + 1 and h.hops(back)[-1] == (f"n{panels}-swa", "Eth1/1")


async def test_planned_cables_are_followed_and_reported_as_planned(client, auth_headers):
    headers = await auth_headers("Administrator")
    sw_a, sw_b = await h.switch(client, headers, "pl-a"), await h.switch(client, headers, "pl-b")
    pp = await h.patch_panel(client, headers, "pl-pp")
    await h.link(client, headers, sw_a["port_by_name"]["Eth1/1"], pp["port_by_name"]["Front01"], label="PL-1", status="planned")
    await h.pass_through(client, headers, pp["port_by_name"]["Front01"], pp["port_by_name"]["Rear01"])
    await h.link(client, headers, pp["port_by_name"]["Rear01"], sw_b["port_by_name"]["Eth1/24"], label="PL-2", status="installed")
    trace = await get_trace(client, headers, sw_a["port_by_name"]["Eth1/1"])
    assert [p["link"]["cable"]["status"] for p in trace["path"]] == ["planned", "installed"]
    assert trace["terminated"] == "end_of_path"


async def test_a_path_that_ends_inside_a_panel_is_no_link_not_end_of_path(client, auth_headers):
    headers = await auth_headers("Administrator")
    sw_a = await h.switch(client, headers, "open-a")
    pp = await h.patch_panel(client, headers, "open-pp")
    await h.link(client, headers, sw_a["port_by_name"]["Eth1/1"], pp["port_by_name"]["Front01"], label="OPEN-1")
    await h.pass_through(client, headers, pp["port_by_name"]["Front01"], pp["port_by_name"]["Rear01"])
    trace = await get_trace(client, headers, sw_a["port_by_name"]["Eth1/1"])
    assert trace["terminated"] == "no_link" and trace["hop_count"] == 1
    assert trace["path"][0]["pass_through"]["to"]["port_name"] == "Rear01"  # the signal reaches the rear, then nothing is connected
    lone = await get_trace(client, headers, sw_a["port_by_name"]["Eth1/24"])
    assert lone["terminated"] == "no_link" and lone["path"] == []


async def test_a_removed_cable_is_not_followed_but_stays_in_history(client, auth_headers):
    headers = await auth_headers("Administrator")
    chain = await h.build_chain(client, headers, panels=1, prefix="rm")
    second = chain["cables"][1]
    removed = await client.post(f"/api/v1/cables/{second['id']}/remove", json={}, headers=headers | if_match(second["version"]))
    assert removed.status_code == 200
    trace = await get_trace(client, headers, chain["sw_a"]["port_by_name"]["Eth1/1"])
    assert trace["terminated"] == "no_link" and [p["link"]["cable"]["label"] for p in trace["path"]] == ["rm-C0"]
    assert "rm-C1" not in str(trace["path"])
    # history is reported separately, for the port the cable was attached to
    far = await get_trace(client, headers, chain["sw_b"]["port_by_name"]["Eth1/24"])
    assert far["path"] == [] and far["terminated"] == "no_link"
    assert [c["label"] for c in far["previous_cables"]] == ["rm-C1"]


async def test_a_loop_terminates_with_a_cycle_indication_inside_the_bound(client, auth_headers):
    headers = await auth_headers("Administrator")
    a = await h.patch_panel(client, headers, "loop-a")
    b = await h.patch_panel(client, headers, "loop-b")
    pa, pb = a["port_by_name"], b["port_by_name"]
    await h.pass_through(client, headers, pa["Front01"], pa["Rear01"])
    await h.pass_through(client, headers, pb["Front01"], pb["Rear01"])
    await h.link(client, headers, pa["Front01"], pb["Rear01"], label="LOOP-1")
    await h.link(client, headers, pb["Front01"], pa["Rear01"], label="LOOP-2")
    trace = await get_trace(client, headers, pa["Front01"])
    assert trace["terminated"] == "cycle_detected"
    assert trace["hop_count"] <= trace["max_hops"] and trace["hop_count"] == 2
    closing = trace["path"][-1]["pass_through"]["to"]  # the signal would re-enter the port the trace started from
    assert (closing["equipment_hostname"], closing["port_name"]) == ("loop-a", "Front01")
    again = await get_trace(client, headers, pa["Front01"])
    assert again["path"] == trace["path"]  # deterministic


async def test_a_cable_between_the_two_ports_of_one_pass_through_is_a_cycle(client, auth_headers):
    headers = await auth_headers("Administrator")
    pp = await h.patch_panel(client, headers, "self-pp")
    ports = pp["port_by_name"]
    await h.pass_through(client, headers, ports["Front01"], ports["Rear01"])
    await h.link(client, headers, ports["Front01"], ports["Rear01"], label="SELF-1")
    trace = await get_trace(client, headers, ports["Front01"])
    assert trace["terminated"] == "cycle_detected" and trace["hop_count"] == 1


async def test_the_hop_bound_stops_a_long_path(client, auth_headers, monkeypatch):
    headers = await auth_headers("Administrator")
    chain = await h.build_chain(client, headers, panels=3, prefix="hb")
    monkeypatch.setattr(trace_service, "MAX_HOPS", 2)
    trace = await get_trace(client, headers, chain["sw_a"]["port_by_name"]["Eth1/1"])
    assert trace["terminated"] == "hop_limit" and trace["hop_count"] == 2 and trace["max_hops"] == 2


async def test_inconsistent_topology_ends_safely_as_broken_topology(client, auth_headers, db_session, _admin_engine):
    headers = await auth_headers("Administrator")
    # (a) a pass-through that lost one member, (b) a cable that lost an endpoint, (c) two logical connections on one port
    chain = await h.build_chain(client, headers, panels=1, prefix="bk")
    member = (await db_session.execute(text(
        "SELECT id FROM port_pass_through_member WHERE equipment_port_id = :p"), {"p": chain["panels"][0]["port_by_name"]["Rear01"]})).scalar_one()
    sw_c, sw_d = await h.switch(client, headers, "bk-c"), await h.switch(client, headers, "bk-d")
    broken_cable = await h.link(client, headers, sw_c["port_by_name"]["Eth1/1"], sw_d["port_by_name"]["Eth1/1"], label="BK-X")
    endpoint = (await db_session.execute(text(
        "SELECT id FROM cable_endpoint WHERE cable_id = :c AND end_label = 'B'"), {"c": broken_cable["id"]})).scalar_one()
    sw_e = await h.switch(client, headers, "bk-e", ports=("Eth1/1", "Eth1/2", "Eth1/3"))
    await db_session.commit()
    async with _admin_engine.begin() as conn:  # bypass the integrity triggers to simulate corruption
        await conn.execute(text("SET session_replication_role = replica"))
        await conn.execute(text("DELETE FROM port_pass_through_member WHERE id = :m"), {"m": member})
        await conn.execute(text("DELETE FROM cable_endpoint WHERE id = :e"), {"e": endpoint})
        await conn.execute(text(
            "INSERT INTO port_connection (id, source_port_id, target_port_id, status) VALUES (gen_random_uuid(), :s1, :t, 'active'), "
            "(gen_random_uuid(), :s2, :t, 'active')"),
            {"s1": sw_e["port_by_name"]["Eth1/1"], "s2": sw_e["port_by_name"]["Eth1/2"], "t": sw_e["port_by_name"]["Eth1/3"]})
    first = await get_trace(client, headers, chain["sw_a"]["port_by_name"]["Eth1/1"])
    assert first["terminated"] == "broken_topology" and first["terminated_reason"] and first["hop_count"] == 1
    second = await get_trace(client, headers, sw_c["port_by_name"]["Eth1/1"])
    assert second["terminated"] == "broken_topology" and second["path"] == []
    third = await get_trace(client, headers, sw_e["port_by_name"]["Eth1/3"])
    assert third["terminated"] == "broken_topology"


async def test_discovery_evidence_is_never_traversed(client, auth_headers):
    from tests.api._network_helpers import ingest_records
    from tests.api._network_inventory import neighbor_record, observing_device

    headers = await auth_headers("Administrator")
    sw_a = await h.switch(client, headers, "ev-a")
    pp = await h.patch_panel(client, headers, "ev-pp")
    collector, integration = await observing_device(client, headers, sw_a)
    await ingest_records(client, collector, [neighbor_record(integration["id"], local="Eth1/1", chassis="ev-pp", port="Front01")])
    await h.pass_through(client, headers, pp["port_by_name"]["Front01"], pp["port_by_name"]["Rear01"])
    trace = await get_trace(client, headers, sw_a["port_by_name"]["Eth1/1"])
    assert trace["path"] == [] and trace["terminated"] == "no_link"  # an unconfirmed adjacency is not a link
