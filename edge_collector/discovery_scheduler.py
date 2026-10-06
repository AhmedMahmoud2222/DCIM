"""Single-worker discovery scheduling, with no task backlog or cross-thread SQLite use."""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .discovery_runner import SessionFactory, run_discovery_cycle
from .queue import QueueRecord, SQLiteQueue


class _Buffer:
    def __init__(self) -> None:
        self.records: list[QueueRecord] = []

    def enqueue(self, record: QueueRecord) -> bool:
        if len(self.records) >= 8194:
            raise ValueError("discovery result exceeds its budget")
        self.records.append(record)
        return True


class ScheduledDiscovery:
    """Refresh every minute, expire offline plans after five minutes, poll one device at a time.

    tick() never waits for network I/O. Only the runtime thread writes the durable queue.
    A scan result is discarded if its assignment expired while the worker was busy.
    The supplied session factory must provide bounded network operations.
    """

    def __init__(
        self, load_plan: Callable[[], list[dict[str, Any]]], session_factory: SessionFactory, *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load_plan = load_plan
        self._session_factory = session_factory
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery")
        self._future: Future | None = None
        self._kind = ""
        self._plan: dict[str, dict[str, Any]] = {}
        self._due: dict[str, float] = {}
        self._refresh_at = 0.0
        self._expires_at = 0.0
        self._closed = False
        self.failed = False

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def tick(self, queue: SQLiteQueue) -> None:
        if self._closed:
            return
        now = self._clock()
        if self._future is not None:
            if not self._future.done():
                return
            future, self._future = self._future, None
            try:
                result = future.result()
                if self._kind == "plan":
                    self._accept_plan(result, now)
                elif now < self._expires_at:
                    for record in result:
                        queue.enqueue(record)
                self.failed = False
            except Exception:  # noqa: BLE001 - never retain or log exception messages/credentials
                self.failed = True
        if now >= self._refresh_at:
            self._refresh_at = now + 60.0
            self._kind = "plan"
            self._future = self._executor.submit(self._load_plan)
            return
        if now >= self._expires_at:
            return
        ready = sorted((due, key) for key, due in self._due.items() if due <= now)
        if ready:
            _, key = ready[0]
            item = self._plan[key]
            self._due[key] = now + item["poll_interval_seconds"]
            self._kind = "scan"
            self._future = self._executor.submit(self._scan, item)

    def _accept_plan(self, result: object, now: float) -> None:
        if not isinstance(result, list) or len(result) > 500:
            raise ValueError("invalid discovery plan")
        plan = {}
        for item in result:
            if not isinstance(item, dict):
                raise ValueError("invalid discovery plan")
            key = item.get("integration_id")
            interval = item.get("poll_interval_seconds")
            if (
                not isinstance(key, str) or not key or key in plan
                or type(interval) is not int or not 60 <= interval <= 1800
            ):
                raise ValueError("invalid discovery plan")
            if isinstance(item.get("plan"), dict):
                plan[key] = item
        self._plan = plan
        self._due = {key: self._due.get(key, now) for key in plan}
        self._expires_at = now + 300.0

    def _scan(self, item: dict[str, Any]) -> list[QueueRecord]:
        buffer = _Buffer()
        run_discovery_cycle([item], self._session_factory, buffer)
        return buffer.records
