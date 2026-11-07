"""Edge telemetry path: scheduler -> durable queue -> runtime routing -> Central /telemetry client."""
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from edge_collector.client import CentralClient, MalformedResponseError
from edge_collector.config import CollectorConfig
from edge_collector.queue import QueueRecord, SQLiteQueue
from edge_collector.runtime import EdgeRuntime
from edge_collector.snmp import SNMPError
from edge_collector.snmp_metrics import collect_raw_numeral
from edge_collector.snmp_wire import TAG_INTEGER, TAG_OCTET_STRING, Varbind
from edge_collector.telemetry_contract import ContractBook
from edge_collector.telemetry_scheduler import ScheduledTelemetry

INTEGRATION = str(uuid.uuid4())
REVISION = str(uuid.uuid4())
OID = "1.3.6.1.4.1.9999.1.1.0"
NOW = datetime(2026, 9, 18, tzinfo=UTC)
COLLECTOR_ID = uuid.uuid4()


def book(oid=OID):
    return ContractBook.from_plan([{
        "integration_id": INTEGRATION, "source_identifier": oid, "mapping_revision_id": REVISION, "revision": 1,
        "source_unit": "degC", "source_scale": "0.1", "conversion_hash": "a" * 64,
    }])


def plan_item():
    return {"integration_id": INTEGRATION, "target_host": "192.0.2.1", "target_port": 161, "poll_interval_seconds": 60}


def finish(scheduler):
    assert scheduler._future is not None
    scheduler._future.result(timeout=5)


def test_scheduler_polls_only_pinned_oids_and_queues_pinned_records(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    seen = []

    def reader(item, target, oid):
        seen.append((target.host, oid))
        return "253"

    now = [1.0]
    scheduler = ScheduledTelemetry(lambda: [plan_item()], book, reader, clock=lambda: now[0], wall_clock=lambda: NOW)
    try:
        scheduler.tick(queue); finish(scheduler)       # load plans
        scheduler.tick(queue); finish(scheduler)       # accept plans, start poll
        scheduler.tick(queue)                          # enqueue result
        [record] = queue.list_due(NOW)
    finally:
        scheduler.close()
        queue.close()
    assert seen == [("192.0.2.1", OID)]
    assert record.payload["record_type"] == "telemetry"
    assert record.payload["value"] == "253"
    assert record.payload["external_identifier"] == OID
    assert record.payload["mapping_revision_id"] == REVISION
    assert record.payload["source_scale"] == "0.1"


def test_one_failing_oid_does_not_block_its_sibling(tmp_path):
    other = "1.3.6.1.4.1.9999.2.2.0"
    two = ContractBook({**book().pins, **book(other).pins})
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))

    def reader(item, target, oid):
        if oid == OID:
            raise TimeoutError
        return "5"

    scheduler = ScheduledTelemetry(lambda: [plan_item()], lambda: two, reader, clock=lambda: 1.0, wall_clock=lambda: NOW)
    try:
        for _ in range(2):
            scheduler.tick(queue); finish(scheduler)
        scheduler.tick(queue)
        records = queue.list_due(NOW)
    finally:
        scheduler.close()
        queue.close()
    assert [r.payload["source_identifier"] for r in records] == [other]


def test_dead_device_costs_two_timeouts_not_one_per_pinned_oid(tmp_path):
    oids = [f"1.3.6.1.4.1.9999.{n}.1.0" for n in range(1, 7)]
    many = ContractBook({k: v for o in oids for k, v in book(o).pins.items()})
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    calls = []

    def reader(item, target, oid):
        calls.append(oid)
        raise TimeoutError

    scheduler = ScheduledTelemetry(lambda: [plan_item()], lambda: many, reader, clock=lambda: 1.0, wall_clock=lambda: NOW)
    try:
        for _ in range(2):
            scheduler.tick(queue); finish(scheduler)
        scheduler.tick(queue)
    finally:
        scheduler.close()
        queue.close()
    assert len(calls) == 2


def test_a_success_resets_the_timeout_count(tmp_path):
    oids = [f"1.3.6.1.4.1.9999.{n}.1.0" for n in range(1, 6)]
    many = ContractBook({k: v for o in oids for k, v in book(o).pins.items()})
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    calls = []

    def reader(item, target, oid):
        calls.append(oid)
        if len(calls) in (1, 3, 5):
            raise TimeoutError
        return "7"

    scheduler = ScheduledTelemetry(lambda: [plan_item()], lambda: many, reader, clock=lambda: 1.0, wall_clock=lambda: NOW)
    try:
        for _ in range(2):
            scheduler.tick(queue); finish(scheduler)
        scheduler.tick(queue)
        records = queue.list_due(NOW)
    finally:
        scheduler.close()
        queue.close()
    assert len(calls) == 5  # the count reset after each success, so the device was never abandoned
    assert records and records[-1].payload["source_identifier"] == oids[3]


