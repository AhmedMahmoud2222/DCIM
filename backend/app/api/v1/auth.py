"""§31 Security Hardening, implemented literally: refresh token lives only in an
HttpOnly/Secure/SameSite=Strict cookie, never in a JSON body or localStorage-reachable
response — a stolen access token (short-lived, kept in memory by the frontend) is far
less damaging than a stolen long-lived refresh token would be. The refresh/logout
endpoints additionally require a double-submit CSRF token (a non-HttpOnly cookie the
frontend echoes back as a header) precisely because they are cookie-authenticated."""

import secrets
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.application import auth_service
from app.application.audit_service import write_audit_log
from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain.auth.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "dcim_refresh_token"
CSRF_COOKIE_NAME = "dcim_csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _set_auth_cookies(response: Response, *, refresh_token: str) -> str:
    settings = get_settings()
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path="/api/v1/auth",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=settings.is_production,
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path="/api/v1/auth",
    )
    return csrf_token


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/api/v1/auth")


def _require_csrf_match(request: Request, x_csrf_token: str | None) -> None:
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_value or not x_csrf_token or not secrets.compare_digest(cookie_value, x_csrf_token):
        raise ApiError(status_code=403, title="Forbidden", detail="CSRF token missing or invalid.")


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> AccessTokenResponse:
    user = await auth_service.authenticate(db, email=body.email, password=body.password)
    access_token, refresh_token = await auth_service.issue_tokens(db, user=user)
    _set_auth_cookies(response, refresh_token=refresh_token)

    await write_audit_log(
        db,
        actor_user_id=user.id,
        action="user.login",
        entity_type="user",
        entity_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        correlation_id=getattr(request.state, "correlation_id", None),
        source="api",
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return AccessTokenResponse(access_token=access_token)


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER_NAME),
) -> AccessTokenResponse:
    _require_csrf_match(request, x_csrf_token)
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise ApiError(status_code=401, title="Unauthorized", detail="No refresh token cookie present.")

    access_token, new_refresh_token, _ = await auth_service.rotate_refresh_token(db, refresh_token=refresh_token)
    _set_auth_cookies(response, refresh_token=new_refresh_token)
    await db.commit()
    return AccessTokenResponse(access_token=access_token)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER_NAME),
) -> None:
    _require_csrf_match(request, x_csrf_token)
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if refresh_token:
        await auth_service.revoke_refresh_token(db, refresh_token=refresh_token)
        await db.commit()
    _clear_auth_cookies(response)


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, full_name=user.full_name)
