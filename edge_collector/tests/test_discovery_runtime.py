"""The production runtime invokes discovery without sacrificing delivery/heartbeat."""
from datetime import datetime, timezone

from edge_collector.client import AckResult
from edge_collector.config import CollectorConfig
from edge_collector.queue import SQLiteQueue
from edge_collector.runtime import EdgeRuntime


class Transport:
    def __init__(self):
        self.heartbeats = 0

    def flush(self, records):
        return AckResult(frozenset(r.record_id for r in records), frozenset())

    def heartbeat(self, **_kwargs):
        self.heartbeats += 1


class Discovery:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def tick(self, queue):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private credential text")


def test_runtime_invokes_discovery_and_maintains_heartbeat(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    client, discovery = Transport(), Discovery()
    runtime = EdgeRuntime(queue, client, discovery=discovery)
    runtime.run_once()
    assert discovery.calls == 1
    assert client.heartbeats == 1
    queue.close()


def test_discovery_failure_isolated_from_heartbeat(tmp_path, caplog):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    client, discovery = Transport(), Discovery(fail=True)
    runtime = EdgeRuntime(queue, client, discovery=discovery,
                          clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime.run_once()
    assert client.heartbeats == 1
    assert "private credential text" not in caplog.text
    queue.close()