def test_integration_without_a_pin_is_never_polled(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    calls = []
    scheduler = ScheduledTelemetry(
        lambda: [plan_item()], lambda: ContractBook({}), lambda *a: calls.append(a) or "1", clock=lambda: 1.0,
    )
    try:
        for _ in range(2):
            scheduler.tick(queue); finish(scheduler)
        scheduler.tick(queue)
    finally:
        scheduler.close()
        queue.close()
    assert calls == []


class Session:
    def __init__(self, varbind):
        self.varbind = varbind

    def get(self, oid):
        return self.varbind


def test_raw_numeral_is_exact_and_unscaled():
    assert collect_raw_numeral(Session(Varbind(OID, TAG_INTEGER, (253).to_bytes(2, "big"))), OID) == "253"
    assert collect_raw_numeral(Session(Varbind(OID, TAG_OCTET_STRING, b"21.50")), OID) == "21.50"
    with pytest.raises(SNMPError):
        collect_raw_numeral(Session(Varbind(OID, TAG_OCTET_STRING, b"nan")), OID)
    with pytest.raises(SNMPError):
        collect_raw_numeral(Session(Varbind("1.2.3", TAG_INTEGER, b"\x01")), OID)
    with pytest.raises(SNMPError):
        collect_raw_numeral(Session(Varbind("x", TAG_INTEGER, b"\x01")), "../etc")
    assert Decimal("21.50") == Decimal(collect_raw_numeral(Session(Varbind(OID, TAG_OCTET_STRING, b"21.50")), OID))


def test_zero_length_integer_encoding_is_malformed_not_zero():
    for tag in (TAG_INTEGER, 0x41, 0x42, 0x43):  # INTEGER, Counter32, Gauge32, TimeTicks
        with pytest.raises(SNMPError):
            collect_raw_numeral(Session(Varbind(OID, tag, b"")), OID)
    assert collect_raw_numeral(Session(Varbind(OID, TAG_INTEGER, b"\x00")), OID) == "0"  # a real zero is still zero


def telemetry_record(record_id="r1"):
    payload = {
        "record_type": "telemetry", "integration_id": INTEGRATION, "source_identifier": OID, "external_identifier": OID,
        "value": "253", "attributes": {}, "mapping_revision_id": REVISION, "source_unit": "degC", "source_scale": "0.1",
    }
    return QueueRecord(record_id, NOW, payload)


def test_client_posts_telemetry_and_keeps_only_unacknowledged_retryable_records():
    bodies = []

    def handler(request):
        assert request.url.path.endswith(f"/collectors/{COLLECTOR_ID}/telemetry")
        body = json.loads(request.content)
        bodies.append(body)
        statuses = {"a": {"status": "accepted"}, "b": {"status": "rejected", "error": "UNKNOWN_METRIC_MAPPING"},
                    "c": {"status": "rejected", "error": "INVALID_TELEMETRY_VALUE"}}
        return httpx.Response(200, json={"results": [{"dedup_key": r["dedup_key"], **statuses[r["dedup_key"]]} for r in body["records"]]})

    client = CentralClient("https://central.example/api/v1", COLLECTOR_ID, "s" * 32, transport=httpx.MockTransport(handler))
    ack = client.flush_telemetry([telemetry_record("a"), telemetry_record("b"), telemetry_record("c")])
    assert ack.acknowledged_ids == {"a", "c"}  # accepted, and permanently invalid
    assert ack.unacknowledged_ids == {"b"}  # mapping not created yet: stay queued
    sent = bodies[0]["records"][0]
    assert sent["mapping_revision_id"] == REVISION and sent["value"] == "253" and sent["source_scale"] == "0.1"
    assert "record_type" not in sent


def test_client_rejects_an_ack_for_an_unknown_record():
    client = CentralClient(
        "https://central.example/api/v1", COLLECTOR_ID, "s" * 32,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"dedup_key": "zzz", "status": "accepted"}]})),
    )
    with pytest.raises(MalformedResponseError):
        client.flush_telemetry([telemetry_record("a")])


def test_runtime_routes_telemetry_and_discovery_records_to_their_own_endpoints(tmp_path):
    from edge_collector.client import AckResult

    class Client:
        def __init__(self):
            self.telemetry, self.discovery = [], []

        def flush(self, records):
            self.discovery.append([r.record_id for r in records])
            return AckResult(frozenset(r.record_id for r in records), frozenset())

        def flush_telemetry(self, records):
            self.telemetry.append([r.record_id for r in records])
            return AckResult(frozenset(r.record_id for r in records), frozenset())

        def heartbeat(self, **kwargs):
            pass

    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"), clock=lambda: NOW)
    queue.enqueue(telemetry_record("t1"))
    queue.enqueue(QueueRecord("d1", NOW, {"integration_id": INTEGRATION, "external_identifier": "x"}))
    client = Client()
    EdgeRuntime(queue, client, clock=lambda: NOW).run_once()
    assert client.telemetry == [["t1"]]
    assert client.discovery == [["d1"]]
    assert queue.metrics().count == 0
    queue.close()
