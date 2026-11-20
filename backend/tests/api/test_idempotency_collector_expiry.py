"""DB-03 (#357): collector ingestion behaviour once a ledger row has expired (matrix T29-T33, T36, T39),
through the real signed `/collectors/{id}/ingest` API against PostgreSQL.

With DISC-ORDER-01 merged every collector record type is intrinsically idempotent and order-safe, so a
post-expiry replay of an old record is acknowledged `accepted` but changes nothing. The 72 h collector TTL is
therefore a volume dial, not a correctness requirement, which is why the replay is safe even if it ever
outlives the ledger row."""

import ast
import pathlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.api.v1 import collectors as collectors_module
from app.application import idempotency as idem
from app.core.config import get_settings
from tests.api._network_helpers import collector_with_integration, ingest_records
from tests.api._network_inventory import list_neighbors, neighbor_record, scan_marker


async def _expire(db_session, dedup_key: str) -> None:
    await db_session.execute(
        text("UPDATE idempotency_key SET expires_at = now() - interval '1 second' WHERE key LIKE :k"),
        {"k": f"%:{dedup_key}"},
    )
    await db_session.commit()


def _device(integration_id, external_identifier, occurred_at, **raw):
    return {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration_id, "external_identifier": external_identifier,
        "occurred_at": occurred_at, "raw_attributes": raw,
    }


async def _device_state(db_session, external_identifier):
    return dict((await db_session.execute(text(
        "SELECT last_seen_at, raw_attributes, status FROM discovered_device WHERE external_identifier = :e"),
        {"e": external_identifier})).mappings().one())


async def _row_counts(db_session):
    return tuple((await db_session.execute(text(
        "SELECT (SELECT count(*) FROM discovered_device), (SELECT count(*) FROM reconciliation_diff), "
        "(SELECT count(*) FROM outbox_event WHERE event_type IN ('DeviceDiscovered', 'ReconciliationRequired'))"
    ))).one())


