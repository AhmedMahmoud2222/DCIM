"""Issue #128 / G1 over HTTP: wire pinning, the authenticated contract plan, bounded holds and operator resolution."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from edge_collector.telemetry_contract import ContractBook, classify_telemetry_ack, telemetry_record_payload
from sqlalchemy import select

from app.application import telemetry_service
from app.application.mapping_revision_service import append_mapping_revision
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryContractHold, TelemetryReading
from tests.api._network_helpers import collector_with_integration, signed_get
from tests.api._phase8_helpers import sign_request


@pytest.fixture
async def world(client, auth_headers):
    headers = await auth_headers("Administrator")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    mapping = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": integration["id"], "source_identifier": "temp.1", "canonical_metric": "temperature_c", "unit": "degF"},
        headers=headers,
    )
    assert mapping.status_code == 201, mapping.text
    return {"headers": headers, "collector": collector, "integration": integration, "mapping": mapping.json()}


async def post_telemetry(client, collector, records, *, path_collector=None):
    raw = json.dumps({"records": records}).encode()
    signer = path_collector or collector
    headers = sign_request(secret=signer["secret"], collector_id=uuid.UUID(signer["id"]), raw_body=raw)
    headers["Content-Type"] = "application/json"
    response = await client.post(f"/api/v1/collectors/{signer['id']}/telemetry", content=raw, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def record(world, value, *, key=None, **extra):
    return {
        "dedup_key": key or uuid.uuid4().hex, "integration_id": world["integration"]["id"], "external_identifier": "crac-1",
        "source_identifier": "temp.1", "occurred_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(), "value": value, **extra,
    }


async def change_source_to_celsius(db_session, world):
    revision = await append_mapping_revision(db_session, uuid.UUID(world["mapping"]["id"]), unit="degC")
    await db_session.commit()
    return revision


async def test_creating_a_mapping_returns_its_first_revision(world):
    assert world["mapping"]["current_revision_id"] is not None


async def test_plan_is_authenticated_scoped_to_the_collectors_own_integrations_and_matches_the_mapping(client, auth_headers, world, db_session):
    other_collector, other_integration = await collector_with_integration(client, world["headers"], integration_type="icmp")
    await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": other_integration["id"], "source_identifier": "x", "canonical_metric": "power_kw", "unit": "W"},
        headers=world["headers"],
    )
    plan = (await signed_get(client, world["collector"], "telemetry-contracts")).json()
    assert [(item["source_identifier"], item["mapping_revision_id"], item["source_unit"], item["source_scale"], item["revision"], item["provenance"])
            for item in plan] == [("temp.1", world["mapping"]["current_revision_id"], "degF", "1.00000000", 1, "authored")]
    assert other_integration["id"] not in json.dumps(plan)
    forged = dict(world["collector"], secret="wrong-secret-wrong-secret-wrong-secret")
    assert (await signed_get(client, forged, "telemetry-contracts")).status_code == 401
    swapped = await client.get(
        f"/api/v1/collectors/{other_collector['id']}/telemetry-contracts",
        headers=sign_request(secret=world["collector"]["secret"], collector_id=uuid.UUID(world["collector"]["id"]), raw_body=b""),
    )
    assert swapped.status_code in {401, 403}
    assert (await client.get(f"/api/v1/collectors/{world['collector']['id']}/telemetry-contracts")).status_code in {401, 422}
    # After a source change the plan advertises the new revision, the old one remains resolvable by id.
    new = await change_source_to_celsius(db_session, world)
    refreshed = (await signed_get(client, world["collector"], "telemetry-contracts")).json()
    assert [(item["mapping_revision_id"], item["source_unit"], item["revision"]) for item in refreshed] == [(str(new.id), "degC", 2)]


async def test_collector_helpers_build_a_record_central_accepts_end_to_end_across_a_source_change(client, world, db_session):
    plan = (await signed_get(client, world["collector"], "telemetry-contracts")).json()
    payload = telemetry_record_payload(
        ContractBook.from_plan(plan), integration_id=world["integration"]["id"], source_identifier="temp.1",
        external_identifier="crac-1", value=68.0,
    )  # acquired under revision 1 (degF) and queued
    await change_source_to_celsius(db_session, world)  # then Central's mapping becomes degC
    wire = {**payload, "dedup_key": "queued-1", "occurred_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()}
    (ack,) = await post_telemetry(client, world["collector"], [wire])
    assert ack["status"] == "accepted"
    assert classify_telemetry_ack(ack) == "acknowledge"
    stored = (await db_session.execute(select(TelemetryReading))).scalar_one()
    await db_session.refresh(stored)
    assert (float(stored.value), stored.unit, stored.raw_unit, str(stored.mapping_revision_id), stored.contract_evidence) == (
        20.0, "degC", "degF", world["mapping"]["current_revision_id"], "pinned",
    )
    history = await client.get("/api/v1/telemetry/latest", params={"integration_id": world["integration"]["id"]}, headers=world["headers"])
    assert history.json()[0]["value"] == 20.0 and history.json()[0]["raw_unit"] == "degF"


async def test_unpinned_record_after_a_source_change_is_held_not_reinterpreted_and_never_stored(client, world, db_session):
    await change_source_to_celsius(db_session, world)
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 68, key="legacy-1")])
    assert (ack["status"], ack["error"]) == ("rejected", "AMBIGUOUS_MAPPING_CONTRACT")
    assert classify_telemetry_ack(ack) == "retry"
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    assert (hold.status, hold.attempts, hold.reason, hold.value_text) == ("held", 1, "MULTIPLE_REVISIONS", "68")
    await post_telemetry(client, world["collector"], [record(world, 68, key="legacy-1")])
    await db_session.refresh(hold)
    assert hold.attempts == 2


async def test_unpinned_record_on_a_never_changed_mapping_is_accepted_with_inferred_evidence(client, world, db_session):
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 68)])
    assert ack["status"] == "accepted"
    assert (await db_session.execute(select(TelemetryReading.contract_evidence))).scalar_one() == "inferred_single_revision"


async def test_bad_pins_are_permanent_rejections_and_peers_persist(client, world, db_session):
    other = await client.post(
        "/api/v1/telemetry/mappings",
        json={"integration_id": world["integration"]["id"], "source_identifier": "temp.2", "canonical_metric": "temperature_c", "unit": "degC"},
        headers=world["headers"],
    )
    results = await post_telemetry(client, world["collector"], [
        record(world, 1, key="ok", mapping_revision_id=world["mapping"]["current_revision_id"]),
        record(world, 1, key="cross-source", mapping_revision_id=other.json()["current_revision_id"]),
        record(world, 1, key="unknown", mapping_revision_id=str(uuid.uuid4())),
        record(world, 1, key="echo", mapping_revision_id=world["mapping"]["current_revision_id"], source_unit="degC"),
    ])
    assert [(r["dedup_key"], r["status"], r["error"]) for r in results] == [
        ("ok", "accepted", None), ("cross-source", "rejected", "MAPPING_REVISION_MISMATCH"),
        ("unknown", "rejected", "UNKNOWN_MAPPING_REVISION"), ("echo", "rejected", "MAPPING_REVISION_MISMATCH"),
    ]
    assert [classify_telemetry_ack(r) for r in results] == ["acknowledge"] * 4
    assert (await db_session.execute(select(TelemetryReading.dedup_key))).scalars().all() == ["ok"]


async def test_pinned_record_from_an_unassigned_collector_is_still_not_assigned(client, world, auth_headers):
    stranger, _ = await collector_with_integration(client, world["headers"], integration_type="icmp")
    (ack,) = await post_telemetry(
        client, stranger, [record(world, 1, mapping_revision_id=world["mapping"]["current_revision_id"])]
    )
    assert ack["error"] == "NOT_ASSIGNED"


async def test_retry_budget_is_bounded_and_expiry_keeps_the_record_for_the_operator(client, world, db_session, monkeypatch):
    monkeypatch.setattr(telemetry_service, "MAX_HOLD_ATTEMPTS", 3)
    await change_source_to_celsius(db_session, world)
    errors = []
    for _ in range(5):
        (ack,) = await post_telemetry(client, world["collector"], [record(world, 68, key="stuck")])
        errors.append(ack["error"])
    assert errors == ["AMBIGUOUS_MAPPING_CONTRACT"] * 3 + ["CONTRACT_HOLD_EXPIRED"] * 2
    assert classify_telemetry_ack({"status": "rejected", "error": errors[-1]}) == "acknowledge"
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    await db_session.refresh(hold)
    assert (hold.status, hold.value_text) == ("expired", "68")
    assert (await db_session.execute(select(TelemetryReading))).scalars().all() == []


async def test_hold_age_is_bounded_too(client, world, db_session):
    await change_source_to_celsius(db_session, world)
    await post_telemetry(client, world["collector"], [record(world, 68, key="old")])
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    hold.first_held_at = datetime.now(UTC) - telemetry_service.MAX_HOLD_AGE - timedelta(minutes=1)
    await db_session.commit()
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 68, key="old")])
    assert ack["error"] == "CONTRACT_HOLD_EXPIRED"


async def test_operator_lists_and_resolves_a_hold_under_a_chosen_revision(client, world, db_session):
    first = world["mapping"]["current_revision_id"]
    await change_source_to_celsius(db_session, world)
    await post_telemetry(client, world["collector"], [record(world, 68, key="held-1")])
    listed = (await client.get("/api/v1/telemetry/contract-holds", params={"status": "held"}, headers=world["headers"])).json()
    assert [(h["dedup_key"], h["reason"], h["value"]) for h in listed] == [("held-1", "MULTIPLE_REVISIONS", "68")]
    hold_id = listed[0]["id"]

    wrong = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve", json={"mapping_revision_id": str(uuid.uuid4())}, headers=world["headers"])
    assert wrong.status_code == 422
    resolved = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve", json={"mapping_revision_id": first}, headers=world["headers"])
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved" and resolved.json()["resolved_revision_id"] == first
    stored = (await db_session.execute(select(TelemetryReading))).scalar_one()
    assert (float(stored.value), stored.unit, stored.contract_evidence) == (20.0, "degC", "operator_resolved")
    again = await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve", json={"mapping_revision_id": first}, headers=world["headers"])
    assert again.status_code == 409
    # A later redelivery of the same record from the collector is acknowledged as a duplicate, not held again.
    (ack,) = await post_telemetry(client, world["collector"], [record(world, 68, key="held-1")])
    assert ack["status"] == "duplicate"


async def test_a_hold_can_be_resolved_after_it_expired(client, world, db_session, monkeypatch):
    first = world["mapping"]["current_revision_id"]
    monkeypatch.setattr(telemetry_service, "MAX_HOLD_ATTEMPTS", 1)
    await change_source_to_celsius(db_session, world)
    for _ in range(3):
        await post_telemetry(client, world["collector"], [record(world, 86, key="exp")])
    hold = (await db_session.execute(select(TelemetryContractHold))).scalar_one()
    assert hold.status == "expired"
    resolved = await client.post(f"/api/v1/telemetry/contract-holds/{hold.id}/resolve", json={"mapping_revision_id": first}, headers=world["headers"])
    assert resolved.status_code == 200
    assert float((await db_session.execute(select(TelemetryReading.value))).scalar_one()) == 30.0


async def test_hold_endpoints_enforce_authentication_and_permissions(client, auth_headers, world, db_session):
    await change_source_to_celsius(db_session, world)
    await post_telemetry(client, world["collector"], [record(world, 68, key="h")])
    hold_id = (await db_session.execute(select(TelemetryContractHold.id))).scalar_one()
    body = {"mapping_revision_id": world["mapping"]["current_revision_id"]}
    assert (await client.get("/api/v1/telemetry/contract-holds")).status_code == 401
    assert (await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve", json=body)).status_code == 401
    viewer = await auth_headers("Viewer")
    assert (await client.get("/api/v1/telemetry/contract-holds", headers=viewer)).status_code == 200
    assert (await client.post(f"/api/v1/telemetry/contract-holds/{hold_id}/resolve", json=body, headers=viewer)).status_code == 403
    assert (await client.post(f"/api/v1/telemetry/contract-holds/{uuid.uuid4()}/resolve", json=body, headers=world["headers"])).status_code == 404


async def test_history_and_latest_keep_working_for_pinned_and_legacy_rows_together(client, world, db_session):
    mapping = (await db_session.execute(select(IntegrationMetricMapping))).scalar_one()
    legacy = TelemetryReading(
        id=uuid.uuid4(), collector_id=uuid.UUID(world["collector"]["id"]), integration_id=mapping.integration_id,
        mapping_id=mapping.id, external_identifier="crac-1", series_key="legacy", dedup_key="legacy", metric="temperature_c",
        unit="degC", value=19, occurred_at=datetime.now(UTC) - timedelta(minutes=5), received_at=datetime.now(UTC), attributes={},
    )
    db_session.add(legacy)
    await db_session.commit()
    await post_telemetry(client, world["collector"], [record(world, 68, mapping_revision_id=world["mapping"]["current_revision_id"])])
    response = await client.get("/api/v1/telemetry/latest", params={"integration_id": world["integration"]["id"]}, headers=world["headers"])
    assert response.status_code == 200
    assert sorted(item["value"] for item in response.json()) == [19.0, 20.0]
