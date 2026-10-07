"""SEC (Codex PR #49 review, ROUND 8): `app/db/session.py::get_db` is the shared
`Depends(get_db)` dependency used by essentially the entire API surface (~150 call sites
across 20 route modules -- see that module's own docstring), so its resource-cleanup
guarantees need proof independent of any one route, and independent of any one *kind* of
failure. Prior rounds' regression coverage in `tests/api/test_collectors.py` monkeypatched
`AsyncSession.close`/`.rollback` themselves to raise immediately -- a full method
replacement that never touches SQLAlchemy's own internals at all, so the session's
`_transaction` pointer is never actually mutated before the injected failure. That does
not reproduce Codex's round-8 finding, which is specifically about the internal ordering
*inside* SQLAlchemy's real close/rollback implementation.

This module's own investigation (empirically verified against this project's pinned
SQLAlchemy 2.1.1, not merely read from source) traces exactly where that ordering bites:

  - `SessionTransaction.close()`/`.rollback()` both execute
    `self.session._transaction = self._parent` (`None` for a root transaction) *before*
    the loop that actually ends the low-level `Transaction`/`Connection` -- so `Session.
    invalidate()`'s own `if self._transaction is not None` guard is already false by the
    time an exception from that loop reaches `get_db()`'s `except` blocks, making the old
    `await session.invalidate()` fallback a proven, reproducible no-op.

  - For a **genuinely severed** connection (verified here by calling `.terminate()` on
    the real asyncpg driver connection), this theoretical gap turns out not to matter in
    practice: the InterfaceError SQLAlchemy's asyncpg dialect raises is classified as a
    disconnect, and the dialect's own automatic invalidate-on-disconnect handling
    (independent of any ORM-level `invalidate()` call) discards the connection anyway --
    confirmed here with `pool_pre_ping` explicitly disabled, so that mechanism can't be
    credited to pre-ping alone.

  - The gap becomes concretely, operationally real for the *other* kind of close/rollback
    failure: an exception SQLAlchemy's dialect does **not** classify as a disconnect (a
    plain Python exception raised from deep inside the ORM's own transaction-closing
    code -- reproduced here by patching `sqlalchemy.engine.base.Transaction.close`, the
    exact call `SessionTransaction.close()`'s loop makes, to raise once). Verified
    directly: with the pre-round-8 `get_db()`, this permanently leaks the connection out
    of the pool (`engine.pool.checkedout()` never returns to 0) -- with a pool of size 1
    and no overflow, a second, wholly ordinary request against the same engine then hangs
    for the full pool-checkout timeout waiting for a connection that will never come
    back. `get_db()`'s round-8 fix (a connection reference snapshotted via public APIs --
    `session.in_transaction()` / `session.connection()` -- *before* the operation that
    might fail, invalidated directly if it does) closes exactly this gap, verified below.

`app/db/session.py::engine` is a process-wide singleton bound to whichever asyncio event
loop first uses it; pytest-asyncio gives each test function its own loop, so every test
below disposes the engine (or its own dedicated engine) in its own `finally` -- otherwise
a connection left in the pool from one test's loop breaks the next test's loop entirely
(an "attached to a different loop" `RuntimeError`), exactly the cross-test pollution
earlier rounds in this same PR already hit and documented in `tests/api/test_collectors.py`."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.engine.base import Transaction
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

import app.db.session as session_module
from app.core.config import get_settings
from app.db.session import get_db as real_get_db

pytestmark = pytest.mark.asyncio


def _patch_transaction_close_to_fail_once(monkeypatch) -> None:
    """Patches `Transaction.close` -- the exact call `SessionTransaction.close()`'s own
    connection-ending loop makes, *after* it has already set `session._transaction =
    self._parent` -- to raise a plain, non-disconnect `RuntimeError` on its first call
    only. See this module's docstring for why this specific injection point (rather than
    replacing `AsyncSession.close` wholesale) is what actually reproduces Codex's
    round-8 finding: a plain exception here is not classified as a disconnect by the
    dialect, so none of SQLAlchemy's own automatic invalidate-on-disconnect machinery
    can mask whether `get_db()`'s own fallback actually works."""
    original_close = Transaction.close
    fired = {"count": 0}

    def patched_close(self, *args, **kwargs):
        fired["count"] += 1
        if fired["count"] == 1:
            monkeypatch.setattr(Transaction, "close", original_close)
            raise RuntimeError("close failed: synthetic-transaction-close-SEC07K-keep-private")
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(Transaction, "close", patched_close)


