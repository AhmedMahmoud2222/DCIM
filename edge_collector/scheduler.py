"""Edge-local polling schedule; WAN delivery failures never stop acquisition.

Each integration has one in-flight poll.  A slow poll is skipped until it completes;
the next due time is calculated from its stable integration offset, preventing an
unbounded backlog or a site-wide thundering herd.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

POLL_INTERVAL_PRESETS = frozenset((60, 180, 300, 600, 900, 1800))
DEFAULT_POLL_INTERVAL_SECONDS = 300


def stable_jitter_seconds(integration_id: str, interval_seconds: int) -> int:
    if interval_seconds not in POLL_INTERVAL_PRESETS:
        raise ValueError("unsupported polling interval")
    # bounded to 10% of interval; stable across restarts for predictable load.
    return int.from_bytes(
        hashlib.sha256(integration_id.encode()).digest()[:4], "big"
    ) % max(1, interval_seconds // 10)


@dataclass
class PollSchedule:
    integration_id: str
    interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    in_flight: bool = False
    last_started_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        if self.in_flight:
            return False
        if self.last_started_at is None:
            return True
        return now >= self.last_started_at + timedelta(
            seconds=self.interval_seconds
            + stable_jitter_seconds(self.integration_id, self.interval_seconds)
        )

    def start(self, now: datetime) -> bool:
        if not self.due(now):
            return False
        self.in_flight, self.last_started_at = True, now
        return True

    def finish(self) -> None:
        self.in_flight = False
