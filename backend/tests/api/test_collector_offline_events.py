"""Issue #103 area B: deterministic collector offline/online transitions."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import collector_health_sweep as sweep
from app.application.collector_service import HEARTBEAT_OFFLINE_AFTER_SECONDS as OFFLINE
from app.application.notification_service import MAX_ATTEMPTS
from app.core.secrets import encrypt_secret
from app.domain.integration.models import Collector, CollectorHeartbeat
from app.domain.operations.models import (
    CollectorState,
    CollectorTransition,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
)
from tests.api._phase3_helpers import create_room_and_site

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def at(seconds_ago: float) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


# ------------------------------------------------------------------ pure decision boundaries


@pytest.mark.parametrize(
    ("age", "expected"),
    [(0, "online"), (299.999, "online"), (300.0, "online"), (300.001, "offline"), (301, "offline"), (3600, "offline")],
)
def test_threshold_boundary_matches_collector_health_classification(age, expected):
    assert OFFLINE == 300
    got = sweep.desired_state(
        current="online", last_heartbeat_at=at(age), offline_basis_heartbeat_at=None, registered_at=at(10_000), now=NOW
    )
    assert got == expected


def test_never_seen_collector_is_judged_by_registration_age():
    kw = {"current": None, "last_heartbeat_at": None, "offline_basis_heartbeat_at": None, "now": NOW}
    assert sweep.desired_state(registered_at=at(120), **kw) == "online"
    assert sweep.desired_state(registered_at=at(OFFLINE), **kw) == "online"
    assert sweep.desired_state(registered_at=at(OFFLINE + 1), **kw) == "offline"


def test_recovery_requires_a_strictly_newer_recent_heartbeat():
    basis = at(900)
    kw = {"current": "offline", "offline_basis_heartbeat_at": basis, "registered_at": at(10_000), "now": NOW}
    assert sweep.desired_state(last_heartbeat_at=basis, **kw) == "offline"  # the same heartbeat again
    assert sweep.desired_state(last_heartbeat_at=basis - timedelta(seconds=1), **kw) == "offline"  # older
    assert sweep.desired_state(last_heartbeat_at=None, **kw) == "offline"
    assert sweep.desired_state(last_heartbeat_at=at(5), **kw) == "online"
    assert sweep.desired_state(last_heartbeat_at=at(OFFLINE + 1), **kw) == "offline"  # newer than basis but too old
    assert sweep.desired_state(last_heartbeat_at=NOW + timedelta(seconds=sweep.CLOCK_SKEW_SECONDS + 1), **kw) == "offline"
    assert sweep.desired_state(last_heartbeat_at=NOW + timedelta(seconds=5), **kw) == "online"  # within skew


# ------------------------------------------------------------------------- database behaviour


async def make_collector(db, *, site_id, name=None, status="active", created=None) -> Collector:
    c = Collector(
        id=uuid.uuid4(), name=name or f"col-{uuid.uuid4().hex[:8]}", collector_type="edge", site_id=site_id, status=status,
        secret_ciphertext="x", secret_rotated_at=NOW, created_at=created or at(100_000),
    )
    db.add(c)
    await db.commit()
    return c


async def beat(db, collector, seconds_ago):
    db.add(CollectorHeartbeat(id=uuid.uuid4(), collector_id=collector.id, ts=at(seconds_ago), status="ok"))
    await db.commit()


async def counts(db, collector_id):
    t = (await db.execute(select(func.count()).select_from(CollectorTransition).where(CollectorTransition.collector_id == collector_id))).scalar_one()
    o = (
        await db.execute(
            text("select count(*) from outbox_event where aggregate_id = :i and event_type in ('CollectorOffline','CollectorOnline')"),
            {"i": collector_id},
        )
    ).scalar_one()
    a = (await db.execute(text("select count(*) from audit_log where entity_id = :i and action like 'collector.state.%'"), {"i": collector_id})).scalar_one()
    return t, o, a


@pytest.mark.asyncio
async def test_healthy_collector_gets_a_baseline_and_no_event(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    c = await make_collector(db_session, site_id=uuid.UUID(site))
    await beat(db_session, c, 10)
    assert await sweep.sweep_collector_states(db_session, now=NOW) == []
    state = await db_session.get(CollectorState, c.id)
    assert state.state == "online" and state.generation == 1
    assert await counts(db_session, c.id) == (0, 0, 0)


@pytest.mark.asyncio
async def test_offline_then_recovery_emit_one_event_each_and_repeat_sweeps_are_silent(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    c = await make_collector(db_session, site_id=uuid.UUID(site), name="edge-a")
    await beat(db_session, c, 20)
    await sweep.sweep_collector_states(db_session, now=NOW - timedelta(seconds=10))  # baseline online

    # exactly at the threshold: still online, no event
    later = NOW + timedelta(seconds=OFFLINE - 20)  # heartbeat is 300 s old
    assert await sweep.sweep_collector_states(db_session, now=later) == []
    # just after: offline
    r = await sweep.sweep_collector_states(db_session, now=later + timedelta(seconds=1))
    assert [(x.from_state, x.to_state, x.generation) for x in r] == [("online", "offline", 2)]
    assert await counts(db_session, c.id) == (1, 1, 1)

    for i in range(3):  # repeated sweeps while still offline
        assert await sweep.sweep_collector_states(db_session, now=later + timedelta(seconds=60 * (i + 1))) == []
    assert await counts(db_session, c.id) == (1, 1, 1)

    # a late-arriving OLD heartbeat must not revive it
    await beat(db_session, c, 500)
    assert await sweep.sweep_collector_states(db_session, now=later + timedelta(seconds=300)) == []
    state = await db_session.get(CollectorState, c.id)
    assert state.state == "offline"

    # a genuinely new heartbeat does
    recovery_now = later + timedelta(seconds=600)
    db_session.add(CollectorHeartbeat(id=uuid.uuid4(), collector_id=c.id, ts=recovery_now - timedelta(seconds=5), status="ok"))
    await db_session.commit()
    r = await sweep.sweep_collector_states(db_session, now=recovery_now)
    assert [(x.from_state, x.to_state, x.generation) for x in r] == [("offline", "online", 3)]
    assert await sweep.sweep_collector_states(db_session, now=recovery_now + timedelta(seconds=30)) == []
    assert await counts(db_session, c.id) == (2, 2, 2)

    rows = (await db_session.execute(select(CollectorTransition).where(CollectorTransition.collector_id == c.id).order_by(CollectorTransition.generation))).scalars().all()
    assert [(t.from_state, t.to_state) for t in rows] == [("online", "offline"), ("offline", "online")]
    assert rows[0].collector_name == "edge-a" and rows[0].site_id == uuid.UUID(site)
    assert rows[0].correlation_id == f"collector-health:{c.id}:2"
    ob = (await db_session.execute(text("select payload, correlation_id from outbox_event where event_type = 'CollectorOffline' and aggregate_id = :i"), {"i": c.id})).one()
    assert ob.payload["site_id"] == site and ob.payload["to_state"] == "offline" and ob.correlation_id == rows[0].correlation_id


@pytest.mark.asyncio
async def test_never_heartbeat_collector_goes_offline_after_grace(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    c = await make_collector(db_session, site_id=uuid.UUID(site), created=at(OFFLINE + 1))
    r = await sweep.sweep_collector_states(db_session, now=NOW)
    assert [(x.from_state, x.to_state) for x in r] == [("unknown", "offline")]
    t = (await db_session.execute(select(CollectorTransition).where(CollectorTransition.collector_id == c.id))).scalar_one()
    assert t.never_heartbeat is True and t.heartbeat_at is None


@pytest.mark.asyncio
async def test_inactive_collectors_are_ignored_and_sites_stay_separate(client, auth_headers, db_session):
    _, site_a = await create_room_and_site(client, auth_headers)
    _, site_b = await create_room_and_site(client, auth_headers)
    quiet = await make_collector(db_session, site_id=uuid.UUID(site_a), name="quiet")
    healthy = await make_collector(db_session, site_id=uuid.UUID(site_b), name="healthy")
    disabled = await make_collector(db_session, site_id=uuid.UUID(site_a), name="off", status="disabled")
    await beat(db_session, quiet, 1000)
    await beat(db_session, healthy, 5)
    await beat(db_session, disabled, 5000)
    r = await sweep.sweep_collector_states(db_session, now=NOW)
    assert {(x.collector_id, x.to_state) for x in r} == {(quiet.id, "offline")}
    t = (await db_session.execute(select(CollectorTransition))).scalar_one()
    assert t.site_id == uuid.UUID(site_a) and t.collector_name == "quiet"
    assert await db_session.get(CollectorState, healthy.id) is not None
    assert await db_session.get(CollectorState, disabled.id) is None


@pytest.mark.asyncio
async def test_concurrent_sweeps_record_one_transition(client, auth_headers, db_session, db_engine):
    _, site = await create_room_and_site(client, auth_headers)
    c = await make_collector(db_session, site_id=uuid.UUID(site))
    await beat(db_session, c, 2000)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def run():
        async with factory() as s:
            return await sweep.sweep_collector_states(s, now=NOW)

    results = await asyncio.gather(*[run() for _ in range(4)])
    assert sum(len(r) for r in results) == 1
    assert await counts(db_session, c.id) == (1, 1, 1)


@pytest.mark.asyncio
async def test_unique_generation_is_the_database_backstop(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    c = await make_collector(db_session, site_id=uuid.UUID(site))
    await beat(db_session, c, 2000)
    await sweep.sweep_collector_states(db_session, now=NOW)
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        CollectorTransition(
            id=uuid.uuid4(), collector_id=c.id, collector_name="x", generation=1, from_state="online", to_state="offline",
            detected_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def make_policy(db, *, site_id=None, events=("collector.offline", "collector.online")):
    ch = NotificationChannel(
        id=uuid.uuid4(), name=f"ch-{uuid.uuid4().hex[:6]}", kind="webhook", url_display="https://hooks.example",
        url_ciphertext=encrypt_secret("https://hooks.example/h/secret-token"), secret_ciphertext=encrypt_secret("sign-key"),
        enabled=True, version=1,
    )
    db.add(ch)
    await db.flush()
    pol = NotificationPolicy(
        id=uuid.uuid4(), name=f"pol-{uuid.uuid4().hex[:6]}", channel_id=ch.id, event_types=list(events), site_id=site_id,
        enabled=True, version=1,
    )
    db.add(pol)
    await db.commit()
    return pol


@pytest.mark.asyncio
async def test_transition_queues_one_notification_per_policy_and_replays_add_none(client, auth_headers, db_session):
    _, site = await create_room_and_site(client, auth_headers)
    _, other = await create_room_and_site(client, auth_headers)
    matching = await make_policy(db_session, site_id=uuid.UUID(site))
    await make_policy(db_session, site_id=uuid.UUID(other))  # different site: must not fire
    await make_policy(db_session, events=("incident.opened",))  # different event
    c = await make_collector(db_session, site_id=uuid.UUID(site))
    await beat(db_session, c, 2000)
    await sweep.sweep_collector_states(db_session, now=NOW)
    await sweep.sweep_collector_states(db_session, now=NOW + timedelta(seconds=60))
    rows = (await db_session.execute(select(NotificationDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].policy_id == matching.id
    d = rows[0]
    assert d.event_type == "collector.offline" and d.status == "pending" and d.max_attempts == MAX_ATTEMPTS
    assert d.payload["collector_id"] == str(c.id) and d.payload["site_id"] == site
    assert "secret-token" not in str(d.payload)
