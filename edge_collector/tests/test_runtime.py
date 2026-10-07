from __future__ import annotations

from datetime import datetime, timedelta, timezone

from edge_collector.config import CollectorConfig
from edge_collector.queue import QueueRecord, SQLiteQueue
from edge_collector.retry import RetryPolicy
from edge_collector.runtime import EdgeRuntime

UTC = timezone.utc
TEST_NOW = datetime(2026, 9, 18, tzinfo=UTC)


class FakeClient:
    def __init__(self, flush_result=None, flush_error: Exception | None = None) -> None:
        self.flush_result = flush_result
        self.flush_error = flush_error
        self.flushes: list[list[QueueRecord]] = []
        self.heartbeats: list[tuple[int, str]] = []

    def flush(self, records):
        self.flushes.append(list(records))
        if self.flush_error:
            raise self.flush_error
        return self.flush_result

    def heartbeat(self, *, queue_depth: int, status: str) -> None:
        self.heartbeats.append((queue_depth, status))


def make_record(identifier: str) -> QueueRecord:
    return QueueRecord(identifier, TEST_NOW, {"external_identifier": identifier})


def test_runtime_acknowledges_only_accepted_and_duplicate_records(tmp_path):
    from edge_collector.client import AckResult

    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"), clock=lambda: TEST_NOW)
    queue.enqueue(make_record("accepted"))
    queue.enqueue(make_record("rejected"))
    queue.enqueue(make_record("duplicate"))
    client = FakeClient(AckResult(frozenset({"accepted", "duplicate"}), frozenset({"rejected"})))
    runtime = EdgeRuntime(queue, client, retry_policy=RetryPolicy(jitter_ratio=0), clock=lambda: datetime(2026, 9, 18, 1, tzinfo=UTC))

    runtime.run_once()

    assert [record.record_id for record in queue.list_due(datetime(2026, 9, 18, 1, tzinfo=UTC))] == []
    assert queue.metrics().retries == 1


def test_runtime_defers_timeout_using_bounded_backoff(tmp_path):
    from edge_collector.client import RetryableTransportError

    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"), clock=lambda: TEST_NOW)
    queue.enqueue(make_record("timeout"))
    client = FakeClient(flush_error=RetryableTransportError("timeout"))
    runtime = EdgeRuntime(
        queue, client, retry_policy=RetryPolicy(base_delay_seconds=10, max_delay_seconds=30, jitter_ratio=0), clock=lambda: now
    )

    runtime.run_once()

    assert queue.list_due(now) == []
    assert [record.record_id for record in queue.list_due(now + timedelta(seconds=10))] == ["timeout"]
    assert queue.metrics().retries == 1


def test_runtime_sends_heartbeat_periodically_with_queue_metrics(tmp_path):
    from edge_collector.client import AckResult

    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"), clock=lambda: TEST_NOW)
    client = FakeClient(AckResult(frozenset(), frozenset()))
    runtime = EdgeRuntime(
        queue, client, retry_policy=RetryPolicy(jitter_ratio=0), heartbeat_interval_seconds=60, clock=lambda: now
    )

    runtime.run_once()
    runtime.run_once()

    assert client.heartbeats == [(0, "ok")]


def test_retry_policy_is_exponential_capped_and_jittered_within_bounds():
    policy = RetryPolicy(base_delay_seconds=10, max_delay_seconds=30, jitter_ratio=0.2)

    assert policy.delay_for(0, random_value=0) == 8
    assert policy.delay_for(1, random_value=1) == 24
    assert policy.delay_for(99, random_value=1) == 30


def test_central_payload_refusal_is_a_permanent_status_not_a_retry(tmp_path):
    import httpx
    import uuid
    import pytest

    from edge_collector.client import CentralClient, PermanentPayloadError, RetryableTransportError

    def respond(status):
        client = CentralClient("http://central.test/api/v1", uuid.uuid4(), "s", transport=httpx.MockTransport(lambda r: httpx.Response(status)))
        record = QueueRecord("a", TEST_NOW, {"integration_id": "i", "external_identifier": "h", "raw_attributes": {}})
        return client.flush([record])

    for status in (400, 413, 422):
        with pytest.raises(PermanentPayloadError):
            respond(status)
    for status in (401, 403, 429, 500, 503):  # authentication and server trouble stay retryable and non-dropping
        with pytest.raises(RetryableTransportError) as caught:
            respond(status)
        assert not isinstance(caught.value, PermanentPayloadError)


class PoisonedClient(FakeClient):
    """Refuses (422) any batch that contains a poisoned record, like Central's batch validation."""

    def __init__(self, poisoned: set[str]) -> None:
        super().__init__()
        self.poisoned = poisoned

    def flush(self, records):
        from edge_collector.client import AckResult, PermanentPayloadError

        self.flushes.append(list(records))
        if any(record.record_id in self.poisoned for record in records):
            raise PermanentPayloadError("422")
        ids = frozenset(record.record_id for record in records)
        return AckResult(ids, frozenset())


def test_a_poisoned_record_is_isolated_dropped_and_never_retried_forever(tmp_path):
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"), clock=lambda: TEST_NOW)
    for index in range(40):
        queue.enqueue(make_record(f"r{index:02d}"))
    client = PoisonedClient({"r17"})
    runtime = EdgeRuntime(queue, client, retry_policy=RetryPolicy(jitter_ratio=0), clock=lambda: now)

    runtime.run_once()

    assert queue.list_due(now) == []  # the 39 valid records were delivered and the poisoned one dropped
    delivered = {record.record_id for batch in client.flushes for record in batch if record.record_id != "r17"}
    assert len(delivered) == 39
    calls = len(client.flushes)
    runtime.run_once()
    assert len(client.flushes) == calls  # nothing left to resend: no endless retry of the poisoned record


def test_isolation_work_per_cycle_is_bounded_and_the_remainder_is_deferred(tmp_path):
    from edge_collector.runtime import MAX_ISOLATION_REQUESTS

    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"), clock=lambda: TEST_NOW)
    ids = [f"r{index:03d}" for index in range(200)]
    for identifier in ids:
        queue.enqueue(make_record(identifier))
    client = PoisonedClient(set(ids))  # every record is refused: worst case for a hostile queue
    runtime = EdgeRuntime(queue, client, retry_policy=RetryPolicy(jitter_ratio=0), clock=lambda: now)

    runtime.run_once()

    assert len(client.flushes) <= MAX_ISOLATION_REQUESTS
    assert queue.metrics().count > 0  # unprocessed records wait for the next cycle instead of hammering Central
