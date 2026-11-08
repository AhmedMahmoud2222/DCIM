"""Test-support driver for the Issue #103 browser E2E (never used by the application).

The browser suite needs three things the public API deliberately does not offer: a collector whose last
heartbeat is old enough to be offline, alarms that the telemetry pipeline would have opened, and a way to run
the scheduled sweeps now instead of waiting for Celery beat. This script does exactly those, against the
disposable E2E database only, and prints one JSON object so the spec can read ids back.

Usage (from backend/, with the same environment as the API):
    python scripts/e2e_ops_driver.py seed --site-id <uuid> --label <text>
    python scripts/e2e_ops_driver.py heartbeat --collector-id <uuid> --age-seconds 0
    python scripts/e2e_ops_driver.py release-retries
    python scripts/e2e_ops_driver.py tick
"""

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

import app.db.models  # noqa: F401  (registers every mapped table before the first flush)
from app.core.secrets import encrypt_secret
from app.db.session import AsyncSessionLocal
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import Collector, CollectorAssignment, CollectorHeartbeat, Integration
from app.domain.operations.models import ItsmTicket, NotificationDelivery


async def seed(site_id: uuid.UUID, label: str) -> dict:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        collector = Collector(
            id=uuid.uuid4(),
            name=f"e2e-edge-{label}",
            collector_type="edge",
            site_id=site_id,
            status="active",
            secret_ciphertext=encrypt_secret("e2e-not-a-real-secret"),
            secret_rotated_at=now,
            created_at=now - timedelta(days=1),
        )
        integration = Integration(
            id=uuid.uuid4(),
            name=f"e2e-int-{label}",
            integration_type="snmp",
            site_id=site_id,
            target_host="192.0.2.50",
            config={},
            poll_interval_seconds=60,
            enabled=True,
            version=1,
        )
        db.add_all([collector, integration])
        await db.flush()
        rule = AlarmRule(
            id=uuid.uuid4(),
            integration_id=integration.id,
            metric="availability",
            rule_type="availability_unavailable",
            name=f"e2e-unavailable-{label}",
            enabled=True,
        )
        db.add(rule)
        db.add(
            CollectorAssignment(
                id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, effective_from=now - timedelta(days=1)
            )
        )
        db.add(CollectorHeartbeat(id=uuid.uuid4(), collector_id=collector.id, ts=now - timedelta(minutes=10), status="ok"))
        await db.flush()
        alarm_ids = []
        for i in range(2):
            a = Alarm(
                id=uuid.uuid4(),
                rule_id=rule.id,
                integration_id=integration.id,
                subject_key=f"e2e-{label}.device-{i}.availability",
                status="ACTIVE",
                opened_at=now - timedelta(seconds=30 * (i + 1)),
                last_value=0,
                details={"metric": "availability"},
            )
            db.add(a)
            alarm_ids.append(str(a.id))
        await db.commit()
        return {"collector_id": str(collector.id), "collector_name": collector.name, "alarm_ids": alarm_ids}


async def heartbeat(collector_id: uuid.UUID, age_seconds: int) -> dict:
    async with AsyncSessionLocal() as db:
        db.add(
            CollectorHeartbeat(
                id=uuid.uuid4(), collector_id=collector_id, ts=datetime.now(UTC) - timedelta(seconds=age_seconds), status="ok"
            )
        )
        await db.commit()
    return {"ok": True}


async def release_retries() -> dict:
    """Make retries due now so the E2E does not wait out real backoff."""
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        n = (
            await db.execute(
                update(NotificationDelivery).where(NotificationDelivery.status == "retry").values(next_attempt_at=now)
            )
        ).rowcount
        t = (await db.execute(update(ItsmTicket).where(ItsmTicket.status == "retry").values(next_attempt_at=now))).rowcount
        await db.commit()
    return {"deliveries": n, "tickets": t}


def tick() -> dict:
    from app.infrastructure.tasks import itsm, notifications

    return {
        "transitions": notifications.sweep_collector_health(),
        "correlated": notifications.correlate_events(),
        "redispatched_notifications": notifications.redispatch_due_notifications(),
        "redispatched_tickets": itsm.redispatch_due_tickets(),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--site-id", required=True)
    s.add_argument("--label", required=True)
    h = sub.add_parser("heartbeat")
    h.add_argument("--collector-id", required=True)
    h.add_argument("--age-seconds", type=int, default=0)
    sub.add_parser("release-retries")
    sub.add_parser("tick")
    args = p.parse_args()
    if args.cmd == "seed":
        out = asyncio.run(seed(uuid.UUID(args.site_id), args.label))
    elif args.cmd == "heartbeat":
        out = asyncio.run(heartbeat(uuid.UUID(args.collector_id), args.age_seconds))
    elif args.cmd == "release-retries":
        out = asyncio.run(release_retries())
    else:
        out = tick()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
