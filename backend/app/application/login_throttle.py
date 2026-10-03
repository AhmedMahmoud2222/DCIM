"""Per-account login failure throttle (SEC-AUTH-01, Issue #54).

Fixed-window counter in Redis keyed by a SHA-256 of the lower-cased email, so the key
never holds the address itself and unknown emails are counted exactly like known ones.

Every attempt first reserves a slot with one atomic INCR. Only the first MAX_FAILURES
reservations in a window go on to verify the password; later ones are refused with 429
before any Argon2 work, so concurrent guesses cannot all be evaluated while the counter
still reads low. A successful login deletes the counter, which refunds its slot.

Redis outage fails open: a broken Redis must not lock every operator out of the
platform. Client creation, every command and the client cleanup all sit inside that
boundary; the failure is logged with fixed fields only and the login still runs.

Not covered here: per-IP limits (needs a trusted-proxy decision for X-Forwarded-For) and
distributed guessing across many accounts."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

import redis.asyncio as aioredis
import structlog

from app.core.config import get_settings
from app.core.errors import ApiError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

KEY_PREFIX = "dcim:login-fail:"
MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60


class LoginThrottledError(ApiError):
    def __init__(self, retry_after: int):
        super().__init__(
            status_code=429,
            title="Too Many Requests",
            detail="Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(max(retry_after, 1))},
        )


def _key(email: str) -> str:
    return KEY_PREFIX + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _client() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)


async def _guarded(operation: str, action: Callable[[aioredis.Redis], Awaitable[T]]) -> T | None:
    """Runs `action` against a fresh client. Client creation, the action and the cleanup are
    all inside the fail-open boundary; only LoginThrottledError escapes."""
    client: aioredis.Redis | None = None
    try:
        client = _client()
        return await action(client)
    except LoginThrottledError:
        raise
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        logger.warning("auth.login_throttle_unavailable", operation=operation)
        return None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - a failing close must not turn a login into a 500
                logger.warning("auth.login_throttle_unavailable", operation=f"{operation}_close")


async def reserve(email: str) -> None:
    """Takes one attempt slot for the account, atomically. Raises LoginThrottledError when the
    window's budget is already spent; the caller verifies the password only if this returns."""

    async def _take(client: aioredis.Redis) -> None:
        key = _key(email)
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
        if int(count) > MAX_FAILURES:
            raise LoginThrottledError(ttl if ttl and ttl > 0 else WINDOW_SECONDS)

    await _guarded("reserve", _take)


async def reset(email: str) -> None:
    """Clears the counter after a successful login."""

    async def _clear(client: aioredis.Redis) -> None:
        await client.delete(_key(email))

    await _guarded("reset", _clear)
