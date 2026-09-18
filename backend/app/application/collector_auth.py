"""Collector machine-to-machine trust boundary (master prompt §9: "Do not use the
normal end-user JWT mechanism as the collector trust mechanism. Collector
authentication is a machine-to-machine trust boundary."). A Collector never carries a
user JWT and never goes through `app.application.rbac`'s permission model -- it
authenticates with a per-request HMAC signature, verified here, never by
`app.api.deps.get_current_user`.

Request signing scheme (documented explicitly, per §9's own list):
- `X-Collector-Id`: the Collector's UUID.
- `X-Collector-Timestamp`: Unix seconds, the time the collector signed the request.
- `X-Collector-Nonce`: a random, collector-generated string, unique per request.
- `X-Collector-Signature`: hex HMAC-SHA256, keyed by the collector's own secret, over
  `f"{collector_id}.{timestamp}.{nonce}."` concatenated with the raw request body
  bytes. Signing the body (not just headers) gives payload integrity: a
  man-in-the-middle that alters the body without the secret cannot produce a valid
  signature for the altered content.

Why HMAC over a bare bearer secret: the collector's raw secret is never transmitted on
the wire at all, only a signature derived from it — this is real defense-in-depth
alongside TLS (§9's "TLS 1.3, mutual TLS or equivalent"), not a replacement for it; TLS
remains required in production for confidentiality, this scheme adds authenticity/
integrity that survives even a TLS-terminating intermediary the collector doesn't fully
trust. Verifying a signature requires the server to recompute it, which requires the
plaintext secret -- this is why `Collector.secret_ciphertext` is reversibly encrypted
(`app/core/secrets.py`'s Fernet primitive) rather than Argon2-hashed like a user
password (Argon2 is one-way; the server could never verify a signature against it).

Verified in this order (fail closed at the first failure, never partially trust a
request that fails a later check):
1. Collector exists and `status == "active"` (`registered`-but-not-yet-activated or
   `disabled` is rejected outright -- disabling a collector must take effect
   immediately, not just stop it from receiving new assignments).
2. Timestamp is within `REQUEST_TIMESTAMP_WINDOW_SECONDS` of server time (replay
   protection against an old captured request; also surfaces a badly-out-of-sync
   collector clock as a clear 401 rather than a confusing downstream failure).
3. HMAC signature verified via `hmac.compare_digest` (constant-time, never a `==`
   string comparison that could leak timing information about how much of the
   signature matched).
4. Nonce has never been seen before from this collector (`CollectorRequestNonce`'s
   unique constraint as an atomic claim, checked only AFTER the signature verifies --
   an attacker who cannot forge a valid signature should not be able to consume/burn a
   nonce and cause a legitimate later replay-check false negative). A captured, valid,
   in-window, correctly-signed request still cannot be replayed a second time."""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secrets import SecretDecryptionError, decrypt_secret
from app.domain.integration.models import Collector, CollectorRequestNonce

REQUEST_TIMESTAMP_WINDOW_SECONDS = 300

_NONCE_TABLE = cast(Table, CollectorRequestNonce.__table__)


class CollectorAuthError(Exception):
    """Any failure in the verification chain -- deliberately one exception type for
    every failure mode (unknown collector, disabled collector, expired timestamp, bad
    signature, replayed nonce) so the API layer maps all of them to the same 401
    without leaking *which* check failed to an unauthenticated caller."""


def generate_collector_secret() -> str:
    """32 bytes of CSPRNG entropy, url-safe-encoded -- same entropy class as a JWT
    signing key, generated once and shown to the caller exactly once."""
    return secrets.token_urlsafe(32)


def compute_signature(*, secret: str, collector_id: uuid.UUID, timestamp: str, nonce: str, raw_body: bytes) -> str:
    message = f"{collector_id}.{timestamp}.{nonce}.".encode() + raw_body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def claim_nonce(db: AsyncSession, *, collector_id: uuid.UUID, nonce: str) -> bool:
    """Atomic claim via the unique (collector_id, nonce) index -- returns True if this
    is the first time this nonce has been seen from this collector, False if it is a
    replay. Committed immediately (matching `IdempotencyKey`'s own claim pattern) so
    the claim is visible to other concurrent requests under MVCC, not just at the end
    of this request's own transaction."""
    stmt = (
        pg_insert(_NONCE_TABLE)
        .values(id=uuid.uuid4(), collector_id=collector_id, nonce=nonce, seen_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["collector_id", "nonce"])
        .returning(_NONCE_TABLE.c.id)
    )
    result = await db.execute(stmt)
    claimed = result.scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def verify_collector_request(
    db: AsyncSession,
    *,
    collector_id_header: str,
    timestamp_header: str,
    nonce_header: str,
    signature_header: str,
    raw_body: bytes,
) -> Collector:
    """Returns the authenticated, active `Collector` or raises `CollectorAuthError`."""
    try:
        collector_id = uuid.UUID(collector_id_header)
    except (ValueError, TypeError) as exc:
        raise CollectorAuthError("Malformed collector identifier.") from exc

    collector = await db.get(Collector, collector_id)
    if collector is None or collector.status != "active":
        raise CollectorAuthError("Unknown or inactive collector.")

    # Finding I1 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): bound the header's length
    # before parsing (a real Unix-seconds timestamp is never more than ~11 digits for
    # millennia; this also keeps `int()` itself cheap, though CPython's own integer-
    # string-conversion limit already guards against a pathologically long digit
    # string), and compare entirely in integer epoch-seconds space -- never construct
    # a `datetime` from the untrusted value. `datetime.fromtimestamp()` raises
    # `OverflowError`/`ValueError`/`OSError` (the exact set differs by platform and by
    # how far out of range the value is) for a timestamp outside the platform's
    # representable range, and that call is NOT where this trust boundary should ever
    # need to look up a calendar date in the first place -- "is this timestamp within
    # N seconds of now" is a pure integer-arithmetic question, and integer arithmetic
    # in Python has no overflow limit at all, so this is the correct fix, not a broader
    # catch bolted onto the old approach.
    if not timestamp_header or len(timestamp_header) > 20:
        raise CollectorAuthError("Malformed timestamp.")
    try:
        ts = int(timestamp_header)
    except (ValueError, TypeError) as exc:
        raise CollectorAuthError("Malformed timestamp.") from exc
    now_ts = int(datetime.now(UTC).timestamp())
    if abs(now_ts - ts) > REQUEST_TIMESTAMP_WINDOW_SECONDS:
        raise CollectorAuthError("Request timestamp outside the accepted window.")

    if not nonce_header or len(nonce_header) > 64:
        raise CollectorAuthError("Malformed nonce.")
    if not signature_header or len(signature_header) != 64:
        raise CollectorAuthError("Malformed signature.")

    try:
        plaintext_secret = decrypt_secret(collector.secret_ciphertext)
    except SecretDecryptionError as exc:
        raise CollectorAuthError("Collector credential could not be verified.") from exc

    expected_signature = compute_signature(
        secret=plaintext_secret, collector_id=collector_id, timestamp=timestamp_header,
        nonce=nonce_header, raw_body=raw_body,
    )
    if not hmac.compare_digest(expected_signature, signature_header):
        raise CollectorAuthError("Signature verification failed.")

    if not await claim_nonce(db, collector_id=collector_id, nonce=nonce_header):
        raise CollectorAuthError("Nonce already used -- possible replay.")

    return collector
