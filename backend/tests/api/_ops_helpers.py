"""Shared builders for Issue #103 tests: integrations, rules, alarms, assignments, a dual-fed power plant."""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.secrets import encrypt_secret
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import Collector, CollectorAssignment, Integration

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def at(seconds_ago: float, base: datetime = NOW) -> datetime:
    return base - timedelta(seconds=seconds_ago)


async def make_integration(db, *, site_id=None, name=None) -> Integration:
    i = Integration(
        id=uuid.uuid4(), name=name or f"int-{uuid.uuid4().hex[:8]}", integration_type="snmp", site_id=site_id,
        target_host="192.0.2.10", config={}, poll_interval_seconds=60, enabled=True, version=1,
    )
    db.add(i)
    await db.commit()
    return i


async def make_rule(db, integration, *, rule_type="availability_unavailable", metric="availability") -> AlarmRule:
    r = AlarmRule(
        id=uuid.uuid4(), integration_id=integration.id, metric=metric, rule_type=rule_type,
        threshold=None if rule_type == "availability_unavailable" else 80, name=f"rule-{uuid.uuid4().hex[:6]}", enabled=True,
    )
    db.add(r)
    await db.commit()
    return r


async def make_alarm(db, integration, rule, *, opened_at, asset_id=None, status="ACTIVE", subject=None) -> Alarm:
    a = Alarm(
        id=uuid.uuid4(), rule_id=rule.id, integration_id=integration.id, managed_asset_id=asset_id,
        subject_key=subject or f"subject-{uuid.uuid4().hex[:8]}", status=status, opened_at=opened_at, last_value=1,
        details={"metric": rule.metric},
    )
    db.add(a)
    await db.commit()
    return a


async def make_collector(db, *, site_id, name=None, created=None) -> Collector:
    c = Collector(
        id=uuid.uuid4(), name=name or f"col-{uuid.uuid4().hex[:6]}", collector_type="edge", site_id=site_id, status="active",
        secret_ciphertext=encrypt_secret("s"), secret_rotated_at=NOW, created_at=created or NOW - timedelta(days=30),
    )
    db.add(c)
    await db.commit()
    return c


async def assign(db, collector, integration) -> None:
    db.add(
        CollectorAssignment(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, effective_from=NOW - timedelta(days=1)
        )
    )
    await db.commit()
