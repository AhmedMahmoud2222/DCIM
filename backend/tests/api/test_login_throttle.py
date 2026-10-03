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
    statuses = [
        (await _login(client, "victim@example.com", f"guess-{i}")).status_code for i in range(login_throttle.MAX_FAILURES + 3)
    ]
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


# ------------------------------------------------------------------ adversarial concurrency and Redis failure
import asyncio  # noqa: E402

import pytest  # noqa: E402
import structlog  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.api import test_auth_refresh_reuse as _refresh_reuse  # noqa: E402

per_request_client = _refresh_reuse.per_request_client  # independent session per request


def _evaluations(monkeypatch, *, delay: float = 0.05) -> list[str]:
    """Records every password evaluation. The pause widens the window between 'allowed to
    proceed' and 'failure recorded', which is exactly where an unreserved counter leaks."""
    evaluated: list[str] = []
    real = auth_service.authenticate

    async def spy(db, *, email, password):
        evaluated.append(password)
        await asyncio.sleep(delay)
        return await real(db, email=email, password=password)

    monkeypatch.setattr(auth_service, "authenticate", spy)
    return evaluated


async def test_concurrent_guesses_cannot_exceed_the_ten_attempt_budget(per_request_client, make_user, monkeypatch):
    await make_user("burst@example.com", PASSWORD, "Viewer")
    evaluated = _evaluations(monkeypatch)
    burst = login_throttle.MAX_FAILURES * 3
    responses = await asyncio.gather(*[_login(per_request_client, "burst@example.com", f"guess-{i}") for i in range(burst)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [401] * login_throttle.MAX_FAILURES + [429] * (burst - login_throttle.MAX_FAILURES)
    assert len(evaluated) == login_throttle.MAX_FAILURES  # the rest were refused before any password work
    for refused in (r for r in responses if r.status_code == 429):
        assert 1 <= int(refused.headers["Retry-After"]) <= login_throttle.WINDOW_SECONDS


async def test_a_correct_password_inside_a_burst_never_widens_the_budget(per_request_client, make_user, monkeypatch):
    await make_user("mixed@example.com", PASSWORD, "Viewer")
    evaluated = _evaluations(monkeypatch)
    attempts = [f"guess-{i}" for i in range(login_throttle.MAX_FAILURES * 2)] + [PASSWORD]
    responses = await asyncio.gather(*[_login(per_request_client, "mixed@example.com", p) for p in attempts])
    statuses = [r.status_code for r in responses]
    assert set(statuses) <= {200, 401, 429}
    assert statuses.count(401) <= login_throttle.MAX_FAILURES
    assert len(evaluated) <= login_throttle.MAX_FAILURES + 1  # a success may refund its own slot, nothing more


async def test_a_burst_against_one_account_leaves_another_untouched(per_request_client, make_user, monkeypatch):
    await make_user("target@example.com", PASSWORD, "Viewer")
    await make_user("bystander@example.com", PASSWORD, "Viewer")
    _evaluations(monkeypatch, delay=0.01)
    await asyncio.gather(*[_login(per_request_client, "target@example.com", f"g-{i}") for i in range(30)])
    assert (await _login(per_request_client, "bystander@example.com", PASSWORD)).status_code == 200


async def test_inactive_user_takes_the_same_path_and_is_throttled(client, make_user, db_session, monkeypatch):
    await make_user("inactive@example.com", PASSWORD, "Viewer")
    await db_session.execute(text("UPDATE app_user SET is_active = false WHERE email = 'inactive@example.com'"))
    await db_session.commit()
    calls = []
    real = auth_service.verify_password
    monkeypatch.setattr(
        "app.application.auth_service.verify_password", lambda plain, hashed: (calls.append(hashed), real(plain, hashed))[1]
    )
    codes = [(await _login(client, "inactive@example.com", PASSWORD)).status_code for _ in range(login_throttle.MAX_FAILURES + 1)]
    assert codes == [401] * login_throttle.MAX_FAILURES + [429]
    assert len(calls) == login_throttle.MAX_FAILURES  # one hash per evaluated attempt, none once throttled


async def test_throttle_state_and_logs_never_hold_the_raw_email_or_password(client, make_user):
    email = "Sensitive.Person@example.com"
    await make_user(email.lower(), PASSWORD, "Viewer")
    with structlog.testing.capture_logs() as logs:
        for i in range(login_throttle.MAX_FAILURES + 1):
            await _login(client, email, f"wrong-{i}")
    redis = aioredis.from_url(get_settings().redis_url)
    try:
        keys = [k.decode() async for k in redis.scan_iter(match=f"{login_throttle.KEY_PREFIX}*")]
    finally:
        await redis.aclose()
    assert keys and all("sensitive" not in k.lower() and "example.com" not in k for k in keys)
    assert "sensitive" not in repr(logs).lower() and "wrong-" not in repr(logs)


class _Down:
    """A stand-in Redis client for outage shapes. Each flag breaks one stage."""

    def __init__(self, *, commands: bool, close: bool, real: aioredis.Redis | None = None):
        self.commands, self.close, self.real = commands, close, real

    def pipeline(self, *a, **k):
        if self.commands:
            raise ConnectionError("redis down")
        return self.real.pipeline(*a, **k)

    async def delete(self, *a, **k):
        if self.commands:
            raise ConnectionError("redis down")
        return await self.real.delete(*a, **k)

    async def aclose(self):
        if self.real is not None:
            await self.real.aclose()
        if self.close:
            raise TimeoutError("disconnect timeout")


@pytest.mark.parametrize("shape", ["client_creation", "commands", "cleanup"])
async def test_every_redis_failure_stage_fails_open_with_fixed_fields(client, make_user, monkeypatch, shape):
    await make_user("failopen2@example.com", PASSWORD, "Viewer")
    real_factory = login_throttle._client
    if shape == "client_creation":

        def broken():
            raise OSError("cannot build client")

        monkeypatch.setattr(login_throttle, "_client", broken)
    elif shape == "commands":
        monkeypatch.setattr(login_throttle, "_client", lambda: _Down(commands=True, close=False))
    else:
        monkeypatch.setattr(login_throttle, "_client", lambda: _Down(commands=False, close=True, real=real_factory()))
    with structlog.testing.capture_logs() as logs:
        assert (await _login(client, "failopen2@example.com", PASSWORD)).status_code == 200
        assert (await _login(client, "failopen2@example.com", "wrong")).status_code == 401
    warnings = [e for e in logs if e.get("event") == "auth.login_throttle_unavailable"]
    assert warnings and all(set(e) <= {"event", "log_level", "operation"} for e in warnings)
    assert "failopen2" not in repr(logs)


async def test_a_failing_client_close_does_not_disable_throttling(client, make_user, monkeypatch):
    await make_user("closefail@example.com", PASSWORD, "Viewer")
    real_factory = login_throttle._client
    monkeypatch.setattr(login_throttle, "_client", lambda: _Down(commands=False, close=True, real=real_factory()))
    codes = [
        (await _login(client, "closefail@example.com", f"g-{i}")).status_code for i in range(login_throttle.MAX_FAILURES + 1)
    ]
    assert codes == [401] * login_throttle.MAX_FAILURES + [429]


async def test_the_counter_expires_with_the_fixed_window_and_is_not_extended(client, make_user):
    await make_user("window@example.com", PASSWORD, "Viewer")
    await _login(client, "window@example.com", "guess-0")
    redis = aioredis.from_url(get_settings().redis_url)
    try:
        key = login_throttle._key("window@example.com")
        first = await redis.ttl(key)
        await asyncio.sleep(1.1)
        await _login(client, "window@example.com", "guess-1")
        second = await redis.ttl(key)
    finally:
        await redis.aclose()
    assert 0 < first <= login_throttle.WINDOW_SECONDS
    assert 0 < second < first