def _patch_transaction_persistently_broken(monkeypatch):
    """Simulates a genuinely, persistently broken connection for the `rollback()` path,
    rather than a single one-off failure: BOTH `Transaction.rollback` and
    `Transaction.close` raise for as long as this patch is active. This matters because
    `SessionTransaction.rollback()`'s own low-level DB interaction is `t[1].rollback()`
    -- the core `Transaction.rollback()`, not `.close()` -- but it *also* unconditionally
    calls `self.close()` (the ORM-level close) at its tail regardless of whether that
    first rollback succeeded. Empirically verified against this project's pinned
    SQLAlchemy 2.1.1: patching only `.rollback()` to fail once is not enough to
    reproduce a leak, because that tail `self.close()` call still succeeds against the
    (in a mocked test, still perfectly healthy) connection and releases it anyway --
    accurately reflecting that a single transient rollback hiccup is not, by itself, a
    real leak. A connection that is *actually* broken fails every subsequent operation
    on it too, which is what this helper models. Returns a zero-arg function that
    restores both methods -- call it once `get_db()`'s cleanup has run, before touching
    the engine again (the test's own "does a normal request still work" verification
    must run against healthy methods, not these persistently-failing ones)."""
    original_rollback = Transaction.rollback
    original_close = Transaction.close

    def patched_rollback(self, *args, **kwargs):
        raise RuntimeError("rollback failed: synthetic-transaction-rollback-SEC07L-keep-private")

    def patched_close(self, *args, **kwargs):
        raise RuntimeError("close failed: synthetic-transaction-close-SEC07L-keep-private")

    monkeypatch.setattr(Transaction, "rollback", patched_rollback)
    monkeypatch.setattr(Transaction, "close", patched_close)

    def _restore() -> None:
        monkeypatch.setattr(Transaction, "rollback", original_rollback)
        monkeypatch.setattr(Transaction, "close", original_close)

    return _restore


async def test_get_db_close_failure_with_a_live_transaction_releases_the_pool_slot(monkeypatch):
    """The core round-8 reproduction against the real production engine: check out a
    real connection via the real `get_db` dependency, leave its transaction open (no
    commit -- exactly the read-endpoint shape Codex named, e.g. `GET
    /api/v1/discovery/devices`), fail deep inside the real close() logic, and drive the
    dependency generator to its normal (non-exception) exit exactly as FastAPI itself
    would for a successful request. Asserts `engine.pool.checkedout()` returns to 0 --
    i.e. the connection was actually released back to the pool, not merely that some
    fallback function was invoked -- and that a wholly separate, subsequent use of the
    SAME engine succeeds immediately, with no `engine.dispose()` call anywhere before
    that point."""
    engine = session_module.engine
    # Defensive: an earlier test elsewhere in the same run may have left a connection
    # bound to ITS OWN (by-now-closed) event loop sitting in this process-wide engine's
    # pool as "checked in" -- checkedout() alone wouldn't reveal that, and pool_pre_ping
    # trying to validate it would itself raise "attached to a different loop". Disposing
    # unconditionally before this test's own work starts guarantees a clean pool
    # regardless of what ran before it.
    await engine.dispose()
    assert engine.pool.checkedout() == 0, "test must start from a clean pool"

    _patch_transaction_close_to_fail_once(monkeypatch)

    try:
        gen = real_get_db()
        session = await gen.__anext__()
        try:
            # A genuine read, no commit.
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
            assert session.in_transaction() is True, "the read must leave a live, uncommitted transaction"
            assert engine.pool.checkedout() == 1, "the read must have genuinely checked out a connection"

            # Drive the generator to its ordinary exit -- FastAPI does exactly this for a
            # successful request: advance past the `yield`, which runs get_db()'s
            # `finally` block with no exception in flight. The injected close() failure
            # is caught there; the generator must still terminate cleanly
            # (StopAsyncIteration), not propagate the synthetic RuntimeError to the
            # caller.
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
        finally:
            await gen.aclose()

        assert engine.pool.checkedout() == 0, (
            "the connection must be released back to the pool even though close() itself failed deep "
            "inside real SQLAlchemy internals -- this is the operational guarantee session.invalidate() "
            "alone could not provide (see this module's docstring and app/db/session.py's own docstring)"
        )

        # A wholly separate, subsequent use of the SAME engine/pool must succeed
        # immediately. No engine.dispose() has been called anywhere above.
        async with session_module.AsyncSessionLocal() as verifying_session:
            result = await verifying_session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


