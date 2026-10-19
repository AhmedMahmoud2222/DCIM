"""Issue #176: deterministic JWT expiry / clock-step behaviour for authenticated tests.

Background (#163/#170): a host wall-clock step (~3h20m, leaving Modern Standby) expired a
15-minute test token and produced an intermittent 401. These tests reproduce that
mechanism without sleeping and pin the production policy that makes it fail closed:
15-minute access tokens, zero leeway, expiry always enforced.
"""

import asyncio
from datetime import UTC, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_token, decode_token

LIFETIME = timedelta(minutes=15)
PASSWORD = "correct horse battery staple"


async def _login_token(client, make_user, email="clock-user@example.com") -> str:
    await make_user(email, PASSWORD, "Administrator")
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_production_token_policy_is_unchanged():
    assert get_settings().access_token_expire_minutes == 15


def test_expiry_is_enforced_with_zero_leeway(jwt_clock):
    token, _, expires_at = create_token(subject="user-1", token_type="access")
    assert expires_at - jwt_clock.now() == LIFETIME
    jwt_clock.advance(LIFETIME - timedelta(seconds=1))
    assert decode_token(token, expected_type="access")["sub"] == "user-1"
    jwt_clock.advance(timedelta(seconds=1))  # exactly exp: no grace period
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, expected_type="access")


async def test_api_rejects_expired_token_and_accepts_it_just_before(client, make_user, jwt_clock):
    token = await _login_token(client, make_user)
    jwt_clock.advance(LIFETIME - timedelta(seconds=1))
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 200
    jwt_clock.advance(timedelta(seconds=1))
    response = await client.get("/api/v1/auth/me", headers=_bearer(token))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


async def test_forward_clock_step_expires_token_then_relogin_recovers(client, make_user, jwt_clock):
    """The observed incident: a ~3h20m step expires a token that was valid moments earlier."""
    token = await _login_token(client, make_user)
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 200
    jwt_clock.advance(timedelta(hours=3, minutes=20))
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 401
    fresh = await client.post("/api/v1/auth/login", json={"email": "clock-user@example.com", "password": PASSWORD})
    assert fresh.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=_bearer(fresh.json()["access_token"]))).status_code == 200


async def test_backward_clock_step_rejects_future_issued_token(client, make_user, jwt_clock):
    """A token whose iat is in the (stepped-back) future is rejected too, never silently trusted."""
    token = await _login_token(client, make_user)
    jwt_clock.advance(-timedelta(minutes=5))
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 401


async def test_repeated_requests_with_session_relogin_only_on_real_expiry(client, token_session, jwt_clock):
    session = await token_session(clock=jwt_clock)
    for _ in range(25):  # 25 minutes of one request per minute
        response = await client.get("/api/v1/auth/me", headers=await session.headers())
        assert response.status_code == 200, response.text
        jwt_clock.advance(timedelta(minutes=1))
    assert session.logins == 2  # initial login + exactly one after the 15-minute token expired


async def test_session_does_not_hide_a_401_for_an_unexpired_token(client, token_session, jwt_clock):
    session = await token_session(clock=jwt_clock)
    headers = await session.headers()
    await client.post("/api/v1/auth/logout", headers=headers)  # no-op for access token; token stays unexpired
    tampered = {"Authorization": headers["Authorization"][:-1] + ("A" if headers["Authorization"][-1] != "A" else "B")}
    assert (await client.get("/api/v1/auth/me", headers=tampered)).status_code == 401
    assert session.logins == 1  # an unexpired token never triggers a silent re-login


async def test_clock_fixture_is_scoped_to_the_test(jwt_clock):
    from app.core import security

    assert security.datetime is not __import__("datetime").datetime


async def test_forged_signature_is_rejected_while_the_clock_is_controlled(client, make_user, jwt_clock):
    """A wrong-key token keeps a valid, unexpired payload and must still fail (issue #176)."""
    settings = get_settings()
    forged = jwt.encode(
        {
            "sub": "nobody",
            "type": "access",
            "iat": jwt_clock.now(),
            "exp": jwt_clock.now() + LIFETIME,
            "jti": "forged",
        },
        "an-entirely-different-32-byte-signing-key!",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(forged, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    response = await client.get("/api/v1/auth/me", headers=_bearer(forged))
    assert response.status_code == 401
    # The same frozen instant still accepts a genuinely signed token: the 401 is about the
    # signature, not the clock, so clock control can never paper over a key mismatch.
    token = await _login_token(client, make_user, email="signature-user@example.com")
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 200


async def test_refresh_token_is_never_accepted_as_an_access_token(client, make_user, jwt_clock):
    user = await make_user("type-user@example.com", PASSWORD, "Administrator")
    refresh, _, refresh_exp = create_token(subject=str(user.id), token_type="refresh")
    # The refresh token is genuinely valid at this frozen instant: only its type is wrong.
    assert refresh_exp > jwt_clock.now()
    assert decode_token(refresh, expected_type="refresh")["sub"] == str(user.id)
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(refresh, expected_type="access")
    response = await client.get("/api/v1/auth/me", headers=_bearer(refresh))
    assert response.status_code == 401
    assert "access token" in response.json()["detail"].lower()


async def test_permission_denial_is_a_403_until_the_token_actually_expires(client, make_user, jwt_clock):
    """Authn failure (401) and authz denial (403) stay distinct under clock control."""
    await make_user("viewer-clock@example.com", PASSWORD, "Viewer")
    login = await client.post("/api/v1/auth/login", json={"email": "viewer-clock@example.com", "password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    denied = await client.get("/api/v1/users", headers=_bearer(token))  # Viewer lacks user:read
    assert denied.status_code == 403
    assert "user:read" in denied.json()["detail"]

    jwt_clock.advance(LIFETIME + timedelta(seconds=1))
    expired = await client.get("/api/v1/users", headers=_bearer(token))
    assert expired.status_code == 401  # expiry is detected before permission is considered
    assert "expired" in expired.json()["detail"].lower()


async def test_async_tasks_observe_the_controlled_clock_and_production_time_is_restored():
    """Async isolation: spawned tasks see the controlled clock; teardown restores real time.

    Production ``datetime`` bindings in every patched module are verified back to their
    original objects, so one test's clock can never leak into another test's event loop.
    """
    from app.application import auth_service
    from app.core import security
    from tests.jwt_clock import ControlledClock

    production_datetime = (security.datetime, jwt.api_jwt.datetime, auth_service.datetime)

    patcher = pytest.MonkeyPatch()
    clock = ControlledClock().install(patcher)
    try:
        before, after = [], []

        async def sample(target):
            await asyncio.sleep(0)  # run as an independent task on the loop
            target.append(security.datetime.now(UTC))

        await asyncio.create_task(sample(before))
        clock.advance(timedelta(hours=3, minutes=20))  # the Modern Standby step from #163
        await asyncio.create_task(sample(after))
        assert after[0] - before[0] == timedelta(hours=3, minutes=20)  # real elapsed time is ~0

        token, _, expires_at = create_token(subject="task-user", token_type="access")
        assert expires_at - clock.now() == LIFETIME
        clock.advance(LIFETIME)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(token, expected_type="access")  # still enforced inside the async test
    finally:
        patcher.undo()

    assert (security.datetime, jwt.api_jwt.datetime, auth_service.datetime) == production_datetime
