"""SEC-05 retention checks against the disposable PostgreSQL test database."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.application import collector_auth
from app.core.config import Settings
from app.core.secrets import encrypt_secret
from app.domain.integration.models import Collector, CollectorHeartbeat, CollectorRequestNonce
from app.infrastructure.celery_app import celery_app
from app.infrastructure.tasks import maintenance

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


async def _collector(db_session, *, secret="test-collector-secret"):
    collector = Collector(
        id=uuid.uuid4(), name=f"sec05-{uuid.uuid4().hex}",
        collector_type="central", status="active",
        secret_ciphertext=encrypt_secret(secret), secret_rotated_at=NOW,
    )
    db_session.add(collector)
    await db_session.flush()
    return collector


async def _seed(db_session, collector, *, nonce_times=(), heartbeat_times=()):
    for seen_at in nonce_times:
        db_session.add(CollectorRequestNonce(
            id=uuid.uuid4(), collector_id=collector.id,
            nonce=uuid.uuid4().hex, seen_at=seen_at,
        ))
    for ts in heartbeat_times:
        db_session.add(CollectorHeartbeat(
            id=uuid.uuid4(), collector_id=collector.id, ts=ts, status="ok",
        ))
    await db_session.commit()


async def _remaining(db_session, model, column):
    db_session.expire_all()
    return (await db_session.execute(select(column).select_from(model).order_by(column))).scalars().all()


@pytest.mark.asyncio
async def test_strict_cutoffs_repeated_execution_and_zero_rows(db_session):
    collector = await _collector(db_session)
    nonce_cutoff = NOW - timedelta(seconds=3600)
    heartbeat_cutoff = NOW - timedelta(days=30)
    await _seed(
        db_session, collector,
        nonce_times=[
            nonce_cutoff - timedelta(microseconds=1),
            nonce_cutoff,
            nonce_cutoff + timedelta(microseconds=1),
            NOW - timedelta(seconds=300),
        ],
        heartbeat_times=[
            heartbeat_cutoff - timedelta(microseconds=1),
            heartbeat_cutoff,
            heartbeat_cutoff + timedelta(microseconds=1),
        ],
    )
    result = maintenance.prune_expired_collector_rows(now=NOW)
    assert result == {"deleted_nonces": 1, "deleted_heartbeats": 1, "batch_limit_reached": False}
    assert await _remaining(db_session, CollectorRequestNonce, CollectorRequestNonce.seen_at) == [
        nonce_cutoff, nonce_cutoff + timedelta(microseconds=1), NOW - timedelta(seconds=300),
    ]
    assert await _remaining(db_session, CollectorHeartbeat, CollectorHeartbeat.ts) == [
        heartbeat_cutoff, heartbeat_cutoff + timedelta(microseconds=1),
    ]
    assert maintenance.prune_expired_collector_rows(now=NOW) == {
        "deleted_nonces": 0, "deleted_heartbeats": 0, "batch_limit_reached": False,
    }


@pytest.mark.asyncio
async def test_cleanup_keeps_in_window_claim_and_replay_is_rejected(db_session, monkeypatch):
    class FrozenDateTime(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(collector_auth, "datetime", FrozenDateTime)
    collector = await _collector(db_session)
    await db_session.commit()
    timestamp = str(int(NOW.timestamp()))
    nonce = uuid.uuid4().hex
    body = b"{}"
    signature = collector_auth.compute_signature(
        secret="test-collector-secret", collector_id=collector.id,
        timestamp=timestamp, nonce=nonce, raw_body=body,
    )
    request = {
        "collector_id_header": str(collector.id), "timestamp_header": timestamp,
        "nonce_header": nonce, "signature_header": signature, "raw_body": body,
    }
    await collector_auth.verify_collector_request(db_session, **request)
    FrozenDateTime.current = NOW + timedelta(seconds=60)
    maintenance.prune_expired_collector_rows(now=FrozenDateTime.current)
    assert len(await _remaining(db_session, CollectorRequestNonce, CollectorRequestNonce.seen_at)) == 1
    with pytest.raises(collector_auth.CollectorAuthError, match="Nonce already used"):
        await collector_auth.verify_collector_request(db_session, **request)


@pytest.mark.asyncio
async def test_failed_second_delete_rolls_back_both_tables(db_session, monkeypatch):
    collector = await _collector(db_session)
    await _seed(
        db_session, collector,
        nonce_times=[NOW - timedelta(seconds=3601)],
        heartbeat_times=[NOW - timedelta(days=31)],
    )
    original = maintenance._delete_batch

    def fail_heartbeat(conn, table, timestamp_column, cutoff):
        if table.name == "collector_heartbeat":
            raise RuntimeError("synthetic database fault")
        return original(conn, table, timestamp_column, cutoff)

    monkeypatch.setattr(maintenance, "_delete_batch", fail_heartbeat)
    with pytest.raises(RuntimeError, match="Collector retention batch failed"):
        maintenance.prune_expired_collector_rows(now=NOW)
    assert len(await _remaining(db_session, CollectorRequestNonce, CollectorRequestNonce.id)) == 1
    assert len(await _remaining(db_session, CollectorHeartbeat, CollectorHeartbeat.id)) == 1
    monkeypatch.setattr(maintenance, "_delete_batch", original)
    assert maintenance.prune_expired_collector_rows(now=NOW)["deleted_nonces"] == 1


@pytest.mark.asyncio
async def test_overlapping_workers_skip_locked_and_do_not_double_count(db_session):
    collector = await _collector(db_session)
    await _seed(
        db_session, collector,
        nonce_times=[NOW - timedelta(seconds=3601)] * 25,
        heartbeat_times=[NOW - timedelta(days=31)] * 25,
    )
    first, second = await asyncio.gather(
        asyncio.to_thread(maintenance.prune_expired_collector_rows, now=NOW),
        asyncio.to_thread(maintenance.prune_expired_collector_rows, now=NOW),
    )
    assert first["deleted_nonces"] + second["deleted_nonces"] == 25
    assert first["deleted_heartbeats"] + second["deleted_heartbeats"] == 25
    assert await _remaining(db_session, CollectorRequestNonce, CollectorRequestNonce.id) == []
    assert await _remaining(db_session, CollectorHeartbeat, CollectorHeartbeat.id) == []



@pytest.mark.asyncio
async def test_per_run_batch_cap_continues_on_retry(db_session, monkeypatch):
    collector = await _collector(db_session)
    await _seed(db_session, collector, nonce_times=[NOW - timedelta(seconds=3601)] * 5)
    monkeypatch.setattr(maintenance, "BATCH_SIZE", 2)
    monkeypatch.setattr(maintenance, "MAX_BATCHES_PER_RUN", 1)
    counts = []
    for _ in range(3):
        counts.append(maintenance.prune_expired_collector_rows(now=NOW))
    assert [run["deleted_nonces"] for run in counts] == [2, 2, 1]
    assert all(run["batch_limit_reached"] for run in counts)
    assert maintenance.prune_expired_collector_rows(now=NOW) == {
        "deleted_nonces": 0, "deleted_heartbeats": 0, "batch_limit_reached": False,
    }


@pytest.mark.asyncio
async def test_future_auth_window_larger_than_retention_fails_closed(db_session, monkeypatch):
    collector = await _collector(db_session)
    await _seed(db_session, collector, nonce_times=[NOW - timedelta(seconds=3601)])
    monkeypatch.setattr(maintenance, "REQUEST_TIMESTAMP_WINDOW_SECONDS", 4000)
    with pytest.raises(ValueError, match="exceed the request acceptance window"):
        maintenance.prune_expired_collector_rows(now=NOW)
    assert len(await _remaining(db_session, CollectorRequestNonce, CollectorRequestNonce.id)) == 1


def test_defaults_invalid_configuration_and_beat_registration():
    env = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://user:pass@localhost/test",
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret_key": "x" * 32,
        "credential_encryption_key": "x" * 44,
    }
    settings = Settings(**env)
    assert (settings.nonce_retention_seconds, settings.heartbeat_retention_days) == (3600, 30)
    for field, value in [("nonce_retention_seconds", 3599), ("heartbeat_retention_days", 29)]:
        with pytest.raises(ValidationError):
            Settings(**env, **{field: value})
    schedule = celery_app.conf.beat_schedule["prune-collector-nonces-and-heartbeats"]
    assert schedule["task"] == maintenance.prune_collector_nonces_and_heartbeats.name
    assert schedule["schedule"] == 3600.0
    assert schedule["options"]["queue"] == "maintenance"
    assert "app.infrastructure.tasks.maintenance" in celery_app.conf.imports


@pytest.mark.asyncio
async def test_timestamp_indexes_support_global_retention_queries(db_session):
    # Existing indexes start with collector_id, which cannot serve a global range
    # scan. Force the index path on a disposable DB to verify the migration's indexes.
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    for table, timestamp, index in [
        ("collector_request_nonce", "seen_at", "ix_collector_request_nonce_seen_at"),
        ("collector_heartbeat", "ts", "ix_collector_heartbeat_ts"),
    ]:
        plan = (await db_session.execute(text(
            f"EXPLAIN (COSTS OFF) SELECT id FROM {table} "
            f"WHERE {timestamp} < :cutoff ORDER BY {timestamp} LIMIT 500"
        ), {"cutoff": NOW})).scalars().all()
        assert index in " ".join(plan)
    await db_session.rollback()
