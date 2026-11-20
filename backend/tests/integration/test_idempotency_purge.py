"""DB-03 (#357): idempotency purge (disabled by default) and its PostgreSQL lock behaviour against a real,
disposable database. Matrix: T14-T18, T21, T25, T37, T43-T45 of the revised design.

The purge is exercised here only through explicit `Settings(idempotency_purge_enabled=True)` objects and a
test engine. No shipped configuration enables it."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import idempotency as idem
from app.application import idempotency_purge as purge
from app.core.config import Settings, get_settings
from tests.conftest import TEST_DATABASE_URL

ENDPOINT = "POST:/purge-test"
PRINCIPAL = "user:00000000-0000-0000-0000-00000000db05"


def _settings(**overrides) -> Settings:
    base = {"idempotency_purge_enabled": True, "idempotency_purge_pause_ms": 0}
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture
def sync_engine_():
    engine = create_engine(TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg"), future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    opened = []

    def new_session():
        session = factory()
        opened.append(session)
        return session

    try:
        yield new_session
    finally:
        for session in opened:
            await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def tag(sessions):
    """A unique key prefix per test; its rows are removed afterwards so other tests see a clean ledger."""
    prefix = f"purge-{uuid.uuid4()}"
    yield prefix
    cleanup = sessions()
    await cleanup.execute(text("DELETE FROM idempotency_key WHERE key LIKE :p"), {"p": prefix + "%"})
    await cleanup.commit()


async def _seed(session, key, *, expires_ago: timedelta, status="completed", generation=1):
    await session.execute(
        text(
            "INSERT INTO idempotency_key (id, principal, key, endpoint, request_hash, status, claim_generation,"
            " response_status, response_body, expires_at, created_at, updated_at) VALUES"
            " (gen_random_uuid(), :p, :k, :e, 'h', :s, :g, NULL, NULL, now() - make_interval(secs => :ago),"
            " now(), now())"
        ),
        {"p": PRINCIPAL, "k": key, "e": ENDPOINT, "s": status, "g": generation, "ago": expires_ago.total_seconds()},
    )
    await session.commit()


async def _keys(session, prefix) -> set[str]:
    rows = await session.execute(text("SELECT key FROM idempotency_key WHERE key LIKE :p"), {"p": prefix + "%"})
    return {r[0] for r in rows}


HOUR = timedelta(hours=1)


# --- T37: disabled by default, zero SQL, zero connections ------------------------------------------------------


def test_t37_purge_is_disabled_by_default_everywhere():
    fresh = Settings()  # every field at its default (environment overrides aside, none set it)
    assert fresh.idempotency_purge_enabled is False
    assert get_settings().idempotency_purge_enabled is False


async def test_t37_disabled_purge_opens_no_connection_and_issues_no_sql(sessions, sync_engine_, tag):
    seeder = sessions()
    for i in range(3):
        await _seed(seeder, f"{tag}-{i}", expires_ago=10 * HOUR)

    statements: list[str] = []
    connects: list[int] = []
    event.listen(sync_engine_, "before_cursor_execute", lambda *a, **k: statements.append(a[2]))
    event.listen(sync_engine_, "engine_connect", lambda *a, **k: connects.append(1))
    event.listen(sync_engine_, "checkout", lambda *a, **k: connects.append(1))

    assert purge.purge_expired_idempotency_keys(settings=Settings(), engine=sync_engine_) == 0
    # The Celery task entry point with the default configuration behaves identically.
    from app.infrastructure.tasks.maintenance import purge_expired_idempotency_keys_task

    assert purge_expired_idempotency_keys_task() == 0
    assert statements == [] and connects == []
    assert len(await _keys(seeder, tag)) == 3  # nothing deleted


def test_t37_the_disabled_path_never_imports_the_sync_engine(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "app.db.sync_session", raising=False)
    assert purge.purge_expired_idempotency_keys(settings=Settings()) == 0
    assert "app.db.sync_session" not in sys.modules


# --- T21: statement shape ---------------------------------------------------------------------------------------------


def test_t21_purge_is_one_cte_statement_with_skip_locked_and_limit_and_no_order_by():
    sql = str(purge.PURGE_BATCH_SQL).upper()
    assert sql.count(";") == 0
    assert "WITH DOOMED AS" in sql and "FOR UPDATE SKIP LOCKED" in sql and "LIMIT" in sql
    assert "ORDER BY" not in sql
    assert "DELETE FROM IDEMPOTENCY_KEY" in sql and "USING DOOMED" in sql


# --- T14/T17: selection --------------------------------------------------------------------------------------------------


async def test_t14_only_rows_expired_beyond_the_grace_are_deleted(sessions, sync_engine_, tag):
    s = sessions()
    await _seed(s, f"{tag}-old", expires_ago=2 * HOUR)  # beyond the 1 h grace: eligible
    await _seed(s, f"{tag}-old-processing", expires_ago=2 * HOUR, status="processing")  # eligible regardless of status
    await _seed(s, f"{tag}-grace", expires_ago=timedelta(minutes=30))  # expired but within grace: kept
    await _seed(s, f"{tag}-live", expires_ago=-HOUR)  # not expired: kept
    deleted = purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_)
    assert deleted >= 2
    assert await _keys(s, tag) == {f"{tag}-grace", f"{tag}-live"}


async def test_t14_renewed_and_replaced_generations_are_never_selected(sessions, sync_engine_, tag):
    key = f"{tag}-renewed"
    s = sessions()
    await _seed(s, key, expires_ago=2 * HOUR, generation=1)
    outcome = await idem.get_or_claim(s, principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    assert outcome.claim is not None and outcome.claim.claim_generation == 2  # replaced: fresh expiry
    purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_)
    assert await _keys(s, tag) == {key}


async def test_t17_purge_leaves_a_live_generation_completable(sessions, sync_engine_, tag):
    key = f"{tag}-live-owner"
    owner = sessions()
    outcome = await idem.get_or_claim(owner, principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_)
    await idem.complete_claim(owner, outcome.claim, response_status=201, response_body={"ok": True})
    await owner.commit()
    assert await _keys(owner, tag) == {key}


# --- T15/T44: bounded, time-budgeted, throttled ------------------------------------------------------------------


async def test_t15_runs_are_bounded_per_statement_and_repeated_runs_drain(sessions, sync_engine_, tag):
    s = sessions()
    for i in range(23):
        await _seed(s, f"{tag}-{i:02d}", expires_ago=5 * HOUR)

    batch_sizes: list[int] = []

    @event.listens_for(sync_engine_, "after_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("WITH DOOMED"):
            batch_sizes.append(cursor.rowcount)

    settings = _settings(idempotency_purge_batch_size=5)
    # Budget allows exactly one batch: monotonic jumps past the budget after the first statement.
    ticks = iter([0.0, 999.0, 999.0, 999.0])
    deleted = purge.purge_expired_idempotency_keys(
        settings=settings, engine=sync_engine_, monotonic=lambda: next(ticks)
    )
    assert deleted == 5 and batch_sizes == [5]
    assert len(await _keys(s, tag)) == 18

    # Repeated runs (unbounded budget) drain the rest; no statement ever exceeds the batch size.
    assert purge.purge_expired_idempotency_keys(settings=settings, engine=sync_engine_) == 18
    assert max(batch_sizes) <= 5
    assert await _keys(s, tag) == set()
    assert purge.purge_expired_idempotency_keys(settings=settings, engine=sync_engine_) == 0


async def test_t44_pause_is_applied_between_batches_and_the_time_budget_stops_the_run(sessions, sync_engine_, tag):
    s = sessions()
    for i in range(12):
        await _seed(s, f"{tag}-{i:02d}", expires_ago=5 * HOUR)
    pauses: list[float] = []
    deleted = purge.purge_expired_idempotency_keys(
        settings=_settings(idempotency_purge_batch_size=5, idempotency_purge_pause_ms=40),
        engine=sync_engine_, sleep=pauses.append,
    )
    assert deleted == 12
    assert pauses == [0.04, 0.04]  # between batch 1->2 and 2->3; the final partial batch needs no pause


# --- T43: statement timeout ----------------------------------------------------------------------------------------------


async def test_t43_a_timed_out_batch_deletes_nothing_logs_only_a_fixed_code_and_does_not_leak_the_timeout(
    sessions, sync_engine_, tag, monkeypatch
):
    s = sessions()
    await _seed(s, f"{tag}-x", expires_ago=5 * HOUR)
    monkeypatch.setattr(purge, "PURGE_BATCH_SQL", text("SELECT pg_sleep(2) /* :cutoff :batch */"))
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(purge.logger, "warning", lambda code, **kw: logged.append((code, kw)))
    settings = _settings(idempotency_purge_statement_timeout_ms=50)

    started = datetime.now(UTC)
    assert purge.purge_expired_idempotency_keys(settings=settings, engine=sync_engine_) == 0
    assert (datetime.now(UTC) - started) < timedelta(seconds=1.5)  # cancelled at ~50 ms, not 2 s
    assert logged == [("idempotency_purge_statement_timeout", {"deleted_before_failure": 0})]
    assert await _keys(s, tag) == {f"{tag}-x"}  # nothing deleted

    with sync_engine_.connect() as conn:  # transaction-local setting did not leak onto the pooled connection
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "0"


# --- T45: validators ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("idempotency_purge_grace_seconds", 3599),
        ("idempotency_purge_batch_size", 5001),
        ("idempotency_purge_batch_size", 0),
        ("idempotency_purge_statement_timeout_ms", 2001),
        ("idempotency_purge_statement_timeout_ms", 0),
        ("idempotency_purge_max_runtime_seconds", 0),
        ("idempotency_purge_max_runtime_seconds", 241),
        ("idempotency_purge_pause_ms", -1),
        ("collector_idempotency_ttl_hours", 35),
    ],
)
def test_t45_invalid_configuration_is_rejected_at_settings_load(field, bad):
    with pytest.raises(ValidationError):
        Settings(**{field: bad})


def test_t45_valid_boundaries_and_defaults():
    ok = Settings(
        idempotency_purge_grace_seconds=3600, idempotency_purge_batch_size=5000,
        idempotency_purge_statement_timeout_ms=2000, collector_idempotency_ttl_hours=36,
    )
    assert ok.idempotency_purge_enabled is False
    assert Settings().collector_idempotency_ttl_hours == 72


# --- T16: purge vs replacement, both orders ----------------------------------------------------------------------------


async def test_t16_purge_first_replacer_skips_the_locked_row_then_inserts_fresh_after_commit(
    sessions, sync_engine_, tag, monkeypatch
):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 5.0)
    key = f"{tag}-race"
    s = sessions()
    await _seed(s, key, expires_ago=5 * HOUR, generation=3)

    purge_conn = sync_engine_.connect()
    purge_txn = purge_conn.begin()  # an in-flight purge statement: row deleted and locked, not committed
    cutoff = datetime.now(UTC) - HOUR
    assert purge_conn.execute(purge.PURGE_BATCH_SQL, {"cutoff": cutoff, "batch": 500}).rowcount >= 1

    contender = asyncio.create_task(
        idem.get_or_claim(sessions(), principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    )
    await asyncio.sleep(0.4)  # the contender's INSERT/replace cannot proceed while the purge holds the row
    assert not contender.done()
    purge_txn.commit()
    purge_conn.close()
    outcome = await asyncio.wait_for(contender, timeout=5)
    assert outcome.claim is not None and outcome.claim.claim_generation == 1  # a fresh row, not a revival
    assert await _keys(s, tag) == {key}


async def test_t16_replacer_first_purge_skips_the_renewed_row(sessions, sync_engine_, tag):
    key = f"{tag}-race2"
    s = sessions()
    await _seed(s, key, expires_ago=5 * HOUR, generation=3)
    replaced = await idem.get_or_claim(sessions(), principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    assert replaced.claim.claim_generation == 4
    assert purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_) == 0
    assert await _keys(s, tag) == {key}


async def test_a_row_locked_by_an_owner_completing_is_skipped_by_the_purge(sessions, sync_engine_, tag):
    key = f"{tag}-completing"
    s = sessions()
    await _seed(s, key, expires_ago=5 * HOUR, status="processing", generation=2)
    owner = sessions()
    await owner.execute(
        text("UPDATE idempotency_key SET status = 'completed' WHERE key = :k"), {"k": key}  # lock held, uncommitted
    )
    assert purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_) == 0  # skipped, no wait
    await owner.rollback()
    assert purge.purge_expired_idempotency_keys(settings=_settings(), engine=sync_engine_) == 1


# --- T25: INSERT ... ON CONFLICT against an uncommitted delete (must-verify PostgreSQL behaviour) -------------------


async def test_t25_insert_on_conflict_waits_for_an_uncommitted_delete_and_then_claims_within_the_window(
    sessions, tag, monkeypatch
):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 5.0)
    key = f"{tag}-insert-wait"
    s = sessions()
    await _seed(s, key, expires_ago=5 * HOUR)

    deleter = sessions()  # stands in for an in-flight purge DELETE of the same key
    await deleter.execute(text("DELETE FROM idempotency_key WHERE key = :k"), {"k": key})

    monitor = sessions()
    started = asyncio.get_running_loop().time()
    contender = asyncio.create_task(
        idem.get_or_claim(sessions(), principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    )
    waiting = False
    for _ in range(60):
        await asyncio.sleep(0.05)
        waiting = bool(
            (
                await monitor.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query ILIKE 'INSERT INTO idempotency_key%'"
                    )
                )
            ).scalar_one()
        )
        if waiting:
            break
    assert waiting, "ON CONFLICT DO NOTHING should wait for the in-progress deleter of the conflicting tuple"
    assert not contender.done()

    await asyncio.sleep(0.5)
    await deleter.commit()
    outcome = await asyncio.wait_for(contender, timeout=5)
    assert outcome.claim is not None
    assert asyncio.get_running_loop().time() - started <= 5.0 + 0.5  # inside the entry-taken 5 s window
    await monitor.rollback()


# --- completion racing with replacement ------------------------------------------------------------------------------------


async def test_completion_racing_a_replacement_never_double_completes(sessions, tag):
    key = f"{tag}-complete-race"
    s = sessions()
    await _seed(s, key, expires_ago=5 * HOUR, status="processing", generation=1)
    owner = sessions()
    stale_view = await owner.get(idem.IdempotencyKey, (await s.execute(
        text("SELECT id FROM idempotency_key WHERE key = :k"), {"k": key})).scalar_one())
    assert stale_view.claim_generation == 1

    # Replacement wins first: the slow owner's completion is fenced out.
    replacement = await idem.get_or_claim(sessions(), principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash="h")
    assert replacement.claim.claim_generation == 2
    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(owner, stale_view, response_status=201, response_body={"owner": "old"})
    await owner.rollback()
    new_owner = sessions()
    claim = await new_owner.get(idem.IdempotencyKey, replacement.claim.id, populate_existing=True)
    await idem.complete_claim(new_owner, claim, response_status=201, response_body={"owner": "new"})
    await new_owner.commit()
    row = (await s.execute(text("SELECT status, response_body FROM idempotency_key WHERE key = :k"), {"k": key})).one()
    assert row.status == "completed" and row.response_body == {"owner": "new"}
