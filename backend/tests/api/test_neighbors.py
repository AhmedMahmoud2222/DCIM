"""LLDP/CDP neighbor evidence: idempotent ingestion, provenance, deterministic reconciliation,
ambiguity/contradiction handling, stale handling and operator decisions."""

import uuid

from sqlalchemy import text

from tests.api._network_helpers import collector_with_integration, ingest_records, signed_post
from tests.api._network_inventory import (
    list_neighbors,
    make_device,
    neighbor_record,
    observing_device,
    scan_marker,
    set_mac,
)

NEIGHBORS = "/api/v1/discovery/neighbors"


async def scenario(client, headers, db_session, *, remote_ports=("Eth1/24", "Eth1/23")):
    local = await make_device(client, headers, ["Eth1/1", "Eth1/2"], hostname="edge-sw-1", ip_address="192.0.2.50")
    remote = await make_device(client, headers, list(remote_ports), hostname="core-sw-1", ip_address="10.0.0.1")
    await set_mac(db_session, remote["id"], "00:50:56:3a:1b:2c")
    collector, integration = await observing_device(client, headers, local)
    return local, remote, collector, integration


def decide(version: int) -> dict:
    return {"If-Match": str(version)}


async def test_repeated_polls_reconcile_idempotently_and_keep_provenance(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    for minute in range(5):
        out = await ingest_records(client, collector, [
            neighbor_record(integration["id"], occurred_at=f"2026-01-01T00:0{minute}:00Z", scan_id=f"scan-{minute}")
        ])
        assert out["results"][0]["status"] == "accepted"
    rows = await list_neighbors(client, headers)
    assert len(rows) == 1
    row = rows[0]
    assert row["first_seen_at"].startswith("2026-01-01T00:00:00") and row["last_seen_at"].startswith("2026-01-01T00:04:00")
    assert row["source_collector_id"] == collector["id"] and row["integration_id"] == integration["id"]
    assert row["scan_id"] == "scan-4" and row["protocol"] == "lldp" and row["capabilities"] == ["bridge"]
    assert row["raw_evidence"] == {"sys_name": "core-sw-1"}
    assert row["reconciliation_state"] == "unmatched"  # evidence kept even with no authoritative match
    assert row["local_port_id"] is None and row["remote_port_id"] is None


async def test_older_observations_never_overwrite_newer_ones(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2026-01-01T01:00:00Z", system_name="new-name")])
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2026-01-01T00:00:00Z", system_name="old-name")])
    (row,) = await list_neighbors(client, headers)
    assert row["remote_system_name"] == "new-name"
    assert row["last_seen_at"].startswith("2026-01-01T01:00:00") and row["first_seen_at"].startswith("2026-01-01T00:00:00")


async def test_future_collector_clock_cannot_pin_last_seen(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2099-01-01T00:00:00Z")])
    (row,) = await list_neighbors(client, headers)
    assert not row["last_seen_at"].startswith("2099")


async def test_protocols_and_ports_are_separate_evidence_rows(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [
        neighbor_record(integration["id"], protocol="lldp"),
        neighbor_record(integration["id"], protocol="cdp"),
        neighbor_record(integration["id"], local="Eth1/2"),
    ])
    assert len(await list_neighbors(client, headers)) == 3
    assert len(await list_neighbors(client, headers, protocol="cdp")) == 1


async def test_invalid_records_are_permanently_rejected_without_failing_the_batch(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    bad = neighbor_record(integration["id"], chassis="x")
    bad["raw_attributes"]["neighbor"]["remote"]["chassis_id"] = "   "
    junk = neighbor_record(integration["id"])
    junk["raw_attributes"] = {"scan_id": "s", "neighbor": "garbage"}
    marker = scan_marker(integration["id"], started="not-a-date", finished="2026-01-01T00:00:00Z")
    good = neighbor_record(integration["id"], chassis="good-sw")
    out = await ingest_records(client, collector, [bad, junk, marker, good])
    codes = [(r["status"], r.get("error_code")) for r in out["results"]]
    assert codes == [("rejected", "INVALID_PAYLOAD")] * 3 + [("accepted", None)]
    assert all("good-sw" not in (r.get("error") or "") for r in out["results"])
    assert [n["remote_chassis_ident"] for n in await list_neighbors(client, headers)] == ["good-sw"]


async def test_collector_cannot_report_neighbors_for_an_integration_it_is_not_assigned(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, _mine = await collector_with_integration(client, headers)
    _other_collector, theirs = await collector_with_integration(client, headers)
    out = await ingest_records(client, collector, [neighbor_record(theirs["id"])])
    assert out["results"][0]["error_code"] == "NOT_ASSIGNED"
    assert await list_neighbors(client, headers) == []


async def test_neighbor_ingest_requires_collector_signature(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    forged = dict(collector, secret="not-the-secret-not-the-secret-0000")
    response = await signed_post(client, forged, "ingest", {"batch_id": "b", "records": [neighbor_record(integration["id"])]})
    assert response.status_code == 401
    assert await list_neighbors(client, headers) == []


async def test_unreconciled_observer_leaves_evidence_unmatched(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"])])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "unmatched"
    assert "not reconciled" in row["match_evidence"]["local"]["reason"]


async def test_deterministic_match_is_proposed_then_confirmed_by_an_operator(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", system_name="core-sw-1", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "proposed", row["match_evidence"]
    assert row["match_evidence"]["proposal"] == {
        "local_port_id": local["port_by_name"]["Eth1/1"], "remote_port_id": remote["port_by_name"]["Eth1/24"]}
    assert row["match_evidence"]["remote_device"]["agreeing_tiers"] == ["chassis_mac", "management_address"]
    assert row["local_port_id"] is None  # a proposal never writes authoritative links

    confirmed = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={"reason": "verified on site"}, headers=headers | decide(row["version"]))
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["reconciliation_state"] == "confirmed" and body["version"] == row["version"] + 1
    assert body["local_port_id"] == local["port_by_name"]["Eth1/1"] and body["remote_port_id"] == remote["port_by_name"]["Eth1/24"]
    assert body["match_evidence"]["decision"] == {"override": False}
    again = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={}, headers=headers | decide(body["version"]))
    assert again.status_code == 409
    audit = (await db_session.execute(text("SELECT action FROM audit_log WHERE entity_type = 'discovered_neighbor'"))).scalars().all()
    assert "neighbor.confirm" in audit


async def test_reobserving_a_confirmed_neighbor_never_changes_the_decision(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    record = neighbor_record(integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")
    await ingest_records(client, collector, [record])
    (row,) = await list_neighbors(client, headers)
    await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={}, headers=headers | decide(row["version"]))
    # the remote reports a different description later; the confirmed links must not move
    again = neighbor_record(integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.99",
                            occurred_at="2026-01-02T00:00:00Z")
    await ingest_records(client, collector, [again])
    (after,) = await list_neighbors(client, headers)
    assert after["reconciliation_state"] == "confirmed"
    assert after["local_port_id"] == local["port_by_name"]["Eth1/1"] and after["remote_port_id"] == remote["port_by_name"]["Eth1/24"]
    assert after["remote_management_address"] == "10.0.0.99"  # evidence is refreshed, decision is not


async def test_name_only_evidence_is_not_enough_to_propose(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(integration["id"], chassis="core-sw-1", port="Eth1/24")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "unmatched"
    assert row["match_evidence"]["remote_device"]["suggested_equipment_id"] == remote["id"]
    assert "proposal" not in row["match_evidence"]


async def test_duplicate_inventory_addresses_make_the_match_ambiguous_not_arbitrary(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await make_device(client, headers, ["Eth1/24"], hostname="rogue-sw", ip_address="10.0.0.1")
    await ingest_records(client, collector, [neighbor_record(integration["id"], port="Eth1/24", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "ambiguous"
    assert "share this management address" in row["match_evidence"]["remote_device"]["reason"]
    assert row["local_port_id"] is None


async def test_contradictory_address_and_mac_evidence_is_ambiguous(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    other = await make_device(client, headers, ["Eth1/24"], hostname="other-sw", ip_address="10.0.0.2")
    await set_mac(db_session, other["id"], "aa:bb:cc:00:00:01")
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="aa:bb:cc:00:00:01", port="Eth1/24", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "ambiguous"
    assert "different equipment" in row["match_evidence"]["remote_device"]["reason"]


async def test_system_name_contradicting_a_strong_match_is_ambiguous(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await make_device(client, headers, ["Eth1/24"], hostname="liar-sw")
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="liar-sw", port="Eth1/24", system_name="liar-sw", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "ambiguous"


async def test_unknown_remote_port_leaves_the_neighbor_unmatched(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(integration["id"], port="Eth9/99", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "unmatched" and row["match_evidence"]["remote_port"]["state"] == "unmatched"


async def test_two_protocols_disagreeing_about_one_port_are_both_ambiguous(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await make_device(client, headers, ["Eth0/1"], hostname="dist-sw-9", ip_address="10.0.0.9")
    await ingest_records(client, collector, [
        neighbor_record(integration["id"], protocol="lldp", port="Eth1/24", management_address="10.0.0.1"),
        neighbor_record(integration["id"], protocol="cdp", chassis="dist-sw-9", port="Eth0/1", management_address="10.0.0.9"),
    ])
    rows = {n["protocol"]: n for n in await list_neighbors(client, headers)}
    # the second ingest sees the first; the first is re-evaluated when asked
    assert rows["cdp"]["reconciliation_state"] == "ambiguous"
    assert rows["cdp"]["match_evidence"]["protocol_disagreement"][0]["protocol"] == "lldp"
    rematched = await client.post(f"{NEIGHBORS}/{rows['lldp']['id']}/rematch", headers=headers)
    assert rematched.status_code == 200 and rematched.json()["reconciliation_state"] == "ambiguous"


async def test_protocols_agreeing_about_one_port_are_not_a_disagreement(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [
        neighbor_record(integration["id"], protocol="lldp", chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1"),
        neighbor_record(integration["id"], protocol="cdp", chassis="core-sw-1", port="Eth1/24", management_address="10.0.0.1"),
    ])
    assert {n["reconciliation_state"] for n in await list_neighbors(client, headers)} == {"proposed"}


async def test_confirming_a_contradicting_neighbor_is_refused_and_leaves_authority_untouched(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")])
    (first,) = await list_neighbors(client, headers)
    confirmed = (await client.post(f"{NEIGHBORS}/{first['id']}/confirm", json={}, headers=headers | decide(first["version"]))).json()

    # Same local port now reported against a *different* remote port.
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], protocol="cdp", chassis="core-sw-1", port="Eth1/23", management_address="10.0.0.1")])
    second = next(n for n in await list_neighbors(client, headers) if n["protocol"] == "cdp")
    assert second["reconciliation_state"] == "conflict"
    assert second["match_evidence"]["conflicts"][0]["kind"] == "confirmed_neighbor"
    refused = await client.post(f"{NEIGHBORS}/{second['id']}/confirm", json={}, headers=headers | decide(second["version"]))
    assert refused.status_code == 409
    explicit = await client.post(
        f"{NEIGHBORS}/{second['id']}/confirm", json={"local_port_id": local["port_by_name"]["Eth1/1"],
                                                      "remote_port_id": remote["port_by_name"]["Eth1/23"]},
        headers=headers | decide(second["version"]))
    assert explicit.status_code == 409  # an override cannot bypass the conflict check either
    after = (await client.get(f"{NEIGHBORS}/{first['id']}", headers=headers)).json()
    assert after["reconciliation_state"] == "confirmed" and after["version"] == confirmed["version"]
    assert after["remote_port_id"] == remote["port_by_name"]["Eth1/24"]


async def test_operator_can_resolve_an_unmatched_neighbor_with_explicit_ports(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(integration["id"], chassis="mystery-box", port="uplink0")])
    (row,) = await list_neighbors(client, headers)
    assert row["reconciliation_state"] == "unmatched"
    missing = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={}, headers=headers | decide(row["version"]))
    assert missing.status_code == 422
    same = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={
        "local_port_id": local["port_by_name"]["Eth1/1"], "remote_port_id": local["port_by_name"]["Eth1/1"]},
        headers=headers | decide(row["version"]))
    assert same.status_code == 422
    unknown = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={
        "local_port_id": local["port_by_name"]["Eth1/1"], "remote_port_id": str(uuid.uuid4())}, headers=headers | decide(row["version"]))
    assert unknown.status_code == 404
    ok = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={
        "local_port_id": local["port_by_name"]["Eth1/1"], "remote_port_id": remote["port_by_name"]["Eth1/23"], "reason": "traced by hand"},
        headers=headers | decide(row["version"]))
    assert ok.status_code == 200 and ok.json()["match_evidence"]["decision"] == {"override": True}


async def test_reject_is_sticky_and_revoke_reopens(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    record = neighbor_record(integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")
    await ingest_records(client, collector, [record])
    (row,) = await list_neighbors(client, headers)
    rejected = (await client.post(f"{NEIGHBORS}/{row['id']}/reject", json={"reason": "wrong patch"}, headers=headers | decide(row["version"]))).json()
    assert rejected["reconciliation_state"] == "rejected"
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1", occurred_at="2026-02-01T00:00:00Z")])
    (still,) = await list_neighbors(client, headers)
    assert still["reconciliation_state"] == "rejected" and still["last_seen_at"].startswith("2026-02-01")
    confirm_rejected = await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={}, headers=headers | decide(still["version"]))
    assert confirm_rejected.status_code == 409
    reopened = (await client.post(f"{NEIGHBORS}/{row['id']}/revoke", json={}, headers=headers | decide(still["version"]))).json()
    assert reopened["reconciliation_state"] == "proposed" and reopened["local_port_id"] is None


async def test_revoke_clears_a_confirmation(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")])
    (row,) = await list_neighbors(client, headers)
    confirmed = (await client.post(f"{NEIGHBORS}/{row['id']}/confirm", json={}, headers=headers | decide(row["version"]))).json()
    assert (await client.post(f"{NEIGHBORS}/{row['id']}/reject", json={}, headers=headers | decide(confirmed["version"]))).status_code == 409
    revoked = (await client.post(f"{NEIGHBORS}/{row['id']}/revoke", json={"reason": "moved"}, headers=headers | decide(confirmed["version"]))).json()
    assert revoked["local_port_id"] is None and revoked["remote_port_id"] is None and revoked["reconciliation_state"] == "proposed"


async def test_completed_scan_marks_unseen_neighbors_stale_without_touching_decisions(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    local, remote, collector, integration = await scenario(client, headers, db_session)
    await ingest_records(client, collector, [
        neighbor_record(integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1",
                        occurred_at="2026-01-01T00:00:00Z"),
        neighbor_record(integration["id"], local="Eth1/2", chassis="other", port="p", occurred_at="2026-01-01T00:00:00Z"),
        neighbor_record(integration["id"], protocol="cdp", chassis="cdp-peer", port="p", occurred_at="2026-01-01T00:00:00Z"),
    ])
    confirmed_row = next(n for n in await list_neighbors(client, headers) if n["remote_chassis_ident"] == "00:50:56:3a:1b:2c")
    await client.post(f"{NEIGHBORS}/{confirmed_row['id']}/confirm", json={}, headers=headers | decide(confirmed_row["version"]))

    incomplete = await ingest_records(client, collector, [scan_marker(integration["id"], started="2026-01-01T01:00:00Z", complete=False)])
    assert incomplete["results"][0]["status"] == "accepted"
    assert {n["status"] for n in await list_neighbors(client, headers)} == {"active"}  # a partial walk proves nothing

    await ingest_records(client, collector, [scan_marker(integration["id"], started="2025-12-31T00:00:00Z")])
    assert {n["status"] for n in await list_neighbors(client, headers)} == {"active"}  # marker older than every observation

    await ingest_records(client, collector, [scan_marker(integration["id"], protocol="lldp", started="2026-01-01T01:00:00Z")])
    by_protocol = {(n["protocol"], n["remote_chassis_ident"]): n for n in await list_neighbors(client, headers)}
    assert by_protocol[("lldp", "00:50:56:3a:1b:2c")]["status"] == "stale"
    assert by_protocol[("lldp", "other")]["status"] == "stale"
    assert by_protocol[("cdp", "cdp-peer")]["status"] == "active"  # other protocol untouched
    kept = by_protocol[("lldp", "00:50:56:3a:1b:2c")]
    assert kept["reconciliation_state"] == "confirmed" and kept["effective_status"] == "stale"
    assert kept["local_port_id"] == local["port_by_name"]["Eth1/1"]

    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1", occurred_at="2026-01-01T02:00:00Z")])
    revived = next(n for n in await list_neighbors(client, headers) if n["remote_chassis_ident"] == "00:50:56:3a:1b:2c")
    assert revived["status"] == "active" and revived["reconciliation_state"] == "confirmed"


async def test_neighbor_unseen_for_three_poll_intervals_reads_as_stale(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)  # default poll interval: 300s -> 15 min floor
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2026-01-01T00:00:00Z")])
    (row,) = await list_neighbors(client, headers)
    assert row["status"] == "active" and row["effective_status"] == "stale"  # 2026-01-01 is long past the stale window


async def test_decision_endpoints_require_a_version_and_reject_stale_ones(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"])])
    (row,) = await list_neighbors(client, headers)
    assert (await client.post(f"{NEIGHBORS}/{row['id']}/reject", json={}, headers=headers)).status_code == 428
    assert (await client.post(f"{NEIGHBORS}/{row['id']}/reject", json={}, headers=headers | decide(99))).status_code == 409
    assert (await client.post(f"{NEIGHBORS}/{uuid.uuid4()}/reject", json={}, headers=headers | decide(1))).status_code == 404
    assert (await client.get(f"{NEIGHBORS}/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert (await client.post(f"{NEIGHBORS}/{uuid.uuid4()}/rematch", headers=headers)).status_code == 404


async def test_rbac_for_neighbor_review(client, auth_headers):
    admin = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, admin)
    await ingest_records(client, collector, [neighbor_record(integration["id"])])
    (row,) = await list_neighbors(client, admin)
    for role, may_decide in (("Administrator", True), ("DCIM Manager", True), ("Engineer", True), ("Operator", False), ("Viewer", False)):
        headers = await auth_headers(role)
        assert (await client.get(NEIGHBORS, headers=headers)).status_code == 200, role
        assert (await client.get(f"{NEIGHBORS}/{row['id']}", headers=headers)).status_code == 200, role
        verdict = await client.post(f"{NEIGHBORS}/{row['id']}/rematch", headers=headers)
        assert verdict.status_code == (200 if may_decide else 403), (role, verdict.status_code)
        for action in ("confirm", "reject", "revoke"):
            response = await client.post(f"{NEIGHBORS}/{row['id']}/{action}", json={}, headers=headers | decide(99))
            assert response.status_code in ((409, 422) if may_decide else (403,)), (role, action, response.status_code)
    anonymous = await client.get(NEIGHBORS)
    assert anonymous.status_code == 401
