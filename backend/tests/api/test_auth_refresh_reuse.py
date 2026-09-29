"""SEC-AUTH-02 (Issue #55): refresh-token reuse detection and rotation race. Both tests
fail against the pre-fix code."""

import asyncio

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, REFRESH_COOKIE_NAME
from app.db.session import get_db
from app.main import app

PASSWORD = "correct horse battery staple"


async def _login(client, email: str, password: str):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def _login_cookies(client, email):
    resp = await _login(client, email, PASSWORD)
    assert resp.status_code == 200
    return resp.cookies[REFRESH_COOKIE_NAME], resp.cookies[CSRF_COOKIE_NAME]


async def _refresh(client, refresh_token: str, csrf: str):
    client.cookies.clear()
    return await client.post(
        "/api/v1/auth/refresh",
        headers={CSRF_HEADER_NAME: csrf},
        cookies={REFRESH_COOKIE_NAME: refresh_token, CSRF_COOKIE_NAME: csrf},
    )


async def test_replaying_a_rotated_refresh_token_revokes_the_whole_session(client, make_user):
    await make_user("reuse@example.com", PASSWORD, "Viewer")
    old_token, csrf = await _login_cookies(client, "reuse@example.com")

    rotated = await _refresh(client, old_token, csrf)
    assert rotated.status_code == 200
    new_token = rotated.cookies[REFRESH_COOKIE_NAME]

    # Attacker replays the stolen (already rotated) token.
    assert (await _refresh(client, old_token, csrf)).status_code == 401

    # The legitimate successor must now be dead as well: reuse signals theft.
    assert (await _refresh(client, new_token, csrf)).status_code == 401


@pytest_asyncio.fixture
async def per_request_client(db_engine):
    """Each request gets its own session/transaction, unlike the shared-session `client`,
    so concurrent requests genuinely race in PostgreSQL."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_concurrent_refresh_with_one_token_mints_only_one_successor(per_request_client, make_user, db_engine):
    client = per_request_client
    await make_user("race@example.com", PASSWORD, "Viewer")
    token, csrf = await _login_cookies(client, "race@example.com")

    results = await asyncio.gather(*[_refresh(client, token, csrf) for _ in range(6)], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception) and r.status_code == 200]
    assert len(ok) <= 1

    async with db_engine.connect() as conn:
        live = (await conn.execute(text("SELECT count(*) FROM refresh_token WHERE revoked_at IS NULL"))).scalar_one()
    assert live <= 1
