from __future__ import annotations

from datetime import datetime, timedelta, timezone

from edge_collector.config import CollectorConfig
from edge_collector.queue import QueueRecord, SQLiteQueue
from edge_collector.retry import RetryPolicy
from edge_collector.runtime import EdgeRuntime

UTC = timezone.utc


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
    return QueueRecord(identifier, datetime(2026, 9, 18, tzinfo=UTC), {"external_identifier": identifier})


def test_runtime_acknowledges_only_accepted_and_duplicate_records(tmp_path):
    from edge_collector.client import AckResult

    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"))
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
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"))
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
    queue = SQLiteQueue(CollectorConfig(tmp_path / "queue.sqlite"))
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
