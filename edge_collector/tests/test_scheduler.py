from datetime import datetime, timedelta, timezone

import pytest

from edge_collector.scheduler import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_PRESETS,
    PollSchedule,
    stable_jitter_seconds,
)

UTC = timezone.utc


def test_default_and_allowed_intervals():
    assert DEFAULT_POLL_INTERVAL_SECONDS == 300
    assert POLL_INTERVAL_PRESETS == {60, 180, 300, 600, 900, 1800}
    with pytest.raises(ValueError):
        stable_jitter_seconds("x", 301)


def test_one_inflight_poll_and_stable_bounded_jitter():
    now = datetime(2026, 9, 19, tzinfo=UTC)
    schedule = PollSchedule("integration-a")
    assert schedule.start(now)
    assert not schedule.start(now + timedelta(hours=1))
    schedule.finish()
    jitter = stable_jitter_seconds("integration-a", 300)
    assert stable_jitter_seconds("integration-a", 300) == jitter
    next_due = schedule.next_due_at()
    assert stable_jitter_seconds("integration-a", 300) == jitter
    assert next_due > now
    assert (next_due - now).total_seconds() <= 300
    assert not schedule.due(next_due - timedelta(microseconds=1))
    assert schedule.due(next_due)


def test_stable_phase_does_not_add_jitter_to_every_cycle():
    schedule = PollSchedule("integration-b")
    first = datetime(2026, 9, 19, tzinfo=UTC)
    assert schedule.start(first)
    schedule.finish()
    second = schedule.next_due_at()
    assert schedule.start(second)
    schedule.finish()
    third = schedule.next_due_at()
    assert (third - second).total_seconds() == 300
