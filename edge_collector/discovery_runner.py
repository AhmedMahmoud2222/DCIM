"""One neighbor-discovery cycle over a collector's discovery plan.

For each plan item (one integration) the runner opens a session, scans the neighbor
protocols the profile enables and enqueues the observations for store-and-forward delivery.
Every item is isolated: a missing credential, an unreachable device or a parser fault is
recorded against that item and the next item still runs. Results carry error *class names*
only, never exception text, so credentials and device-supplied strings cannot reach logs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .neighbors import Walker, build_queue_records, collect_neighbors
from .queue import SQLiteQueue
from .snmp import SNMPTarget

SessionFactory = Callable[[Mapping[str, Any], SNMPTarget], Walker]


@dataclass(slots=True)
class ItemResult:
    integration_id: str
    observed: int = 0
    enqueued: int = 0
    malformed_rows: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CycleResult:
    items: list[ItemResult] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.failures)


def run_discovery_cycle(
    plan: list[dict[str, Any]], session_factory: SessionFactory, queue: SQLiteQueue, *,
    clock: Callable[[], datetime] | None = None,
) -> CycleResult:
    now = clock or (lambda: datetime.now(UTC))
    result = CycleResult()
    for item in plan:
        integration_id = str(item.get("integration_id", ""))
        outcome = ItemResult(integration_id)
        result.items.append(outcome)
        profile = item.get("plan")
        if not isinstance(profile, dict) or not integration_id:
            outcome.failures.append("NoProfilePlan")
            continue
        started = now()
        try:
            session = session_factory(item, SNMPTarget(str(item["target_host"]), int(item.get("target_port") or 161)))
            scans = collect_neighbors(session, profile)
        except Exception as error:  # noqa: BLE001 - isolate each device; only the class name is kept
            outcome.failures.append(type(error).__name__)
            continue
        finished = now()
        for scan in scans:
            outcome.observed += len(scan.observations)
            outcome.malformed_rows += scan.malformed_rows
            if scan.failure:
                outcome.failures.append(f"{scan.protocol}:{scan.failure}")
        for record in build_queue_records(
            scans, integration_id=integration_id, external_identifier=str(item.get("target_host", "")),
            scan_id=uuid.uuid4().hex, scan_started_at=started, scan_finished_at=max(finished, started),
        ):
            if queue.enqueue(record):
                outcome.enqueued += 1
    return result
