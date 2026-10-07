"""Exercise runtime -> scheduler -> real discovery runner -> durable SQLite queue."""
from datetime import datetime, timezone
from threading import Event

import pytest

from edge_collector.config import CollectorConfig
from edge_collector.credentials import CredentialStoreError, LocalCredentialStore
from edge_collector.discovery_scheduler import ScheduledDiscovery
from edge_collector.discovery_sessions import BoundedWalker, local_session_factory
from edge_collector.queue import SQLiteQueue
from edge_collector.runtime import EdgeRuntime
from edge_collector.snmp import SNMPTarget, SNMPTargetPolicy, SNMPTimeoutError
from edge_collector.snmp_wire import TAG_OCTET_STRING, Varbind, WalkResult

PLAN = {
    "neighbor_discovery": {
        "lldp": {"enabled": True, "table_oid": "1.3.6.1", "columns": {"chassis_id": 5},
                 "index_fields": ["time_mark", "local_port_num", "rem_index"]},
        "cdp": {"enabled": True, "table_oid": "1.3.6.2", "columns": {"device_id": 6},
                "index_fields": ["if_index", "device_index"]},
    },
    "neighbor_behavior": {"lldp": {"enabled": True}, "cdp": {"enabled": True}},
}


def item(key="i"):
    return {"integration_id": key, "target_host": "192.0.2.1", "target_port": 161,
            "poll_interval_seconds": 60, "snmp_version": "v2c", "plan": PLAN}


class EmptyWalker:
    def __init__(self):
        self.calls = []

    def walk(self, oid, **kwargs):
        self.calls.append(oid)
        return WalkResult((), False)


def finish_task(scheduler):
    assert scheduler._future is not None
    scheduler._future.result(timeout=5)


def test_runtime_schedules_real_cycle_and_buffers_on_the_queue_thread(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    walker = EmptyWalker()
    now = [1.0]
    scheduler = ScheduledDiscovery(lambda: [item()], lambda *_: walker, clock=lambda: now[0])

    class Client:
        def heartbeat(self, **kwargs):
            pass

    runtime = EdgeRuntime(queue, Client(), discovery=scheduler)
    try:
        runtime.run_once()  # fetch plan asynchronously
        finish_task(scheduler)
        runtime.run_once()  # accept plan and start scan
        finish_task(scheduler)
        runtime.run_once()  # enqueue scan results on this thread
        assert walker.calls == ["1.3.6.1", "1.3.6.2"]
        assert queue.metrics().count == 2
        assert {r.payload["record_type"] for r in queue.list_due(datetime.now(timezone.utc))} == {"neighbor_scan"}
        assert scheduler._future is None
        scheduler.tick(queue)
        assert len(walker.calls) == 2  # not due: no unbounded polling
    finally:
        scheduler.close()
        queue.close()


def test_busy_worker_does_not_block_heartbeat_or_accumulate_tasks(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    started, release = Event(), Event()

    def load():
        started.set()
        assert release.wait(5)
        return []

    scheduler = ScheduledDiscovery(load, lambda *_: EmptyWalker())

    class Client:
        heartbeats = 0

        def heartbeat(self, **kwargs):
            self.heartbeats += 1

    client = Client()
    runtime = EdgeRuntime(queue, client, discovery=scheduler)
    try:
        runtime.run_once()
        assert started.wait(5)
        future = scheduler._future
        for _ in range(20):
            runtime.run_once()
            assert scheduler._future is future
        assert client.heartbeats == 1
    finally:
        release.set()
        scheduler.close()
        queue.close()


def test_failed_device_does_not_stop_next_device_or_leak(tmp_path, caplog):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    walker = EmptyWalker()

    def factory(plan, target):
        if plan["integration_id"] == "a":
            raise RuntimeError("private password")
        return walker

    scheduler = ScheduledDiscovery(lambda: [item("a"), item("b")], factory, clock=lambda: 1.0)
    try:
        scheduler.tick(queue)
        finish_task(scheduler)
        scheduler.tick(queue)
        finish_task(scheduler)
        scheduler.tick(queue)
        finish_task(scheduler)
        scheduler.tick(queue)
        assert queue.metrics().count == 2
        assert len(walker.calls) == 2
        assert "private password" not in caplog.text
    finally:
        scheduler.close()
        queue.close()


def test_offline_plan_expires_and_late_results_are_discarded(tmp_path):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    now = [1.0]
    scheduler = ScheduledDiscovery(lambda: [item()], lambda *_: EmptyWalker(), clock=lambda: now[0])
    try:
        scheduler.tick(queue)
        finish_task(scheduler)
        scheduler.tick(queue)
        finish_task(scheduler)
        now[0] = 302.0
        scheduler.tick(queue)
        assert queue.metrics().count == 0
    finally:
        scheduler.close()
        queue.close()


@pytest.mark.parametrize("plan", [[item()] * 501, [item(), item()], [dict(item(), poll_interval_seconds=0)]])
def test_invalid_plans_fail_closed(tmp_path, plan):
    queue = SQLiteQueue(CollectorConfig(tmp_path / "q.db"))
    scheduler = ScheduledDiscovery(lambda: plan, lambda *_: EmptyWalker(), clock=lambda: 1.0)
    try:
        scheduler.tick(queue)
        finish_task(scheduler)
        scheduler.tick(queue)
        assert scheduler.failed
        assert scheduler._plan == {}
        assert scheduler._future is None
    finally:
        scheduler.close()
        queue.close()


def test_local_credentials_cannot_downgrade_v3_to_v2c():
    factory = local_session_factory(LocalCredentialStore({"i": {"community": "private"}}), SNMPTargetPolicy())
    with pytest.raises(CredentialStoreError, match="do not match"):
        factory(dict(item(), snmp_version="v3"), SNMPTarget("192.0.2.1"))
    with pytest.raises(CredentialStoreError, match="numeric"):
        factory(item(), SNMPTarget("unbounded-dns.example"))


def test_walk_deadline_is_independent_for_each_protocol():
    now = [0.0]

    class Session:
        def get_bulk(self, oid, repetitions):
            now[0] += 21.0
            return [Varbind(oid + ".1", TAG_OCTET_STRING, b"x")]

    walker = BoundedWalker(Session(), clock=lambda: now[0])
    for oid in ("1.3.6.1", "1.3.6.2"):
        with pytest.raises(SNMPTimeoutError, match="deadline"):
            walker.walk(oid)
    assert now[0] == 42.0
