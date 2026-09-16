import uuid

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import decode_token
from app.db.session import get_db as get_db  # re-exported for a single import point
from app.domain.auth.models import User

_bearer_scheme = HTTPBearer(auto_error=False)


class UnauthenticatedError(ApiError):
    def __init__(self, detail: str = "Authentication required."):
        super().__init__(status_code=401, title="Unauthorized", detail=detail)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise UnauthenticatedError()
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("Invalid or expired access token.") from exc

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise UnauthenticatedError("User no longer exists or is inactive.")
    return user
