"""Login, refresh, logout per §31 Security Hardening: 15-minute access token, 7-day
rotating refresh token, refresh tokens tracked server-side by jti for revocation."""

import uuid
from datetime import UTC, datetime

import jwt
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import create_token, decode_token, verify_password
from app.domain.auth.models import RefreshToken, User

logger = structlog.get_logger(__name__)


class InvalidCredentialsError(ApiError):
    def __init__(self):
        super().__init__(status_code=401, title="Unauthorized", detail="Invalid email or password.")


class InvalidRefreshTokenError(ApiError):
    def __init__(self):
        super().__init__(status_code=401, title="Unauthorized", detail="Refresh token is invalid, expired, or revoked.")


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User:
    stmt = select(User).where(User.email == email.lower(), User.is_active.is_(True))
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentialsError()
    return user


async def issue_tokens(db: AsyncSession, *, user: User) -> tuple[str, str]:
    access_token, _, _ = create_token(subject=str(user.id), token_type="access")
    refresh_token, jti, expires_at = create_token(subject=str(user.id), token_type="refresh")

    db.add(RefreshToken(id=uuid.uuid4(), user_id=user.id, jti=jti, expires_at=expires_at))
    await db.flush()
    return access_token, refresh_token


async def rotate_refresh_token(db: AsyncSession, *, refresh_token: str) -> tuple[str, str, User]:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        raise InvalidRefreshTokenError() from exc

    jti = payload["jti"]
    # FOR UPDATE serializes concurrent rotations of the same token: the second request
    # waits, then sees revoked_at set and takes the reuse path below instead of minting a
    # second live successor (SEC-AUTH-02).
    stmt = select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
    record = (await db.execute(stmt)).scalar_one_or_none()
    if record is None:
        raise InvalidRefreshTokenError()
    if record.revoked_at is not None:
        if record.expires_at >= datetime.now(UTC):
            await _revoke_all_for_user_and_commit(db, user_id=record.user_id)
        raise InvalidRefreshTokenError()
    if record.expires_at < datetime.now(UTC):
        raise InvalidRefreshTokenError()

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise InvalidRefreshTokenError()

    record.revoked_at = datetime.now(UTC)
    await db.flush()

    access_token, new_refresh_token = await issue_tokens(db, user=user)
    return access_token, new_refresh_token, user


async def _revoke_all_for_user_and_commit(db: AsyncSession, *, user_id: uuid.UUID) -> None:
    """A rotated token presented again means two parties hold the same session lineage
    (theft or a replayed cookie). Ends every live refresh token for the user. Committed
    here because the caller raises immediately afterwards and get_db() rolls back on
    exceptions, which would otherwise undo the revocation."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    logger.warning("auth.refresh_token_reuse_detected", user_id=str(user_id))
    await db.commit()


async def revoke_refresh_token(db: AsyncSession, *, refresh_token: str) -> None:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except jwt.PyJWTError:
        return
    stmt = select(RefreshToken).where(RefreshToken.jti == payload["jti"])
    record = (await db.execute(stmt)).scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await db.flush()
