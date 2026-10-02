"""SEC-AUTH-02: independent PostgreSQL transactions and persisted reuse revocation."""

import asyncio
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, REFRESH_COOKIE_NAME
from app.application import auth_service
from app.core.security import decode_token
from app.db.session import get_db
from app.main import app

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def per_request_client(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    previous = dict(app.dependency_overrides)

    async def _override():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def _login(client, email):
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.cookies[REFRESH_COOKIE_NAME], response.cookies[CSRF_COOKIE_NAME]


async def _request(client, token, csrf, operation="refresh"):
    # Explicit Cookie header avoids sharing a mutating cookie jar between requests.
    return await client.post(
        f"/api/v1/auth/{operation}",
        headers={
            CSRF_HEADER_NAME: csrf,
            "Cookie": f"{REFRESH_COOKIE_NAME}={token}; {CSRF_COOKIE_NAME}={csrf}",
        },
    )


async def _live_jtis(engine, user_id):
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT jti FROM refresh_token WHERE user_id = :uid AND revoked_at IS NULL"),
            {"uid": user_id},
        )
        return set(result.scalars())


async def _wait_for_blocked(engine, user_id, count):
    """Observe distinct PostgreSQL backends waiting on actual locks, not a sleep race."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            blocked = (
                await connection.execute(
                    text(
                        "SELECT count(DISTINCT pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock' AND state = 'active' "
                        "AND (query LIKE '%app_user%' OR query LIKE '%refresh_token%')"
                    )
                )
            ).scalar_one()
        if blocked >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Did not observe {count} independently blocked database connections for {user_id}")


async def test_replaying_a_rotated_refresh_token_revokes_the_whole_session(
    per_request_client, make_user, db_engine
):
    user = await make_user("reuse@example.com", PASSWORD, "Viewer")
    token, csrf = await _login(per_request_client, user.email)
    response = await _request(per_request_client, token, csrf)
    assert response.status_code == 200
    successor = response.cookies[REFRESH_COOKIE_NAME]
    assert await _live_jtis(db_engine, user.id) == {decode_token(successor, expected_type="refresh")["jti"]}
    assert (await _request(per_request_client, token, csrf)).status_code == 401
    # A separate connection after dependency rollback, before any further HTTP call.
    assert await _live_jtis(db_engine, user.id) == set()
    assert (await _request(per_request_client, successor, csrf)).status_code == 401


async def test_concurrent_refresh_with_one_token_mints_only_one_successor(
    per_request_client, make_user, db_engine
):
    user = await make_user("race@example.com", PASSWORD, "Viewer")
    token, csrf = await _login(per_request_client, user.email)
    # Hold the token in another transaction; all six requests must queue in PostgreSQL.
    async with db_engine.connect() as blocker:
        transaction = await blocker.begin()
        await blocker.execute(
            text("SELECT id FROM refresh_token WHERE user_id = :uid FOR UPDATE"), {"uid": user.id}
        )
        tasks = [asyncio.create_task(_request(per_request_client, token, csrf)) for _ in range(6)]
        try:
            await _wait_for_blocked(db_engine, user.id, 6)
        finally:
            await transaction.rollback()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    assert sorted(response.status_code for response in results) == [200, 401, 401, 401, 401, 401]
    assert await _live_jtis(db_engine, user.id) == set()


async def test_reuse_cannot_miss_another_tokens_in_flight_successor(
    per_request_client, make_user, db_engine, monkeypatch
):
    user = await make_user("cross-token@example.com", PASSWORD, "Viewer")
    first, csrf = await _login(per_request_client, user.email)
    assert (await _request(per_request_client, first, csrf)).status_code == 200
    other, other_csrf = await _login(per_request_client, user.email)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = auth_service.issue_tokens

    async def paused_issue(db, *, user):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        return await original(db, user=user)

    monkeypatch.setattr(auth_service, "issue_tokens", paused_issue)
    rotation = asyncio.create_task(_request(per_request_client, other, other_csrf))
    replay = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        replay = asyncio.create_task(_request(per_request_client, first, csrf))
        await _wait_for_blocked(db_engine, user.id, 1)
    finally:
        release.set()
        rotation_result = await asyncio.wait_for(rotation, timeout=15)
        if replay is not None:
            replay_result = await asyncio.wait_for(replay, timeout=15)
    assert rotation_result.status_code == 200
    assert replay_result.status_code == 401
    assert await _live_jtis(db_engine, user.id) == set()


async def test_competing_reuse_requests_do_not_deadlock(per_request_client, make_user, db_engine):
    user = await make_user("two-replays@example.com", PASSWORD, "Viewer")
    first, csrf = await _login(per_request_client, user.email)
    second, csrf_second = await _login(per_request_client, user.email)
    assert (await _request(per_request_client, first, csrf)).status_code == 200
    assert (await _request(per_request_client, second, csrf_second)).status_code == 200
    async with db_engine.connect() as blocker:
        transaction = await blocker.begin()
        await blocker.execute(text("SELECT id FROM app_user WHERE id = :uid FOR UPDATE"), {"uid": user.id})
        tasks = [
            asyncio.create_task(_request(per_request_client, first, csrf)),
            asyncio.create_task(_request(per_request_client, second, csrf_second)),
        ]
        try:
            await _wait_for_blocked(db_engine, user.id, 2)
        finally:
            await transaction.rollback()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    assert [response.status_code for response in results] == [401, 401]
    assert await _live_jtis(db_engine, user.id) == set()


async def test_logout_then_replay_revokes_other_sessions(per_request_client, make_user, db_engine):
    user = await make_user("logout-reuse@example.com", PASSWORD, "Viewer")
    token, csrf = await _login(per_request_client, user.email)
    await _login(per_request_client, user.email)
    assert (await _request(per_request_client, token, csrf, operation="logout")).status_code == 204
    assert len(await _live_jtis(db_engine, user.id)) == 1
    assert (await _request(per_request_client, token, csrf)).status_code == 401
    assert await _live_jtis(db_engine, user.id) == set()


@pytest.mark.parametrize("token", ["malformed", "", "a.b.c"])
async def test_malformed_refresh_returns_401(per_request_client, token):
    assert (await _request(per_request_client, token, "test-csrf")).status_code == 401


async def test_unknown_and_database_expired_tokens_do_not_revoke_live_sessions(
    per_request_client, make_user, db_engine
):
    from app.core.security import create_token

    user = await make_user("invalid-state@example.com", PASSWORD, "Viewer")
    token, csrf = await _login(per_request_client, user.email)
    unknown, _, _ = create_token(subject=str(user.id), token_type="refresh")
    before = await _live_jtis(db_engine, user.id)
    assert (await _request(per_request_client, unknown, csrf)).status_code == 401
    assert await _live_jtis(db_engine, user.id) == before
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE refresh_token SET expires_at = now() - interval '1 second' WHERE user_id = :uid"),
            {"uid": user.id},
        )
    assert (await _request(per_request_client, token, csrf)).status_code == 401
    assert await _live_jtis(db_engine, user.id) == before


async def test_reuse_logging_has_only_fixed_safe_fields(per_request_client, make_user, monkeypatch):
    captured = []

    class Recorder:
        def warning(self, event, **fields):
            captured.append((event, fields))

    monkeypatch.setattr(auth_service, "logger", Recorder())
    user = await make_user("logging@example.com", PASSWORD, "Viewer")
    token, csrf = await _login(per_request_client, user.email)
    assert (await _request(per_request_client, token, csrf)).status_code == 200
    assert (await _request(per_request_client, token, csrf)).status_code == 401
    assert captured == [("auth.refresh_token_reuse_detected", {"user_id": str(user.id)})]
    assert token not in repr(captured)
    assert PASSWORD not in repr(captured)


async def test_persistence_regression_detects_a_removed_commit(
    per_request_client, make_user, db_engine, monkeypatch
):
    """Run the SAME persistence regression with commit suppressed; it must fail."""
    original = auth_service._revoke_all_for_user_and_commit

    async def without_commit(db, *, user_id):
        real_commit = db.commit

        async def no_commit():
            pass

        db.commit = no_commit
        try:
            await original(db, user_id=user_id)
        finally:
            db.commit = real_commit

    monkeypatch.setattr(auth_service, "_revoke_all_for_user_and_commit", without_commit)
    with pytest.raises(AssertionError):
        await test_replaying_a_rotated_refresh_token_revokes_the_whole_session(
            per_request_client, make_user, db_engine
        )
    # The failure was the fresh-connection persistence assertion: successor remains live.
    async with db_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM refresh_token WHERE revoked_at IS NULL"))
        ).scalar_one() == 1
