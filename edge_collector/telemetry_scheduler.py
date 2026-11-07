"""Single-worker telemetry acquisition: poll contract-pinned OIDs and queue pinned readings for delivery.

Mirrors `ScheduledDiscovery`: `tick()` never waits for network I/O, one worker polls one device at a time, and only the
runtime thread writes the durable queue. Central's authenticated plans decide what is polled:

  * the discovery plan supplies each assigned integration's target and polling interval;
  * the telemetry-contracts plan supplies `(integration_id, source_identifier)` pins. `source_identifier` is the numeric
    OID to GET. A source without a pin is never polled, so an unpinned value cannot be queued.
  * a device that times out `MAX_CONSECUTIVE_TIMEOUTS` times in a row is skipped for the rest of that cycle.

`external_identifier` is the source OID. A Central `PortTelemetryBinding` with protocol `snmp` and the same `external_ref`
receives the reading in the rack overlay when its `equipment_id` is the asset the mapping names.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from .queue import QueueRecord, SQLiteQueue
from .snmp import SNMPTarget, SNMPTimeoutError
from .telemetry_contract import ContractBook, telemetry_record_payload

TELEMETRY_RECORD_TYPE = "telemetry"
MAX_PLAN_INTEGRATIONS = 500
# Consecutive timeouts after which the rest of a device's OIDs are skipped for this cycle, so one dead device costs
# at most this many per-OID timeouts instead of one per pinned OID.
MAX_CONSECUTIVE_TIMEOUTS = 2

# (item, oid) -> exact numeral text; raises on any device or transport fault.
Reader = Callable[[Mapping[str, Any], SNMPTarget, str], str]


class ScheduledTelemetry:
    def __init__(
        self, load_plan: Callable[[], list[dict[str, Any]]], load_contracts: Callable[[], ContractBook], reader: Reader, *,
        clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._load_plan = load_plan
        self._load_contracts = load_contracts
        self._reader = reader
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry")
        self._future: Future | None = None
        self._kind = ""
        self._plan: dict[str, dict[str, Any]] = {}
        self._book = ContractBook({})
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
                    self._accept_plans(result, now)
                elif now < self._expires_at:
                    for record in result:
                        queue.enqueue(record)
                self.failed = False
            except Exception:  # noqa: BLE001 - never retain or log exception messages/credentials
                self.failed = True
        if now >= self._refresh_at:
            self._refresh_at = now + 60.0
            self._kind = "plan"
            self._future = self._executor.submit(lambda: (self._load_plan(), self._load_contracts()))
            return
        if now >= self._expires_at:
            return
        ready = sorted((due, key) for key, due in self._due.items() if due <= now)
        if ready:
            _, key = ready[0]
            item = self._plan[key]
            self._due[key] = now + item["poll_interval_seconds"]
            self._kind = "scan"
            self._future = self._executor.submit(self._poll, item, self._book, self._wall_clock())

    def _accept_plans(self, result: object, now: float) -> None:
        plan_items, book = result  # type: ignore[misc]
        if not isinstance(plan_items, list) or len(plan_items) > MAX_PLAN_INTEGRATIONS or not isinstance(book, ContractBook):
            raise ValueError("invalid telemetry plan")
        plan: dict[str, dict[str, Any]] = {}
        for item in plan_items:
            key = item.get("integration_id") if isinstance(item, dict) else None
            interval = item.get("poll_interval_seconds") if isinstance(item, dict) else None
            if not isinstance(key, str) or not key or key in plan or type(interval) is not int or not 60 <= interval <= 1800:
                raise ValueError("invalid telemetry plan")
            plan[key] = item
        self._plan, self._book = plan, book
        self._due = {key: self._due.get(key, now) for key in plan}
        self._expires_at = now + 300.0

    def _poll(self, item: dict[str, Any], book: ContractBook, sampled_at: datetime) -> list[QueueRecord]:
        integration_id = str(item["integration_id"])
        target = SNMPTarget(str(item["target_host"]), int(item.get("target_port") or 161))
        records: list[QueueRecord] = []
        timeouts = 0
        for (pinned_integration, oid), _pin in sorted(book.pins.items()):
            if pinned_integration != str(uuid.UUID(integration_id)):
                continue
            if timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                break
            try:
                numeral = self._reader(item, target, oid)
                timeouts = 0
                payload = telemetry_record_payload(
                    book, integration_id=integration_id, source_identifier=oid, external_identifier=oid, value=numeral,
                )
            except Exception as error:  # noqa: BLE001 - one OID's fault must not stop its siblings
                timeouts = timeouts + 1 if isinstance(error, TimeoutError | SNMPTimeoutError) else 0
                continue
            payload["record_type"] = TELEMETRY_RECORD_TYPE
            records.append(QueueRecord(uuid.uuid4().hex, sampled_at, payload))
        return records