async def test_get_db_rollback_then_invalidate_failure_with_a_live_transaction_releases_the_pool_slot(
    monkeypatch,
):
    """The `except` branch's sibling of the test above: an unhandled exception from the
    route (simulated here by throwing into the generator) makes `get_db()` attempt
    `rollback()`; when THAT also fails deep inside real SQLAlchemy internals with a
    still-live, checked-out connection, the same connection-snapshot mechanism must
    release the pool slot -- not the old `session.invalidate()` call, which this
    module's docstring explains was equally a no-op on this branch for the identical
    reason."""
    engine = session_module.engine
    await engine.dispose()  # see the sibling close-failure test's comment on why
    assert engine.pool.checkedout() == 0

    restore_transaction_methods = _patch_transaction_persistently_broken(monkeypatch)

    try:
        gen = real_get_db()
        session = await gen.__anext__()
        try:
            await session.execute(text("SELECT 1"))
            assert session.in_transaction() is True
            assert engine.pool.checkedout() == 1

            # get_db()'s bare `raise` after the nested rollback-failure guard re-raises
            # the ORIGINAL exception, not the rollback failure -- the sanitization
            # guarantee established in earlier rounds. This test's own concern is the
            # pool slot, not this (already-covered) sanitization behavior.
            with pytest.raises(RuntimeError, match="original route failure"):
                await gen.athrow(RuntimeError("original route failure"))
        finally:
            await gen.aclose()

        assert engine.pool.checkedout() == 0, "a failed rollback with a live connection must still release the pool slot"

        # Restore healthy Transaction methods before verifying recovery -- this test's
        # "persistently broken connection" was this specific (mocked) connection only;
        # a real subsequent request would get a different, healthy one regardless.
        restore_transaction_methods()

        async with session_module.AsyncSessionLocal() as verifying_session:
            result = await verifying_session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        assert engine.pool.checkedout() == 0
    finally:
        restore_transaction_methods()
        await engine.dispose()


