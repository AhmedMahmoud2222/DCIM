from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from edge_collector.config import CollectorConfig
from edge_collector.queue import QueueCorruptionError, QueueRecord, SQLiteQueue

UTC = timezone.utc
TEST_NOW = datetime(2026, 9, 18, tzinfo=UTC)


def record(identifier: str, at: datetime | None = None) -> QueueRecord:
    return QueueRecord(
        record_id=identifier,
        occurred_at=at or TEST_NOW,
        payload={"metric": "temperature_c", "value": 21.5, "source": identifier},
    )


def queue_at(tmp_path, **overrides) -> SQLiteQueue:
    return SQLiteQueue(CollectorConfig(database_path=tmp_path / "queue.sqlite3", **overrides), clock=lambda: TEST_NOW)


def test_restart_preserves_unacknowledged_records(tmp_path):
    """Removing the process must not remove a collected, unsent observation."""
    path = tmp_path / "queue.sqlite3"
    queue = SQLiteQueue(CollectorConfig(database_path=path), clock=lambda: TEST_NOW)
    queue.enqueue(record("first"))
    queue.close()

    reopened = SQLiteQueue(CollectorConfig(database_path=path), clock=lambda: TEST_NOW)

    assert [item.record_id for item in reopened.list_due(datetime(2026, 9, 19, tzinfo=UTC))] == ["first"]


def test_acknowledge_removes_only_the_accepted_subset(tmp_path):
    """A partial central ACK must not discard sibling rows from a batch."""
    queue = queue_at(tmp_path)
    queue.enqueue(record("accepted"))
    queue.enqueue(record("unacknowledged"))

    removed = queue.acknowledge(["accepted"])

    assert removed == 1
    assert [item.record_id for item in queue.list_due(datetime(2026, 9, 19, tzinfo=UTC))] == ["unacknowledged"]


def test_capped_queue_drops_oldest_and_exposes_the_loss(tmp_path):
    """A full WAN buffer keeps the freshest record while surfacing the local drop."""
    queue = queue_at(tmp_path, max_records=1)
    queue.enqueue(record("older", datetime(2026, 9, 18, tzinfo=UTC)))
    queue.enqueue(record("newer", datetime(2026, 9, 18, 0, 1, tzinfo=UTC)))

    metrics = queue.metrics()

    assert [item.record_id for item in queue.list_due(datetime(2026, 9, 19, tzinfo=UTC))] == ["newer"]
    assert metrics.count == 1
    assert metrics.dropped == 1


def test_retention_expiry_deletes_stale_records_and_exposes_expiry(tmp_path):
    """Expired telemetry is not sent late enough to misrepresent current operations."""
    queue = queue_at(tmp_path, retention_seconds=60)
    queue.enqueue(record("stale", datetime(2026, 9, 18, tzinfo=UTC)))

    due = queue.list_due(datetime(2026, 9, 18, 0, 2, tzinfo=UTC))

    assert due == []
    assert queue.metrics().expired == 1


def test_corrupt_database_fails_closed_with_integrity_diagnostics(tmp_path):
    """A damaged local database cannot be silently replaced or used for sends."""
    path = tmp_path / "queue.sqlite3"
    path.write_bytes(b"this is not a sqlite database")

    with pytest.raises(QueueCorruptionError, match="integrity") as error:
        SQLiteQueue(CollectorConfig(database_path=path))

    assert error.value.diagnostic["database_path"] == str(path)
    assert error.value.diagnostic["check"] == "integrity_check"


def test_retry_defers_only_selected_records_and_tracks_attempts(tmp_path):
    """A transient failure must remain durable and invisible until its retry time."""
    queue = queue_at(tmp_path)
    now = datetime(2026, 9, 18, tzinfo=UTC)
    queue.enqueue(record("retry", now))
    queue.enqueue(record("ready", now))

    queue.mark_retry(["retry"], now=now, delay_seconds=30)

    assert [item.record_id for item in queue.list_due(now)] == ["ready"]
    assert {item.record_id for item in queue.list_due(now + timedelta(seconds=30))} == {"retry", "ready"}
    assert queue.metrics().retries == 1
