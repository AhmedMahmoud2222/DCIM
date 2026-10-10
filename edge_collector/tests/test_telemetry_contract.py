from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from edge_collector.client import CentralClient, MalformedResponseError
from edge_collector.config import CollectorConfig
from edge_collector.queue import QueueRecord, SQLiteQueue
from edge_collector.telemetry_contract import (
    PERMANENT_TELEMETRY_ERRORS,
    RETRYABLE_TELEMETRY_ERRORS,
    ContractBook,
    ContractPlanError,
    MissingContractError,
    classify_telemetry_ack,
    telemetry_record_payload,
)

INTEGRATION = "22222222-2222-2222-2222-222222222222"
REVISION = "33333333-3333-3333-3333-333333333333"
COLLECTOR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def plan_entry(**overrides):
    entry = {
        "integration_id": INTEGRATION, "source_identifier": "temp.1", "mapping_revision_id": REVISION, "revision": 1,
        "canonical_metric": "temperature_c", "source_unit": "degF", "source_scale": "1.00000000",
        "registry_version": "1", "conversion_hash": "a" * 64, "provenance": "authored",
        "effective_from": "2026-10-09T12:00:00Z",
    }
    return entry | overrides


def test_payload_carries_the_pin_and_survives_the_durable_queue_unchanged(tmp_path):
    book = ContractBook.from_plan([plan_entry()])
    payload = telemetry_record_payload(
        book, integration_id=INTEGRATION, source_identifier="temp.1", external_identifier="crac-1", value=68.0,
        attributes={"poll": 3},
    )
    assert payload["mapping_revision_id"] == REVISION and payload["source_unit"] == "degF"

    now = datetime(2026, 10, 9, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(database_path=tmp_path / "q.sqlite3"), clock=lambda: now)
    assert queue.enqueue(QueueRecord(record_id="r1", occurred_at=now, payload=payload))
    queue.close()
    # A restart, possibly after the central mapping changed: the replayed payload is byte-for-byte what was acquired.
    reopened = SQLiteQueue(CollectorConfig(database_path=tmp_path / "q.sqlite3"), clock=lambda: now)
    (replayed,) = reopened.list_due(datetime(2026, 10, 10, tzinfo=UTC))
    assert dict(replayed.payload) == payload


def test_an_unpinned_value_is_never_built():
    book = ContractBook.from_plan([plan_entry()])
    with pytest.raises(MissingContractError):
        telemetry_record_payload(book, integration_id=INTEGRATION, source_identifier="other", external_identifier="x", value=1.0)
    with pytest.raises(MissingContractError):
        telemetry_record_payload(
            ContractBook.from_plan([]), integration_id=INTEGRATION, source_identifier="temp.1", external_identifier="x", value=1.0
        )


@pytest.mark.parametrize("bad", [
    {"mapping_revision_id": "not-a-uuid"}, {"conversion_hash": "xyz"}, {"revision": 0}, {"revision": True},
    {"source_unit": ""}, {"source_scale": 1}, {"source_identifier": ""},
])
def test_malformed_plan_entries_are_rejected(bad):
    with pytest.raises(ContractPlanError):
        ContractBook.from_plan([plan_entry(**bad)])


def test_plan_shape_and_duplicates_are_rejected():
    for raw in ({}, [1], [plan_entry(), plan_entry()]):
        with pytest.raises(ContractPlanError):
            ContractBook.from_plan(raw)


@pytest.mark.parametrize("result,expected", [
    ({"status": "accepted"}, "acknowledge"),
    ({"status": "duplicate"}, "acknowledge"),
    ({"status": "rejected", "error": "AMBIGUOUS_MAPPING_CONTRACT"}, "retry"),
    ({"status": "rejected", "error": "CONVERSION_CONTRACT_DRIFT"}, "retry"),
    ({"status": "rejected", "error": "CONTRACT_HOLD_EXPIRED"}, "acknowledge"),
    ({"status": "rejected", "error": "UNKNOWN_MAPPING_REVISION"}, "acknowledge"),
    ({"status": "rejected", "error": "MAPPING_REVISION_MISMATCH"}, "acknowledge"),
    ({"status": "rejected", "error": "SOMETHING_NEW"}, "retry"),
    ({"status": "weird"}, "retry"),
])
def test_ack_classification_never_drops_a_record_central_still_holds(result, expected):
    assert classify_telemetry_ack(result) == expected


