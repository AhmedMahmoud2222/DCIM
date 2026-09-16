import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_token(*, subject: str, token_type: TokenType, extra_claims: dict[str, Any] | None = None) -> tuple[str, str, datetime]:
    """Returns (encoded_jwt, jti, expires_at). jti lets refresh tokens be revoked server-side."""
    settings = get_settings()
    now = datetime.now(UTC)
    if token_type == "access":
        expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    else:
        expires_at = now + timedelta(days=settings.refresh_token_expire_days)

    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": expires_at,
        "jti": jti,
    }
    if extra_claims:
        payload.update(extra_claims)

    encoded = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded, jti, expires_at


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected token type {expected_type!r}")
    return payload
