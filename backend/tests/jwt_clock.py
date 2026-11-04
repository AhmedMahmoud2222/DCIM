"""Test-only control of the wall clock that JWT issuance and validation read (issue #176).

Production code is untouched: ``create_token`` and PyJWT's ``exp``/``iat`` validation
both call ``datetime.now`` on their own module-level ``datetime``, so the clock is
replaced in exactly those modules for the duration of one test. The clock starts at the
real current time and only moves when a test moves it, so it models a host wall-clock
step (NTP correction, leaving Modern Standby, a paused container) without sleeping.
JWT expiry checks stay fully enabled and no leeway is added.
"""

from datetime import UTC, datetime, timedelta

import jwt.api_jwt
import pytest

from app.application import auth_service
from app.core import security


class _AnyDatetime(type(datetime)):
    """Metaclass so code that does ``isinstance(value, datetime)`` still accepts real datetimes."""

    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class ControlledClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)
        owner = self

        class _Datetime(datetime, metaclass=_AnyDatetime):
            @classmethod
            def now(cls, tz=None):
                return owner.current.astimezone(tz) if tz else owner.current.replace(tzinfo=None)

        self._datetime = _Datetime

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ControlledClock":
        for module in (security, jwt.api_jwt, auth_service):
            monkeypatch.setattr(module, "datetime", self._datetime)
        return self

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    def jump_to(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("ControlledClock.jump_to requires a timezone-aware datetime")
        self.current = moment


class TokenSession:
    """Holds one user's access token and re-logs-in only once that token's own ``exp`` has passed.

    A 401 for a token that has not expired is a real failure and is never retried here, so
    a regression in authentication still surfaces. ``logins`` lets a test assert exactly
    how many logins happened.
    """

    def __init__(self, client, email: str, password: str, clock: ControlledClock | None = None) -> None:
        self._client, self._email, self._password, self._clock = client, email, password, clock
        self._token: str | None = None
        self._exp: datetime | None = None
        self.logins = 0

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(UTC)

    async def headers(self) -> dict:
        if self._token is None or self._exp is None or self._now() >= self._exp:
            response = await self._client.post("/api/v1/auth/login", json={"email": self._email, "password": self._password})
            assert response.status_code == 200, response.text
            self._token = response.json()["access_token"]
            claims = jwt.decode(self._token, options={"verify_signature": False, "verify_exp": False})
            self._exp = datetime.fromtimestamp(claims["exp"], UTC)
            self.logins += 1
        return {"Authorization": f"Bearer {self._token}"}