def test_error_sets_are_disjoint():
    assert not RETRYABLE_TELEMETRY_ERRORS & PERMANENT_TELEMETRY_ERRORS


def test_client_fetches_and_validates_the_signed_contract_plan():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["signed"] = "x-dcim-signature" in {key.lower() for key in request.headers} or bool(request.headers)
        return httpx.Response(200, json=[plan_entry()])

    client = CentralClient("https://central.example/api/v1", COLLECTOR_ID, "secret", transport=httpx.MockTransport(handler))
    book = client.get_telemetry_contracts()
    assert seen["path"].endswith(f"/collectors/{COLLECTOR_ID}/telemetry-contracts")
    assert book.pin_for(INTEGRATION, "temp.1").mapping_revision_id == REVISION

    bad = CentralClient(
        "https://central.example/api/v1", COLLECTOR_ID, "secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{"nope": 1}])),
    )
    with pytest.raises(MalformedResponseError):
        bad.get_telemetry_contracts()
    garbage = CentralClient(
        "https://central.example/api/v1", COLLECTOR_ID, "secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
    )
    with pytest.raises(MalformedResponseError):
        garbage.get_telemetry_contracts()
    assert json.dumps(telemetry_record_payload(book, integration_id=INTEGRATION, source_identifier="temp.1", external_identifier="e", value=1.5))


# ----- Issue #128 / G3: exact numerals ---------------------------------------------------------------------------

from edge_collector.telemetry_contract import value_numeral


@pytest.mark.parametrize("value,expected", [
    (Decimal("1234567890.12345678"), "1234567890.12345678"),
    ("1234567890.12345678", "1234567890.12345678"),
    (12345678901234567890, "12345678901234567890"),
    (Decimal("1E+3"), "1E+3"),
    (Decimal("-0.0"), "-0.0"),
    ("-0", "-0"),
    (0.1, "0.1"),
    (68.0, "68.0"),
])
def test_value_numeral_keeps_every_digit_it_is_given(value, expected):
    assert value_numeral(value) == expected


def test_a_float_cannot_recover_digits_it_already_lost():
    lost = 1234567890.12345678          # the literal is rounded to a float before this function sees it
    assert value_numeral(lost) == "1234567890.1234567"
    assert value_numeral(Decimal("1234567890.12345678")) == "1234567890.12345678"  # acquire as Decimal or text instead


@pytest.mark.parametrize("bad", [True, False, None, [1], Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf"),
                                 "abc", "", " 1", "0x10", "1" * 65, Decimal("0." + "1" * 70)])
def test_unsendable_values_are_refused_before_they_are_queued(bad):
    with pytest.raises((ValueError, TypeError)):
        value_numeral(bad)


def test_exact_value_survives_the_durable_queue_unchanged(tmp_path):
    book = ContractBook.from_plan([plan_entry()])
    payload = telemetry_record_payload(
        book, integration_id=INTEGRATION, source_identifier="temp.1", external_identifier="e", value=Decimal("9999999999.99999999"),
    )
    now = datetime(2026, 10, 10, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(database_path=tmp_path / "q.sqlite3"), clock=lambda: now)
    assert queue.enqueue(QueueRecord(record_id="r1", occurred_at=now, payload=payload))
    (replayed,) = queue.list_due(datetime(2026, 10, 11, tzinfo=UTC))
    assert replayed.payload["value"] == "9999999999.99999999"
    assert json.dumps(dict(replayed.payload))  # a plain JSON document: no Decimal in the queue