async def test_get_db_close_failure_does_not_exhaust_a_fully_saturated_pool(monkeypatch):
    """The concrete, operational consequence of the leak this round's fix closes: with a
    pool of exactly one connection and no overflow, the pre-round-8 `get_db()` leaves
    that one connection permanently checked out after a single non-disconnect close
    failure -- proven, empirically, to make a second, wholly ordinary request hang for
    the full pool-checkout timeout (see this module's docstring). This test uses a
    dedicated pool_size=1/max_overflow=0 engine (not the shared production one) so the
    exhaustion is deterministic and fast, and bounds the second request with
    `asyncio.wait_for` so a regression fails this test immediately instead of hanging
    the suite."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=False, pool_size=1, max_overflow=0)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        # Exercise the REAL, unmodified get_db() -- just bound to this test's dedicated
        # small-pool engine instead of the shared production one, so pool exhaustion is
        # deterministic and fast rather than depending on settings.database_pool_size.
        monkeypatch.setattr(session_module, "AsyncSessionLocal", session_factory)
        _patch_transaction_close_to_fail_once(monkeypatch)

        gen = real_get_db()
        first_session = await gen.__anext__()
        await first_session.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        await gen.aclose()

        assert engine.pool.checkedout() == 0, "the fix must free the one available pool slot"

        async def _second_request() -> int:
            async with session_factory() as s2:
                return (await s2.execute(text("SELECT 1"))).scalar_one()

        # Bounded: a regression (leaked slot) would hang here until the pool's checkout
        # timeout (default 30s); failing fast keeps this test's own runtime sane.
        value = await asyncio.wait_for(_second_request(), timeout=5.0)
        assert value == 1
    finally:
        await engine.dispose()


def _patch_session_connection_to_raise_once(monkeypatch, message: str) -> None:
    """Patches the real, production `AsyncSession.connection` -- exactly the awaited call
    `_snapshot_connection_for_invalidation` makes -- to raise once, on the real class, not
    a stub. See this module's docstring (Codex's fifth follow-up) for why this specific
    injection point is what reproduces the round-9 finding: unlike the round-8 tests
    above (which break the low-level `Transaction.close`/`.rollback` SQLAlchemy calls
    made *inside* the guarded `rollback()`/`close()` attempts), this breaks the snapshot
    attempt that runs *before* either of those guarded calls."""
    from sqlalchemy.ext.asyncio import AsyncSession as RealAsyncSession

    original_connection = RealAsyncSession.connection
    fired = {"count": 0}

    async def patched_connection(self, *args, **kwargs):
        fired["count"] += 1
        if fired["count"] == 1:
            monkeypatch.setattr(RealAsyncSession, "connection", original_connection)
            raise RuntimeError(message)
        return await original_connection(self, *args, **kwargs)

    monkeypatch.setattr(RealAsyncSession, "connection", patched_connection)


async def test_get_db_snapshot_failure_in_finally_still_closes_and_releases_the_pool_slot(monkeypatch, capsys):
    """SEC (Codex PR #49 review, ROUND 9): the `finally` block's snapshot call ran
    unguarded before `session.close()` -- a failure there would have replaced an
    otherwise-successful generator exit with the raw snapshot exception, AND skipped
    `close()` entirely (not merely failed to invalidate the connection afterward, but
    never attempted to release it at all). Drives a real, healthy transaction to its
    ordinary (non-exception) exit -- the same successful-read-route shape as the round-8
    close-failure test above -- while `AsyncSession.connection()` itself raises once.
    Asserts the generator still terminates normally (StopAsyncIteration, not the
    synthetic snapshot exception), `close()` still runs and releases the real pool slot,
    the fixed `db_session_cleanup_finally_snapshot_failed` event is logged, and the raw
    exception text never reaches the log."""
    engine = session_module.engine
    await engine.dispose()  # see the close-failure test's comment on why
    assert engine.pool.checkedout() == 0

    snapshot_secret = "synthetic-snapshot-SEC07M-keep-private"
    _patch_session_connection_to_raise_once(monkeypatch, f"snapshot failed: {snapshot_secret}")

    try:
        gen = real_get_db()
        session = await gen.__anext__()
        try:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
            assert session.in_transaction() is True
            assert engine.pool.checkedout() == 1

            # Ordinary successful exit: no exception in flight. A regression (unguarded
            # snapshot) would raise the synthetic RuntimeError here instead.
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
        finally:
            await gen.aclose()

        assert engine.pool.checkedout() == 0, (
            "close() must still run -- and release the pool slot -- even though the snapshot "
            "attempt immediately before it failed"
        )

        async with session_module.AsyncSessionLocal() as verifying_session:
            result = await verifying_session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()

    log_text = capsys.readouterr().out
    assert snapshot_secret not in log_text
    assert '"event": "db_session_cleanup_finally_snapshot_failed"' in log_text


async def test_get_db_snapshot_failure_in_except_block_does_not_replace_the_original_exception(monkeypatch, capsys):
    """SEC (Codex PR #49 review, ROUND 9): the `except` block's snapshot call ran
    unguarded before the guarded `session.rollback()` attempt -- a failure there would
    have replaced the ORIGINAL, already-sanitized exception propagating from the route
    (e.g. `ingest_batch`'s recovered `ApiError(503)`) with this raw one, undoing every
    prior round's sanitization one step further back, AND skipped the rollback attempt
    entirely. Throws a distinct synthetic "original route failure" into the generator
    (simulating an already-sanitized exception in flight) while `AsyncSession.connection()`
    itself raises once with its own distinct secret. Asserts the ORIGINAL exception is
    what actually propagates out of `athrow` (not the snapshot failure), `rollback()` still
    ran on a real, live transaction (pool slot released), the fixed
    `db_session_cleanup_snapshot_failed` event is logged, and neither secret reaches the
    log."""
    engine = session_module.engine
    await engine.dispose()
    assert engine.pool.checkedout() == 0

    snapshot_secret = "synthetic-snapshot-SEC07N-keep-private"
    original_secret = "synthetic-original-SEC07N-keep-private"
    _patch_session_connection_to_raise_once(monkeypatch, f"snapshot failed: {snapshot_secret}")

    try:
        gen = real_get_db()
        session = await gen.__anext__()
        try:
            await session.execute(text("SELECT 1"))
            assert session.in_transaction() is True
            assert engine.pool.checkedout() == 1

            with pytest.raises(RuntimeError, match=original_secret):
                await gen.athrow(RuntimeError(f"original route failure: {original_secret}"))
        finally:
            await gen.aclose()

        assert engine.pool.checkedout() == 0, (
            "rollback() must still run -- and release the pool slot -- even though the snapshot "
            "attempt immediately before it failed"
        )

        async with session_module.AsyncSessionLocal() as verifying_session:
            result = await verifying_session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()

    log_text = capsys.readouterr().out
    assert snapshot_secret not in log_text
    assert original_secret not in log_text
    assert '"event": "db_session_cleanup_snapshot_failed"' in log_text


async def test_snapshot_helper_is_a_public_api_only_no_op_when_no_transaction_is_open():
    """Sanity check on `_snapshot_connection_for_invalidation`'s own stated contract:
    when nothing has touched the database yet, it must not open a connection just to
    have something to invalidate -- `session.in_transaction()` (pure in-memory, no I/O)
    is what makes that possible without any wasted checkout on the ordinary happy path."""
    from app.db.session import _snapshot_connection_for_invalidation

    engine = session_module.engine
    await engine.dispose()  # see the close-failure test's comment on why
    try:
        async with session_module.AsyncSessionLocal() as session:
            assert session.in_transaction() is False
            connection = await _snapshot_connection_for_invalidation(session)
            assert connection is None
            assert engine.pool.checkedout() == 0

            await session.execute(text("SELECT 1"))
            connection = await _snapshot_connection_for_invalidation(session)
            assert isinstance(connection, AsyncConnection)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- Issue #51
# The residual case PR #49's acceptance left open: the pre-failure connection snapshot ITSELF
# fails (so `get_db()` holds no connection reference) and rollback/close then fail too. The
# snapshot is an awaited call on the very connection that is broken, so it cannot be the only
# source of a reference. `get_db()` now also remembers the connection when the transaction
# begins (`after_begin`, stored in the session's public `info`) and invalidates that one.

_SNAPSHOT_SECRET = "synthetic-snapshot-ISSUE51-keep-private"
_ROLLBACK_SECRET = "synthetic-rollback-ISSUE51-keep-private"
_CLOSE_SECRET = "synthetic-close-ISSUE51-keep-private"


def _break_snapshot_rollback_and_close(monkeypatch) -> None:
    """`AsyncSession.connection()` (the snapshot), `Transaction.rollback` and
    `Transaction.close` all fail persistently with non-disconnect errors, so the dialect's own
    invalidate-on-disconnect handling cannot be what recovers the pool."""
    from sqlalchemy.ext.asyncio import AsyncSession as RealAsyncSession

    async def failing_connection(self, *args, **kwargs):
        raise RuntimeError(f"snapshot failed: {_SNAPSHOT_SECRET}")

    def failing_rollback(self, *args, **kwargs):
        raise RuntimeError(f"rollback failed: {_ROLLBACK_SECRET}")

    def failing_close(self, *args, **kwargs):
        raise RuntimeError(f"close failed: {_CLOSE_SECRET}")

    monkeypatch.setattr(RealAsyncSession, "connection", failing_connection)
    monkeypatch.setattr(Transaction, "rollback", failing_rollback)
    monkeypatch.setattr(Transaction, "close", failing_close)


def _single_slot_factory(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine(
        get_settings().database_url, pool_pre_ping=False, pool_size=1, max_overflow=0, pool_timeout=3
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", factory)
    return engine, factory


async def _next_request_succeeds(factory) -> int:
    async def _request() -> int:
        async with factory() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    # Bounded: a leaked slot would otherwise wait for the pool checkout timeout.
    return await asyncio.wait_for(_request(), timeout=5.0)


async def test_snapshot_rollback_and_close_all_failing_still_frees_the_single_pool_slot(monkeypatch, capsys):
    from sqlalchemy.ext.asyncio import AsyncSession as RealAsyncSession

    engine, factory = _single_slot_factory(monkeypatch)
    original_connection = RealAsyncSession.connection
    original_rollback, original_close = Transaction.rollback, Transaction.close
    try:
        gen = real_get_db()
        session = await gen.__anext__()
        await session.execute(text("SELECT 1"))
        assert session.in_transaction() is True
        assert engine.pool.checkedout() == 1

        _break_snapshot_rollback_and_close(monkeypatch)
        # The route's own exception, not any cleanup failure, is what reaches the caller.
        with pytest.raises(RuntimeError, match="route failure ISSUE51"):
            await gen.athrow(RuntimeError("route failure ISSUE51"))
        await gen.aclose()

        # Characterization, with `session` still referenced (so garbage collection cannot be
        # what frees the slot): the slot is released by explicit invalidation.
        assert engine.pool.checkedout() == 0

        monkeypatch.setattr(RealAsyncSession, "connection", original_connection)
        monkeypatch.setattr(Transaction, "rollback", original_rollback)
        monkeypatch.setattr(Transaction, "close", original_close)
        assert await _next_request_succeeds(factory) == 1
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()

    log_text = capsys.readouterr().out
    for secret in (_SNAPSHOT_SECRET, _ROLLBACK_SECRET, _CLOSE_SECRET):
        assert secret not in log_text
    assert '"event": "db_session_cleanup_snapshot_failed"' in log_text
    assert '"event": "db_session_cleanup_rollback_failed"' in log_text


async def test_snapshot_and_close_failing_on_a_successful_request_still_frees_the_single_pool_slot(monkeypatch, capsys):
    from sqlalchemy.ext.asyncio import AsyncSession as RealAsyncSession

    engine, factory = _single_slot_factory(monkeypatch)
    original_connection = RealAsyncSession.connection
    original_rollback, original_close = Transaction.rollback, Transaction.close
    try:
        gen = real_get_db()
        session = await gen.__anext__()
        await session.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1

        _break_snapshot_rollback_and_close(monkeypatch)
        with pytest.raises(StopAsyncIteration):  # a successful response is not turned into an error
            await gen.__anext__()
        await gen.aclose()
        assert engine.pool.checkedout() == 0

        monkeypatch.setattr(RealAsyncSession, "connection", original_connection)
        monkeypatch.setattr(Transaction, "rollback", original_rollback)
        monkeypatch.setattr(Transaction, "close", original_close)
        assert await _next_request_succeeds(factory) == 1
    finally:
        await engine.dispose()

    log_text = capsys.readouterr().out
    for secret in (_SNAPSHOT_SECRET, _ROLLBACK_SECRET, _CLOSE_SECRET):
        assert secret not in log_text
    assert '"event": "db_session_cleanup_finally_snapshot_failed"' in log_text
    assert '"event": "db_session_cleanup_close_failed"' in log_text


async def test_fallback_never_invalidates_a_connection_that_was_already_released(monkeypatch):
    """After a normal request the remembered `Connection` is closed. The fallback must skip it:
    the underlying pooled connection may already belong to the next request."""
    from app.db.session import _invalidate_after_failure

    engine, factory = _single_slot_factory(monkeypatch)
    try:
        async with factory() as first:
            pid_before = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await first.commit()
        await _invalidate_after_failure(first, None, event="db_session_cleanup_invalidate_failed")
        async with factory() as second:
            pid_after = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        assert pid_after == pid_before, "the pooled connection must have been reused, not discarded"
    finally:
        await engine.dispose()