async def test_t31_expired_replay_of_an_older_device_record_is_accepted_but_changes_nothing(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    older = _device(integration["id"], "10.20.0.1", "2026-03-01T11:00:00Z", v="older")
    newer = _device(integration["id"], "10.20.0.1", "2026-03-01T12:00:00Z", v="newer")
    assert (await ingest_records(client, collector, [older]))["results"][0]["status"] == "accepted"
    assert (await ingest_records(client, collector, [newer]))["results"][0]["status"] == "accepted"
    await db_session.rollback()
    before, counts = await _device_state(db_session, "10.20.0.1"), await _row_counts(db_session)
    assert before["raw_attributes"]["v"] == "newer"

    await _expire(db_session, older["dedup_key"])
    replay = await ingest_records(client, collector, [older])
    assert replay["results"][0]["status"] == "accepted"  # re-executed (not "duplicate"), and then a no-op
    await db_session.rollback()
    assert await _device_state(db_session, "10.20.0.1") == before  # no regression of attributes or last_seen_at
    assert await _row_counts(db_session) == counts  # no new device, diff or outbox events


async def test_t29_expired_replay_of_an_older_neighbor_observation_does_not_regress_evidence(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    old = neighbor_record(integration["id"], occurred_at="2026-01-01T01:00:00Z", system_name="old-name")
    new = neighbor_record(integration["id"], occurred_at="2026-01-01T02:00:00Z", system_name="new-name")
    await ingest_records(client, collector, [old])
    await ingest_records(client, collector, [new])
    (before,) = await list_neighbors(client, headers)

    await _expire(db_session, old["dedup_key"])
    assert (await ingest_records(client, collector, [old]))["results"][0]["status"] == "accepted"
    (after,) = await list_neighbors(client, headers)
    assert after["remote_system_name"] == "new-name"
    assert after["last_seen_at"] == before["last_seen_at"]


async def test_t30_expired_replay_of_a_scan_marker_changes_nothing_beyond_the_first_application(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2026-01-01T00:00:00Z")])
    marker = scan_marker(integration["id"], started="2026-01-01T01:00:00Z")
    assert (await ingest_records(client, collector, [marker]))["results"][0]["status"] == "accepted"
    assert {n["status"] for n in await list_neighbors(client, headers)} == {"stale"}
    # The neighbor is observed again after the scan: active.
    await ingest_records(client, collector, [neighbor_record(integration["id"], occurred_at="2026-01-01T02:00:00Z")])
    assert {n["status"] for n in await list_neighbors(client, headers)} == {"active"}

    await _expire(db_session, marker["dedup_key"])
    assert (await ingest_records(client, collector, [marker]))["results"][0]["status"] == "accepted"
    assert {n["status"] for n in await list_neighbors(client, headers)} == {"active"}  # old marker cannot re-stale it


async def test_t32_a_mixed_batch_isolates_expired_in_ttl_and_new_records(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    expired = _device(integration["id"], "10.21.0.1", "2026-03-01T10:00:00Z", v="a")
    live = _device(integration["id"], "10.21.0.2", "2026-03-01T10:00:00Z", v="b")
    await ingest_records(client, collector, [expired, live])
    await _expire(db_session, expired["dedup_key"])

    fresh = _device(integration["id"], "10.21.0.3", "2026-03-01T10:00:00Z", v="c")
    out = await ingest_records(client, collector, [expired, live, fresh])
    statuses = {r["dedup_key"]: r["status"] for r in out["results"]}
    assert statuses == {expired["dedup_key"]: "accepted", live["dedup_key"]: "duplicate", fresh["dedup_key"]: "accepted"}


async def test_t32_a_record_still_processing_elsewhere_fails_only_itself(client, auth_headers, db_session, monkeypatch):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 0.2)
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    busy = _device(integration["id"], "10.22.0.1", "2026-03-01T10:00:00Z", v="busy")
    sibling = _device(integration["id"], "10.22.0.2", "2026-03-01T10:00:00Z", v="sibling")
    request_hash = idem.hash_request_body(collectors_module.IngestRecordIn(**busy).model_dump(mode="json"))
    await db_session.execute(
        text(
            "INSERT INTO idempotency_key (id, principal, key, endpoint, request_hash, status, claim_generation,"
            " expires_at, created_at, updated_at) VALUES (gen_random_uuid(), :p, :k, 'collector_ingest', :h,"
            " 'processing', 1, now() + interval '1 hour', now(), now())"
        ),
        {"p": f"collector:{collector['id']}", "k": f"{collector['id']}:{busy['dedup_key']}", "h": request_hash},
    )
    await db_session.commit()

    out = await ingest_records(client, collector, [busy, sibling])
    by_key = {r["dedup_key"]: r for r in out["results"]}
    assert by_key[busy["dedup_key"]]["status"] == "rejected" and by_key[busy["dedup_key"]]["error_code"] == "PROCESSING"
    assert by_key[sibling["dedup_key"]]["status"] == "accepted"


async def test_t33_collector_claims_use_the_collector_ttl_and_user_claims_keep_24_hours(
    client, auth_headers, db_session, monkeypatch
):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    default_record = _device(integration["id"], "10.23.0.1", "2026-03-01T10:00:00Z", v="a")
    await ingest_records(client, collector, [default_record])

    short = get_settings().model_copy(update={"collector_idempotency_ttl_hours": 36})
    monkeypatch.setattr(collectors_module, "get_settings", lambda: short)
    configured_record = _device(integration["id"], "10.23.0.2", "2026-03-01T10:00:00Z", v="b")
    await ingest_records(client, collector, [configured_record])

    user = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "equipment", "asset_tag": f"TTL-{uuid.uuid4().hex[:8]}"},
        headers={**await auth_headers("Engineer"), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert user.status_code == 201

    async def collector_hours(dedup_key: str) -> float:
        stmt = text(
            "SELECT extract(epoch FROM (expires_at - created_at)) / 3600 FROM idempotency_key WHERE key LIKE :k"
        )
        return float((await db_session.execute(stmt, {"k": f"%:{dedup_key}"})).scalar_one())

    user_stmt = text(
        "SELECT extract(epoch FROM (expires_at - created_at)) / 3600 FROM idempotency_key "
        "WHERE endpoint = 'POST:/managed-assets' AND principal LIKE 'user:%' "
        "AND created_at > now() - interval '5 minutes'"
    )
    assert await collector_hours(default_record["dedup_key"]) == pytest.approx(72, abs=0.1)
    assert await collector_hours(configured_record["dedup_key"]) == pytest.approx(36, abs=0.1)
    assert float((await db_session.execute(user_stmt)).scalar_one()) == pytest.approx(24, abs=0.1)
    assert timedelta(hours=get_settings().collector_idempotency_ttl_hours) > timedelta(hours=24, seconds=300)


async def test_t39_an_in_ttl_replay_bypasses_every_downstream_ingestion_path(client, auth_headers, monkeypatch):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers)
    calls = {"device": 0, "neighbor": 0, "scan": 0}

    def spy(name, original):
        async def wrapper(*a, **k):
            calls[name] += 1
            return await original(*a, **k)
        return wrapper

    monkeypatch.setattr(collectors_module, "ingest_discovery", spy("device", collectors_module.ingest_discovery))
    monkeypatch.setattr(collectors_module, "ingest_neighbor", spy("neighbor", collectors_module.ingest_neighbor))
    monkeypatch.setattr(collectors_module, "apply_scan_marker", spy("scan", collectors_module.apply_scan_marker))

    records = [
        _device(integration["id"], "10.24.0.1", "2026-03-01T10:00:00Z", v="a"),
        neighbor_record(integration["id"], occurred_at="2026-03-01T10:00:00Z"),
        scan_marker(integration["id"], started="2026-03-01T09:00:00Z"),
    ]
    first = await ingest_records(client, collector, records)
    assert [r["status"] for r in first["results"]] == ["accepted"] * 3
    assert calls == {"device": 1, "neighbor": 1, "scan": 1}

    second = await ingest_records(client, collector, records)
    assert [r["status"] for r in second["results"]] == ["duplicate"] * 3
    assert calls == {"device": 1, "neighbor": 1, "scan": 1}  # no downstream call on the replay


# --- T36: the natural-key premise behind "safe to re-execute after expiry" -------------------------------------------------


def test_t36_no_route_deletes_or_retags_assets_equipment_racks_or_pdus():
    """The post-expiry 409 contract (docs/implementation/IDEMPOTENCY_EXPIRY.md) relies on `asset_tag` being
    permanent: once a create succeeded, its tag exists forever, so re-executing it can only answer 409. If a
    route is ever added that deletes such an asset or changes its tag, this test fails and forces a re-review
    of that contract (a deleted asset would make a post-expiry retry create a SECOND record)."""
    from app.main import app

    guarded_prefixes = ("/api/v1/managed-assets", "/api/v1/equipment", "/api/v1/racks", "/api/v1/power/pdus")
    deleting = [
        (sorted(route.methods or []), route.path) for route in app.routes
        if getattr(route, "methods", None) and "DELETE" in route.methods and route.path.startswith(guarded_prefixes)
        # Removing a placement (a sub-resource) deletes no asset.
        and "/placement" not in route.path
    ]
    assert deleting == [], f"asset-deleting routes need an explicit idempotency re-review: {deleting}"

    offenders: list[str] = []
    for path in pathlib.Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            offenders += [
                f"{path}:{node.lineno}" for t in targets
                if isinstance(t, ast.Attribute) and t.attr == "asset_tag"
            ]
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values"
                    and any(k.arg == "asset_tag" for k in node.keywords)):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"asset_tag must stay immutable; found writers: {offenders}"
