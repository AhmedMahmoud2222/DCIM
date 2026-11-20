"""DB-03 (#357): idempotency ledger expiry, generation-safe replacement and bounded waiting, against real
PostgreSQL (acceptance matrix T1-T13, T20, T23-T24, T35, T38, T40-T42, T50 of the revised design).

Determinism: expiry is driven by SQL-aged rows or the injectable `_now()` clock; the polling deadline uses a
separate patched monotonic clock; two-session interleavings are driven with explicit row locks, never timing.
Each actor owns its own AsyncSession/connection, as one request would in production.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import idempotency as idem
from tests.conftest import TEST_DATABASE_URL

ENDPOINT = "POST:/expiry-test"
PRINCIPAL = "user:00000000-0000-0000-0000-00000000db03"
OTHER_PRINCIPAL = "user:00000000-0000-0000-0000-00000000db04"


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


@pytest.fixture(autouse=True)
def short_poll_window(monkeypatch):
    """Keep the not-expired-and-in-flight paths fast; deadline semantics are tested explicitly below."""
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 0.3)


async def _seed(
    session, key, *, status="completed", request_hash="h", expires_in=timedelta(hours=1), generation=1,
    principal=PRINCIPAL, updated_ago=timedelta(0), response=True,
):
    """Insert a ledger row directly. `expires_in` may be negative (already expired)."""
    await session.execute(
        text(
            "INSERT INTO idempotency_key (id, principal, key, endpoint, request_hash, status, claim_generation,"
            " response_status, response_body, expires_at, created_at, updated_at) VALUES"
            " (gen_random_uuid(), :p, :k, :e, :h, :s, :g, :rs, CAST(:rb AS jsonb),"
            " now() + make_interval(secs => :exp), now(), now() - make_interval(secs => :ago))"
        ),
        {
            "p": principal, "k": key, "e": ENDPOINT, "h": request_hash, "s": status, "g": generation,
            "rs": 201 if response else None, "rb": '{"seeded": true}' if response else None,
            "exp": expires_in.total_seconds(), "ago": updated_ago.total_seconds(),
        },
    )
    await session.commit()


async def _row(session, key, principal=PRINCIPAL):
    result = await session.execute(
        text(
            "SELECT id, status, claim_generation, request_hash, response_status, response_body, expires_at,"
            " expires_at <= now() AS expired FROM idempotency_key WHERE key = :k AND endpoint = :e AND principal = :p"
        ),
        {"k": key, "e": ENDPOINT, "p": principal},
    )
    return result.one_or_none()


async def _claim(session, key, request_hash="h", principal=PRINCIPAL, **kwargs):
    return await idem.get_or_claim(
        session, principal=principal, key=key, endpoint=ENDPOINT, request_hash=request_hash, **kwargs
    )


def _key() -> str:
    return f"k-{uuid.uuid4()}"


# --- T1/T2: non-expired behaviour is unchanged ------------------------------------------------------------


async def test_t1_non_expired_same_hash_replays_the_cached_record(sessions):
    key = _key()
    await _seed(sessions(), key)
    outcome = await _claim(sessions(), key)
    assert outcome.cached is not None and outcome.claim is None
    assert outcome.cached.response_body == {"seeded": True}


async def test_t2_non_expired_different_hash_conflicts(sessions):
    key = _key()
    await _seed(sessions(), key)
    with pytest.raises(idem.IdempotencyConflict):
        await _claim(sessions(), key, request_hash="other")


# --- T3/T4/T7: expired rows start a new processing generation ----------------------------------------------


async def test_t3_expired_completed_same_hash_is_replaced_with_a_new_generation(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, expires_in=timedelta(seconds=-5), generation=3)
    outcome = await _claim(s, key)
    assert outcome.claim is not None and outcome.cached is None
    row = await _row(sessions(), key)
    assert row.status == "processing"
    assert row.claim_generation == 4  # exactly one increment
    assert row.response_status is None and row.response_body is None  # previous response cleared
    assert not row.expired  # expires_at renewed


async def test_t4_expired_different_hash_executes_with_the_new_hash_instead_of_conflicting(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, request_hash="old", expires_in=timedelta(seconds=-5))
    outcome = await _claim(s, key, request_hash="new")
    assert outcome.claim is not None
    assert (await _row(sessions(), key)).request_hash == "new"


@pytest.mark.parametrize("hash_", ["h", "different"])
async def test_t7_expired_processing_row_is_replaced_for_same_and_different_hash(sessions, hash_):
    key = _key()
    s = sessions()
    await _seed(s, key, status="processing", expires_in=timedelta(seconds=-5), response=False, generation=2)
    outcome = await _claim(s, key, request_hash=hash_)
    assert outcome.claim is not None
    row = await _row(sessions(), key)
    assert (row.status, row.claim_generation, row.request_hash) == ("processing", 3, hash_)


# --- T5/T6: in-TTL processing rows ------------------------------------------------------------------------


async def test_t5_fresh_processing_row_reports_still_processing_after_the_window(sessions):
    key = _key()
    await _seed(sessions(), key, status="processing", response=False)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)


async def test_t6_stale_processing_row_is_reclaimed_and_its_expiry_renewed(sessions):
    key = _key()
    s = sessions()
    # Non-expired but stale (owner silent for 5 minutes); expiry is about to lapse in 10 seconds.
    await _seed(s, key, status="processing", response=False, updated_ago=timedelta(minutes=5),
                expires_in=timedelta(seconds=10), generation=1)
    before = (await _row(s, key)).expires_at
    outcome = await _claim(s, key)
    assert outcome.claim is not None
    row = await _row(sessions(), key)
    assert row.claim_generation == 2
    assert row.expires_at > before + timedelta(hours=23)  # a full new 24 h lifetime


# --- T8: inclusive boundary with the injectable clock --------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expect_replaced"),
    [(timedelta(0), True), (timedelta(microseconds=-1), False), (timedelta(microseconds=1), True)],
)
async def test_t8_expiry_boundary_is_inclusive(sessions, monkeypatch, offset, expect_replaced):
    key = _key()
    s = sessions()
    await _seed(s, key)
    expires_at = (await _row(s, key)).expires_at
    monkeypatch.setattr(idem, "_now", lambda: expires_at + offset)
    if expect_replaced:
        outcome = await _claim(sessions(), key)
        assert outcome.claim is not None
    else:
        outcome = await _claim(sessions(), key)
        assert outcome.cached is not None  # one microsecond before expiry: still a replay


# --- T9/T10: concurrency on one expired row --------------------------------------------------------------


@pytest.mark.parametrize("contenders", [2, 8])
async def test_t9_concurrent_contenders_on_an_expired_row_produce_exactly_one_new_generation(sessions, contenders):
    key = _key()
    await _seed(sessions(), key, expires_in=timedelta(seconds=-5), generation=5)
    results = await asyncio.gather(
        *[_claim(sessions(), key) for _ in range(contenders)], return_exceptions=True
    )
    claims = [r for r in results if not isinstance(r, Exception) and r.claim is not None]
    assert len(claims) == 1
    # Every loser sees a fresh, non-expired processing row with the same hash: it waits, then 503s.
    assert all(isinstance(r, idem.IdempotencyStillProcessing) for r in results if isinstance(r, Exception))
    assert (await _row(sessions(), key)).claim_generation == 6


async def test_t10_replacement_skips_a_locked_row_immediately_instead_of_waiting(sessions):
    key = _key()
    holder, contender = sessions(), sessions()
    await _seed(holder, key, expires_in=timedelta(seconds=-5), generation=1)
    record_for_contender = (
        await contender.execute(text("SELECT 1"))  # open the contender's connection first
    )
    assert record_for_contender is not None
    row_id = (await _row(holder, key)).id

    # The holder locks the expired row (an owner completing, a concurrent replacement, a purge).
    await holder.execute(text("SELECT id FROM idempotency_key WHERE id = :i FOR UPDATE"), {"i": row_id})

    class _Seen:
        id = row_id
        claim_generation = 1

    started = asyncio.get_running_loop().time()
    replaced = await idem._try_replace_expired(  # noqa: SLF001
        contender, _Seen(), request_hash="h", now=datetime.now(UTC), ttl=timedelta(hours=24)
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert replaced is None
    assert elapsed < 1.0  # SKIP LOCKED: no wait on the holder's lock

    await holder.commit()  # holder releases without changing the row
    _Seen.claim_generation = 1
    replaced = await idem._try_replace_expired(  # noqa: SLF001
        contender, _Seen(), request_hash="h", now=datetime.now(UTC), ttl=timedelta(hours=24)
    )
    assert replaced == row_id


async def test_a_changed_generation_is_never_replaced_by_a_stale_observation(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, expires_in=timedelta(seconds=-5), generation=7)
    row_id = (await _row(s, key)).id

    class _Seen:
        id = row_id
        claim_generation = 6  # observed before someone else already moved it to generation 7

    assert (
        await idem._try_replace_expired(  # noqa: SLF001
            sessions(), _Seen(), request_hash="h", now=datetime.now(UTC), ttl=timedelta(hours=24)
        )
        is None
    )
    assert (await _row(s, key)).claim_generation == 7


# --- T11/T12/T13/T35: fencing, principals, legacy rows ----------------------------------------------------


async def test_t11_owner_of_a_replaced_generation_cannot_complete_or_release(sessions):
    key = _key()
    first = await _claim(sessions(), key)
    old_owner_session = sessions()
    old_claim = await old_owner_session.get(idem.IdempotencyKey, first.claim.id)
    old_ref = idem.ClaimRef.of(old_claim)
    expirer = sessions()
    await expirer.execute(
        text("UPDATE idempotency_key SET expires_at = now() - interval '1 second' WHERE key = :k"), {"k": key}
    )
    await expirer.commit()

    second = await _claim(sessions(), key)
    assert second.claim is not None and second.claim.claim_generation == old_ref.generation + 1

    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(old_owner_session, old_claim, response_status=201, response_body={"x": 1})
    await old_owner_session.rollback()
    await idem.release_claim(old_owner_session, old_ref)  # fenced: deletes nothing
    row = await _row(sessions(), key)
    assert row is not None and row.status == "processing" and row.claim_generation == old_ref.generation + 1


async def test_t12_expiry_and_replacement_never_cross_principals(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, principal=PRINCIPAL, expires_in=timedelta(seconds=-5), generation=1)
    await _seed(s, key, principal=OTHER_PRINCIPAL, generation=1)  # completed, live, same key and endpoint
    outcome = await _claim(sessions(), key, principal=PRINCIPAL)
    assert outcome.claim is not None
    other = await _row(sessions(), key, principal=OTHER_PRINCIPAL)
    assert (other.status, other.claim_generation, other.response_status) == ("completed", 1, 201)


async def test_t13_a_legacy_unscoped_row_is_inert_and_never_adopted(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, principal="legacy:unscoped", expires_in=timedelta(seconds=-5), generation=1)
    outcome = await _claim(sessions(), key)  # a real principal claims its own fresh row
    assert outcome.claim is not None
    legacy = await _row(sessions(), key, principal="legacy:unscoped")
    assert (legacy.status, legacy.claim_generation) == ("completed", 1)  # untouched


async def test_t35_zombie_owner_after_the_row_was_deleted_and_reinserted_is_fenced(sessions):
    key = _key()
    first = await _claim(sessions(), key)
    zombie_session = sessions()
    zombie_claim = await zombie_session.get(idem.IdempotencyKey, first.claim.id)
    zombie_ref = idem.ClaimRef.of(zombie_claim)
    admin = sessions()
    await admin.execute(text("DELETE FROM idempotency_key WHERE key = :k"), {"k": key})
    await admin.commit()
    second = await _claim(sessions(), key)  # same generation number (1), different row id
    assert second.claim is not None and second.claim.id != zombie_claim.id

    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(zombie_session, zombie_claim, response_status=201, response_body={})
    await zombie_session.rollback()
    await idem.release_claim(zombie_session, zombie_ref)
    assert (await _row(sessions(), key)).status == "processing"


# --- T20: every transition into processing leaves a live expiry ----------------------------------------------


async def test_t20_every_processing_transition_sets_a_future_expiry(sessions):
    new_key, stale_key, expired_key = _key(), _key(), _key()
    s = sessions()
    await _seed(s, stale_key, status="processing", response=False, updated_ago=timedelta(minutes=5),
                expires_in=timedelta(seconds=5))
    await _seed(s, expired_key, expires_in=timedelta(seconds=-5))
    for key in (new_key, stale_key, expired_key):
        assert (await _claim(sessions(), key)).claim is not None
        row = await _row(sessions(), key)
        assert row.status == "processing" and not row.expired
        assert row.expires_at > datetime.now(UTC) + timedelta(hours=23)


async def test_completion_does_not_renew_expiry(sessions):
    key = _key()
    s = sessions()
    outcome = await _claim(s, key)
    shortened = (await _row(sessions(), key)).expires_at - timedelta(hours=20)
    await s.execute(text("UPDATE idempotency_key SET expires_at = :e WHERE key = :k"), {"e": shortened, "k": key})
    await idem.complete_claim(s, outcome.claim, response_status=201, response_body={"ok": True})
    await s.commit()
    assert (await _row(sessions(), key)).expires_at == shortened


# --- T23/T24: no rollback, ORM objects stay readable, bounded loop ---------------------------------------------


async def test_t23_a_blocked_replacement_neither_rolls_back_nor_expires_caller_objects(sessions, monkeypatch):
    key = _key()
    holder, caller = sessions(), sessions()
    await _seed(holder, key, expires_in=timedelta(seconds=-5))
    row_id = (await _row(holder, key)).id
    await holder.execute(text("SELECT id FROM idempotency_key WHERE id = :i FOR UPDATE"), {"i": row_id})

    # A loaded ORM object the endpoint still reads after the claim (ctx.user / collector in production).
    sentinel = await caller.get(idem.IdempotencyKey, row_id)
    rollbacks = []
    original_rollback = caller.rollback

    async def spy_rollback():
        rollbacks.append(1)
        await original_rollback()

    monkeypatch.setattr(caller, "rollback", spy_rollback)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(caller, key)  # holder keeps the row locked for the whole (0.3 s) window
    assert rollbacks == []
    # Reading a loaded attribute would raise MissingGreenlet had the session expired it.
    assert sentinel.principal == PRINCIPAL
    await holder.rollback()


# --- T38: identity-map freshness ---------------------------------------------------------------------------------


async def test_t38_replacement_returns_the_new_generation_even_when_the_old_row_is_in_the_identity_map(sessions):
    key = _key()
    s = sessions()
    await _seed(s, key, expires_in=timedelta(seconds=-5), generation=4)
    preloaded = (await s.execute(text("SELECT id FROM idempotency_key WHERE key = :k"), {"k": key})).scalar_one()
    old = await s.get(idem.IdempotencyKey, preloaded)  # generation 4 now sits in the identity map
    assert old.claim_generation == 4

    outcome = await _claim(s, key)
    assert outcome.claim.claim_generation == 5
    ref = idem.ClaimRef.of(outcome.claim)
    assert ref.generation == 5
    await idem.complete_claim(s, outcome.claim, response_status=201, response_body={"ok": True})  # not ClaimLost
    await s.commit()
    assert (await _row(sessions(), key)).status == "completed"


async def test_t38_regression_guard_a_plain_get_would_return_the_stale_generation(sessions):
    """Shows why populate_existing is mandatory: without it the identity map hands back generation N."""
    key = _key()
    s = sessions()
    await _seed(s, key, expires_in=timedelta(seconds=-5), generation=4)
    row_id = (await s.execute(text("SELECT id FROM idempotency_key WHERE key = :k"), {"k": key})).scalar_one()
    pinned = await s.get(idem.IdempotencyKey, row_id)  # keep a reference: the identity map is weak
    other = sessions()
    assert (await idem._try_replace_expired(  # noqa: SLF001
        other, type("R", (), {"id": row_id, "claim_generation": 4})(), request_hash="h",
        now=datetime.now(UTC), ttl=timedelta(hours=24),
    )) == row_id
    stale = await s.get(idem.IdempotencyKey, row_id)  # no populate_existing
    assert stale is pinned and stale.claim_generation == 4  # stale: the database says 5
    fresh = await s.get(idem.IdempotencyKey, row_id, populate_existing=True)
    assert fresh.claim_generation == 5


# --- T40/T41/T42: deadline semantics ------------------------------------------------------------------------------


class _FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch):
    clock = _FakeTime()
    monkeypatch.setattr(idem, "_monotonic", clock.monotonic)
    monkeypatch.setattr(idem.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 5.0)
    return clock


async def test_t40_failed_replace_checks_the_deadline_before_sleeping(sessions, monkeypatch, fake_time):
    key = _key()
    await _seed(sessions(), key, expires_in=timedelta(seconds=-5))

    async def lose(*_a, **_k):
        fake_time.now += 6  # the attempt itself consumed the whole window
        return None

    monkeypatch.setattr(idem, "_try_replace_expired", lose)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    assert fake_time.sleeps == []  # raised without sleeping


async def test_t40_failed_stale_reclaim_checks_the_deadline_before_sleeping(sessions, monkeypatch, fake_time):
    key = _key()
    await _seed(sessions(), key, status="processing", response=False, updated_ago=timedelta(minutes=5))

    async def lose(*_a, **_k):
        fake_time.now += 6
        return None

    monkeypatch.setattr(idem, "_try_reclaim_stale", lose)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    assert fake_time.sleeps == []


async def test_t40_missing_row_with_failed_insert_checks_the_deadline_before_sleeping(sessions, monkeypatch, fake_time):
    key = _key()
    calls = {"n": 0}

    async def never_wins(*_a, **_k):
        calls["n"] += 1
        fake_time.now += 6
        return None

    monkeypatch.setattr(idem, "_try_claim", never_wins)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    assert calls["n"] == 2 and fake_time.sleeps == []  # initial attempt + one loop attempt, no spin


async def test_t40_a_failing_attempt_cannot_spin_without_sleeping(sessions, monkeypatch, fake_time):
    key = _key()
    await _seed(sessions(), key, expires_in=timedelta(seconds=-5))
    attempts = {"n": 0}

    async def lose(*_a, **_k):
        attempts["n"] += 1
        return None  # instant failure each time

    monkeypatch.setattr(idem, "_try_replace_expired", lose)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    # Every failed iteration slept before retrying, so the loop is bounded by budget / interval (the last raises).
    assert len(fake_time.sleeps) == attempts["n"] - 1  # every failed attempt but the last slept first
    assert attempts["n"] <= int(5.0 / idem._POLL_INTERVAL_SECONDS) + 2  # noqa: SLF001


async def test_t41_the_deadline_is_taken_at_entry_so_a_slow_first_attempt_shrinks_the_polling_budget(
    sessions, monkeypatch, fake_time
):
    key = _key()
    await _seed(sessions(), key, status="processing", response=False)  # live, same hash: nothing to do but wait
    real_try_claim = idem._try_claim  # noqa: SLF001

    async def slow_first_insert(*a, **k):
        fake_time.now += 4.0  # e.g. the INSERT waited on a lock holder for 4 s
        return await real_try_claim(*a, **k)

    monkeypatch.setattr(idem, "_try_claim", slow_first_insert)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    # 5 s budget - 4 s spent = 1 s, plus at most one poll interval (tolerance for float accumulation)
    assert sum(fake_time.sleeps) <= 1.0 + idem._POLL_INTERVAL_SECONDS + 1e-6  # noqa: SLF001


async def test_t42_a_frozen_expiry_clock_cannot_hang_the_loop(sessions, monkeypatch):
    key = _key()
    await _seed(sessions(), key, status="processing", response=False)
    frozen = datetime.now(UTC)
    monkeypatch.setattr(idem, "_now", lambda: frozen)
    started = asyncio.get_running_loop().time()
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)  # 0.3 s window from the autouse fixture
    assert asyncio.get_running_loop().time() - started < 3.0


# --- T50: reserved / malformed principals ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal",
    ["legacy:unscoped", "legacy:anything", "", "   ", "admin", "user:", "collector:", "user: x", "USER:abc",
     "user:a\nb", "x" * 101, "user:" + "a" * 100],
)
async def test_t50_reserved_and_malformed_principals_are_rejected_before_any_database_access(principal):
    class _NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"database touched via {name}")

    with pytest.raises(ValueError):
        await idem.get_or_claim(_NoDb(), principal=principal, key="k", endpoint=ENDPOINT, request_hash="h")


@pytest.mark.parametrize("principal", ["user:A", "user:" + str(uuid.uuid4()), "collector:" + str(uuid.uuid4())])
async def test_generated_principal_shapes_are_accepted(sessions, principal):
    assert (await _claim(sessions(), _key(), principal=principal)).claim is not None
