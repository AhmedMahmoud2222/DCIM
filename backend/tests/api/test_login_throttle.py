"""SEC-AUTH-01 (Issue #54): login throttling and unknown-user timing parity. Each test
except the two guard tests (per-account isolation, reset on success) fails against the
pre-fix code."""

import pytest_asyncio
import redis.asyncio as aioredis

from app.application import auth_service, login_throttle
from app.core.config import get_settings

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture(autouse=True)
async def _clean_throttle_keys():
    async def _wipe() -> None:
        client = aioredis.from_url(get_settings().redis_url)
        try:
            async for key in client.scan_iter(match=f"{login_throttle.KEY_PREFIX}*"):
                await client.delete(key)
        finally:
            await client.aclose()

    await _wipe()
    yield
    await _wipe()


async def _login(client, email: str, password: str):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_repeated_failed_logins_are_throttled(client, make_user):
    await make_user("victim@example.com", PASSWORD, "Viewer")
    statuses = [(await _login(client, "victim@example.com", f"guess-{i}")).status_code for i in range(login_throttle.MAX_FAILURES + 3)]
    assert statuses[: login_throttle.MAX_FAILURES] == [401] * login_throttle.MAX_FAILURES
    assert set(statuses[login_throttle.MAX_FAILURES :]) == {429}


async def test_throttled_account_rejects_even_the_correct_password(client, make_user):
    await make_user("locked@example.com", PASSWORD, "Viewer")
    for i in range(login_throttle.MAX_FAILURES):
        await _login(client, "locked@example.com", f"guess-{i}")
    resp = await _login(client, "locked@example.com", PASSWORD)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_throttle_is_per_account_not_global(client, make_user):
    await make_user("a@example.com", PASSWORD, "Viewer")
    await make_user("b@example.com", PASSWORD, "Viewer")
    for i in range(login_throttle.MAX_FAILURES + 1):
        await _login(client, "a@example.com", f"guess-{i}")
    assert (await _login(client, "b@example.com", PASSWORD)).status_code == 200


async def test_successful_login_resets_the_failure_counter(client, make_user):
    await make_user("reset@example.com", PASSWORD, "Viewer")
    for i in range(login_throttle.MAX_FAILURES - 1):
        await _login(client, "reset@example.com", f"guess-{i}")
    assert (await _login(client, "reset@example.com", PASSWORD)).status_code == 200
    for i in range(login_throttle.MAX_FAILURES - 1):
        assert (await _login(client, "reset@example.com", f"again-{i}")).status_code == 401


async def test_unknown_email_is_throttled_too(client):
    codes = [(await _login(client, "ghost@example.com", "x")).status_code for _ in range(login_throttle.MAX_FAILURES + 1)]
    assert codes[-1] == 429


async def test_unknown_user_still_runs_a_password_hash(client, monkeypatch):
    calls = []
    real = auth_service.verify_password

    def spy(plain, hashed):
        calls.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr("app.application.auth_service.verify_password", spy)
    resp = await _login(client, "nobody-here@example.com", "whatever")
    assert resp.status_code == 401
    assert len(calls) == 1  # timing parity with the known-user path


async def test_login_still_works_when_redis_is_unreachable(client, make_user, monkeypatch):
    class _Unreachable:
        redis_url = "redis://127.0.0.1:1/0"

    monkeypatch.setattr(login_throttle, "get_settings", lambda: _Unreachable())
    await make_user("failopen@example.com", PASSWORD, "Viewer")
    assert (await _login(client, "failopen@example.com", PASSWORD)).status_code == 200
    assert (await _login(client, "failopen@example.com", "wrong")).status_code == 401
