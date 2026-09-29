"""Per-account login failure throttle (SEC-AUTH-01, Issue #54).

Fixed-window counter in Redis keyed by a SHA-256 of the lower-cased email, so the key
never holds the address itself and unknown emails are counted exactly like known ones.
Redis outage fails open: a broken Redis must not lock every operator out of the
platform. The failure is logged with fixed fields only and the login still runs.

Not covered here: per-IP limits (needs a trusted-proxy decision for X-Forwarded-For) and
distributed guessing across many accounts."""

import hashlib

import redis.asyncio as aioredis
import structlog

from app.core.config import get_settings
from app.core.errors import ApiError

logger = structlog.get_logger(__name__)

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


async def check(email: str) -> None:
    """Raises LoginThrottledError when the account has used up its failure budget."""
    client = _client()
    try:
        key = _key(email)
        count = await client.get(key)
        if count is not None and int(count) >= MAX_FAILURES:
            ttl = await client.ttl(key)
            raise LoginThrottledError(ttl if ttl and ttl > 0 else WINDOW_SECONDS)
    except LoginThrottledError:
        raise
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        logger.warning("auth.login_throttle_unavailable", operation="check")
    finally:
        await client.aclose()


async def record_failure(email: str) -> None:
    client = _client()
    try:
        key = _key(email)
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS, nx=True)
            await pipe.execute()
    except Exception:  # noqa: BLE001
        logger.warning("auth.login_throttle_unavailable", operation="record_failure")
    finally:
        await client.aclose()


async def reset(email: str) -> None:
    client = _client()
    try:
        await client.delete(_key(email))
    except Exception:  # noqa: BLE001
        logger.warning("auth.login_throttle_unavailable", operation="reset")
    finally:
        await client.aclose()
