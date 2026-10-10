from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from .client import AckResult, PermanentPayloadError, RetryableTransportError
from .queue import QueueRecord, SQLiteQueue
from .retry import RetryPolicy

# Upper bound on ingest requests one cycle may spend isolating a record Central refuses.
MAX_ISOLATION_REQUESTS = 24


class CentralTransport(Protocol):
    def flush(self, records: list[QueueRecord]) -> AckResult: ...

    def heartbeat(self, *, queue_depth: int, status: str = "ok") -> None: ...


class DiscoveryScheduler(Protocol):
    """A non-blocking scheduler; network acquisition runs outside the runtime thread."""

    def tick(self, queue: SQLiteQueue) -> None: ...


class EdgeRuntime:
    """One bounded store-and-forward cycle, suitable for a scheduler-owned loop."""

    def __init__(
        self,
        queue: SQLiteQueue,
        client: CentralTransport,
        *,
        retry_policy: RetryPolicy | None = None,
        discovery: DiscoveryScheduler | None = None,
        heartbeat_interval_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        random_value: Callable[[], float] | None = None,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self.queue = queue
        self.client = client
        self.discovery = discovery
        self.discovery_failed = False
        self.retry_policy = retry_policy or RetryPolicy()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_value = random_value or random.random
        self._last_heartbeat_at: datetime | None = None

    def run_once(self) -> None:
        now = self._clock()
        records = self.queue.list_due(now)
        if records:
            self._deliver(records, now)
        self._heartbeat_if_due(now)
        if self.discovery is not None:
            try:
                self.discovery.tick(self.queue)
            except Exception:  # noqa: BLE001 - acquisition must not stop delivery; never log secrets
                self.discovery_failed = True
            else:
                self.discovery_failed = False

    def _deliver(self, records: list[QueueRecord], now: datetime) -> None:
        """Flush `records`; a batch Central refuses outright is bisected so one poisoned record
        cannot block its neighbours. A single record refused this way is dropped (it can never
        succeed), and the request budget bounds the work a hostile queue can cause per cycle."""
        pending = [records]
        budget = MAX_ISOLATION_REQUESTS
        while pending:
            group = pending.pop()
            if budget <= 0:
                self._defer(group, now)
                continue
            budget -= 1
            try:
                acknowledgement = self.client.flush(group)
            except PermanentPayloadError:
                if len(group) == 1:
                    self.queue.acknowledge([group[0].record_id])
                else:
                    middle = len(group) // 2
                    pending.extend((group[middle:], group[:middle]))
            except RetryableTransportError:
                self._defer(group, now)
            else:
                self.queue.acknowledge(acknowledgement.acknowledged_ids)
                by_id = {record.record_id: record for record in group}
                self._defer([by_id[record_id] for record_id in acknowledgement.unacknowledged_ids], now)

    def _defer(self, records: list[QueueRecord], now: datetime) -> None:
        delayed_ids: dict[float, list[str]] = defaultdict(list)
        for record in records:
            delay = self.retry_policy.delay_for(record.attempts, random_value=self._random_value())
            delayed_ids[delay].append(record.record_id)
        for delay, record_ids in delayed_ids.items():
            self.queue.mark_retry(record_ids, now=now, delay_seconds=delay)

    def _heartbeat_if_due(self, now: datetime) -> None:
        if self._last_heartbeat_at is not None and (now - self._last_heartbeat_at).total_seconds() < self.heartbeat_interval_seconds:
            return
        metrics = self.queue.metrics()
        status = "degraded" if metrics.dropped or metrics.expired else "ok"
        try:
            self.client.heartbeat(queue_depth=metrics.count, status=status)
        except RetryableTransportError:
            return
        self._last_heartbeat_at = now
