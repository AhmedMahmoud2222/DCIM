"""Issue #128 / G4-R over HTTP: duplicate acks after consumption, the permanent horizon refusal, hold release, settings
and the ledger status endpoint. Synthetic data only."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from edge_collector.telemetry_contract import PERMANENT_TELEMETRY_ERRORS, classify_telemetry_ack
from sqlalchemy import func, select, update

from app.application.mapping_revision_service import append_mapping_revision
from app.application.telemetry_retention import compact_eligible_raw
from app.domain.telemetry.models import (
    DailyTelemetryAggregate,
    MonitoringPolicy,
    TelemetryConsumedKey,
    TelemetryContractHold,
    TelemetryReading,
    TelemetryReplayLedgerState,
)
from tests.api._network_helpers import collector_with_integration
from tests.api._phase8_helpers import sign_request

OLD = datetime.now(UTC) - timedelta(days=400)


@pytest.fixture
async def world(client, auth_headers, db_session):
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=None))
    await db_session.execute(update(TelemetryReplayLedgerState).values(
        ledger_complete_since=datetime(2020, 1, 1, tzinfo=UTC), last_pruned_at=None, last_pruned_count=None, pruning_begun_at=None))
    await db_session.commit()
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    mapping = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": integration["id"], "source_identifier": "temp.1", "canonical_metric": "temperature_c", "unit": "degC"},
        headers=headers,
    )
    assert mapping.status_code == 201, mapping.text
    return {"headers": headers, "collector": collector, "integration": integration, "mapping": mapping.json()}


async def post_telemetry(client, collector, records):
    raw = json.dumps({"records": records}).encode()
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    headers["Content-Type"] = "application/json"
    response = await client.post(f"/api/v1/collectors/{collector['id']}/telemetry", content=raw, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def record(world, value, key, *, minutes=0, **extra):
    return {
        "dedup_key": key, "integration_id": world["integration"]["id"], "external_identifier": "crac-1",
        "source_identifier": "temp.1", "occurred_at": (OLD + timedelta(minutes=minutes)).isoformat(), "value": value,
        "mapping_revision_id": world["mapping"]["current_revision_id"], **extra,
    }


async def compact(db_session):
    assert await compact_eligible_raw(db_session) >= 1
    await db_session.commit()


async def aggregate(db_session):
    db_session.expire_all()
    return (await db_session.execute(select(DailyTelemetryAggregate))).scalar_one()


async def tombstones(db_session):
    return (await db_session.execute(select(func.count()).select_from(TelemetryConsumedKey))).scalar_one()


async def test_redelivery_after_compaction_is_acknowledged_as_duplicate_and_not_counted(client, world, db_session):
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    assert ack["status"] == "accepted", ack
    await compact(db_session)
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    assert ack["status"] == "duplicate"
    assert (await aggregate(db_session)).sample_count == 1
    assert (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one() == 0


async def test_late_record_merges_once_then_its_redelivery_is_a_duplicate(client, world, db_session):
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    (first,) = await post_telemetry(client, world["collector"], [record(world, 20, "late", minutes=5)])
    (again,) = await post_telemetry(client, world["collector"], [record(world, 20, "late", minutes=5)])
    assert (first["status"], again["status"]) == ("accepted", "duplicate")
    agg = await aggregate(db_session)
    assert agg.sample_count == 2 and float(agg.sum_value) == 30.0
    assert await tombstones(db_session) == 2


async def test_finite_horizon_returns_the_permanent_code_and_loses_nothing(client, world, db_session):
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=30))
    await db_session.commit()
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 99, "ancient", minutes=7)])
    assert (ack["status"], ack["error"]) == ("rejected", "REPLAY_HORIZON_EXCEEDED")
    assert classify_telemetry_ack(ack) == "acknowledge"
    agg = await aggregate(db_session)
    assert agg.sample_count == 1
    assert await tombstones(db_session) == 1
    assert (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one() == 0


async def test_null_horizon_accepts_arbitrarily_old_late_records(client, world, db_session):
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 5, "late", minutes=3)])
    assert ack["status"] == "accepted", ack


async def test_hold_release_past_the_horizon_into_a_compacted_day_is_refused_and_keeps_the_hold(client, world, db_session):
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    first = world["mapping"]["current_revision_id"]
    await append_mapping_revision(db_session, uuid.UUID(world["mapping"]["id"]), unit="degF")
    await db_session.commit()
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 68, "held", minutes=9, mapping_revision_id=None)])
    assert ack["error"] == "AMBIGUOUS_MAPPING_CONTRACT"
    hold_id = (await db_session.execute(select(TelemetryContractHold.id))).scalar_one()
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=30))
    await db_session.commit()
    refused = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve",
                                json={"mapping_revision_id": first}, headers=world["headers"])
    assert refused.status_code == 409 and "replay horizon" in refused.json()["title"].lower()
    db_session.expire_all()
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    assert (hold.status, hold.value_text) == ("held", "68")
    assert (await aggregate(db_session)).sample_count == 1
    # NULL horizon: the same release is allowed again, with no RBAC override involved.
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=None))
    await db_session.commit()
    ok = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve",
                           json={"mapping_revision_id": first}, headers=world["headers"])
    assert ok.status_code == 200, ok.text
    assert (await aggregate(db_session)).sample_count == 2


async def test_hold_release_into_an_uncompacted_day_stays_allowed_under_a_finite_horizon(client, world, db_session):
    first = world["mapping"]["current_revision_id"]
    await append_mapping_revision(db_session, uuid.UUID(world["mapping"]["id"]), unit="degF")
    await db_session.commit()
    await post_telemetry(client, world["collector"], [record(world, 68, "held", mapping_revision_id=None)])
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=30))
    await db_session.commit()
    ok = await client.post(f"/api/v1/telemetry/contract-holds/{hold.id}/resolve",
                           json={"mapping_revision_id": first}, headers=world["headers"])
    assert ok.status_code == 200, ok.text


async def test_settings_field_default_validation_and_omitted_put_preserves_it(client, world):
    headers = world["headers"]
    current = (await client.get("/api/v1/settings/monitoring", headers=headers)).json()
    assert current["late_replay_horizon_days"] is None
    body = {k: v for k, v in current.items() if k != "late_replay_horizon_days"}
    assert (await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": 0}, headers=headers)).status_code == 422
    set_ = await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": 45}, headers=headers)
    assert set_.status_code == 200, set_.text
    assert set_.json()["late_replay_horizon_days"] == 45
    kept = await client.put("/api/v1/settings/monitoring", json=body, headers=headers)
    assert kept.json()["late_replay_horizon_days"] == 45
    cleared = await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": None}, headers=headers)
    assert cleared.json()["late_replay_horizon_days"] is None


async def test_ledger_status_endpoint_auth_values_and_warnings(client, world, db_session, auth_headers):
    url = "/api/v1/telemetry/replay-ledger"
    assert (await client.get(url)).status_code == 401
    status = (await client.get(url, headers=world["headers"])).json()
    assert status["late_replay_horizon_days"] is None and status["effective_horizon_days"] is None
    assert status["warnings"] == [] and status["oldest_consumed_day"] is None
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    status = (await client.get(url, headers=world["headers"])).json()
    assert status["oldest_consumed_day"] is not None
    assert "horizon_unset_ledger_grows_without_bound" in status["warnings"]
    assert "k1" not in json.dumps(status)
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=30))
    await db_session.commit()
    status = (await client.get(url, headers=world["headers"])).json()
    assert status["late_replay_horizon_days"] == 30 and status["effective_horizon_days"] == 30.0
    viewer = await auth_headers("Viewer")
    assert (await client.get(url, headers=viewer)).status_code == 200


def test_collector_classifies_the_new_code_as_permanent_and_unknown_codes_as_retry():
    assert "REPLAY_HORIZON_EXCEEDED" in PERMANENT_TELEMETRY_ERRORS
    assert classify_telemetry_ack({"status": "rejected", "error": "REPLAY_HORIZON_EXCEEDED"}) == "acknowledge"
    assert classify_telemetry_ack({"status": "rejected", "error": "SOME_FUTURE_CODE"}) == "retry"
    assert classify_telemetry_ack({"status": "duplicate"}) == "acknowledge"


async def test_raised_raw_retention_redelivery_is_a_duplicate_with_no_raw_row_and_no_alarm(client, world, db_session):
    """Codex P1-1 over signed HTTP: 365 -> 730 days must not let a consumed key back in as raw history."""
    from app.domain.alarm.models import Alarm

    await post_telemetry(client, world["collector"], [record(world, 10, "k1"), record(world, 20, "k2", minutes=1)])
    await compact(db_session)
    before = await aggregate(db_session)
    await db_session.execute(update(MonitoringPolicy).values(raw_retention_days=730))
    await db_session.commit()
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    assert ack["status"] == "duplicate"
    after = await aggregate(db_session)
    assert (after.sample_count, after.sum_value) == (before.sample_count, before.sum_value)
    assert (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(Alarm))).scalar_one() == 0
    # A fresh record on that day stays raw and is not claimed as consumed.
    (fresh,) = await post_telemetry(client, world["collector"], [record(world, 30, "k3", minutes=2)])
    assert fresh["status"] == "accepted"
    assert (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one() == 1
    assert await tombstones(db_session) == 2
    # With a finite horizon the same compacted day refuses over-horizon data.
    await db_session.execute(update(MonitoringPolicy).values(late_replay_horizon_days=30))
    await db_session.commit()
    (refused,) = await post_telemetry(client, world["collector"], [record(world, 40, "k4", minutes=3)])
    assert (refused["status"], refused["error"]) == ("rejected", "REPLAY_HORIZON_EXCEEDED")


async def test_hold_release_after_a_retention_increase_still_respects_the_finite_horizon(client, world, db_session):
    await post_telemetry(client, world["collector"], [record(world, 10, "k1")])
    await compact(db_session)
    first = world["mapping"]["current_revision_id"]
    await append_mapping_revision(db_session, uuid.UUID(world["mapping"]["id"]), unit="degF")
    await db_session.commit()
    await post_telemetry(client, world["collector"], [record(world, 68, "held", minutes=9, mapping_revision_id=None)])
    hold_id = (await db_session.execute(select(TelemetryContractHold.id))).scalar_one()
    await db_session.execute(update(MonitoringPolicy).values(raw_retention_days=730, late_replay_horizon_days=30))
    await db_session.commit()
    refused = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve",
                                json={"mapping_revision_id": first}, headers=world["headers"])
    assert refused.status_code == 409
    db_session.expire_all()
    assert (await db_session.execute(select(TelemetryContractHold.status))).scalar_one() == "held"


async def test_clearing_the_horizon_over_the_api_is_refused_after_pruning_but_allowed_before(client, world, db_session):
    from app.infrastructure.tasks import maintenance

    headers = world["headers"]
    current = (await client.get("/api/v1/settings/monitoring", headers=headers)).json()
    body = {k: v for k, v in current.items() if k != "late_replay_horizon_days"}
    await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": 5}, headers=headers)
    allowed = await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": None}, headers=headers)
    assert allowed.status_code == 200 and allowed.json()["late_replay_horizon_days"] is None
    await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": 5}, headers=headers)
    maintenance.prune_consumed_keys(now=datetime.now(UTC) + timedelta(days=30))
    refused = await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": None}, headers=headers)
    assert refused.status_code == 409 and "cannot be cleared" in refused.json()["title"].lower()
    still = (await client.get("/api/v1/settings/monitoring", headers=headers)).json()
    assert still["late_replay_horizon_days"] == 5
    status = (await client.get("/api/v1/telemetry/replay-ledger", headers=headers)).json()
    assert status["pruning_begun_at"] is not None
    raised = await client.put("/api/v1/settings/monitoring", json={**body, "late_replay_horizon_days": 60}, headers=headers)
    assert raised.status_code == 200
