from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from edge_collector.client import (
    CentralClient,
    MalformedResponseError,
    RetryableTransportError,
)
from edge_collector.queue import QueueRecord

UTC = timezone.utc
COLLECTOR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SECRET = "collector-test-secret"


def queued_record(identifier: str = "record-1") -> QueueRecord:
    return QueueRecord(
        record_id=identifier,
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        payload={
            "integration_id": "22222222-2222-2222-2222-222222222222",
            "external_identifier": "ups-a",
            "raw_attributes": {"metric": "temperature_c", "value": 21.5},
        },
    )


def test_flush_signs_the_exact_canonical_body_and_accepts_duplicate_ack():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"batch_id": json.loads(request.content)["batch_id"], "results": [
            {"dedup_key": "record-1", "status": "duplicate"}
        ]})

    client = CentralClient("https://central.example/api/v1", COLLECTOR_ID, SECRET, transport=httpx.MockTransport(handler))
    result = client.flush([queued_record()])

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    timestamp = request.headers["X-Collector-Timestamp"]
    nonce = request.headers["X-Collector-Nonce"]
    expected_signature = hmac.new(
        SECRET.encode(), f"{COLLECTOR_ID}.{timestamp}.{nonce}.".encode() + request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["X-Collector-Signature"] == expected_signature
    assert request.url.path == "/api/v1/collectors/11111111-1111-1111-1111-111111111111/ingest"
    assert json.loads(request.content)["records"][0]["dedup_key"] == "record-1"
    assert result.acknowledged_ids == frozenset({"record-1"})


@pytest.mark.parametrize("status_code", [429, 500])
def test_flush_marks_retryable_http_failures(status_code: int):
    client = CentralClient(
        "https://central.example/api/v1", COLLECTOR_ID, SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code)),
    )

    with pytest.raises(RetryableTransportError):
        client.flush([queued_record()])


def test_flush_marks_a_timeout_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("outage", request=request)

    client = CentralClient("https://central.example/api/v1", COLLECTOR_ID, SECRET, transport=httpx.MockTransport(handler))

    with pytest.raises(RetryableTransportError):
        client.flush([queued_record()])


def test_flush_fails_closed_for_a_malformed_ack():
    client = CentralClient(
        "https://central.example/api/v1", COLLECTOR_ID, SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []})),
    )

    with pytest.raises(MalformedResponseError):
        client.flush([queued_record()])


def test_heartbeat_sends_current_queue_depth_with_a_fresh_signature():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    client = CentralClient("https://central.example/api/v1", COLLECTOR_ID, SECRET, transport=httpx.MockTransport(handler))
    client.heartbeat(queue_depth=7, status="degraded")

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.url.path.endswith("/heartbeat")
    assert json.loads(request.content) == {"queue_depth": 7, "status": "degraded"}
    assert abs(int(request.headers["X-Collector-Timestamp"]) - int(time.time())) <= 1
