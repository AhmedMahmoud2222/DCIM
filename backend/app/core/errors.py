from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class ApiError(Exception):
    """Base for domain-raised errors that map directly to a problem+json response."""

    def __init__(self, *, status_code: int, title: str, detail: str, type_: str = "about:blank"):
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.type_ = type_
        super().__init__(detail)


class ConflictError(ApiError):
    def __init__(self, detail: str = "The resource was modified by another request."):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            title="Conflict",
            detail=detail,
            type_="https://dcim.internal/errors/conflict",
        )


class NotFoundError(ApiError):
    def __init__(self, detail: str = "The requested resource was not found."):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Not Found",
            detail=detail,
            type_="https://dcim.internal/errors/not-found",
        )


class ForbiddenError(ApiError):
    def __init__(self, detail: str = "You do not have permission to perform this action."):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Forbidden",
            detail=detail,
            type_="https://dcim.internal/errors/forbidden",
        )


class UnauthorizedCollectorError(ApiError):
    """Phase 8: the collector machine-trust boundary's own 401 (never the user-JWT
    `UnauthenticatedError` in app/api/deps.py -- a collector is not a user). Deliberately
    generic across every failure mode in app/application/collector_auth.py (unknown
    collector, disabled collector, expired timestamp, bad signature, replayed nonce) so
    an unauthenticated caller cannot distinguish which check failed."""

    def __init__(self, detail: str = "Collector authentication failed."):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Collector Unauthorized",
            detail=detail,
            type_="https://dcim.internal/errors/collector-unauthorized",
        )


def _problem(request: Request, *, status_code: int, title: str, detail: str, type_: str = "about:blank") -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content={
            "type": type_,
            "title": title,
            "status": status_code,
            "detail": detail,
            "instance": str(request.url.path),
            "request_id": request_id,
        },
        media_type="application/problem+json",
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _problem(request, status_code=exc.status_code, title=exc.title, detail=exc.detail, type_=exc.type_)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(request, status_code=exc.status_code, title=exc.detail or "Error", detail=str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            title="Validation Error",
            detail="One or more fields failed validation.",
            type_="https://dcim.internal/errors/validation",
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("db_integrity_error", request_id=getattr(request.state, "request_id", None), error=str(exc.orig))
        return _problem(
            request,
            status_code=status.HTTP_409_CONFLICT,
            title="Conflict",
            detail="The request conflicts with an existing resource or constraint.",
            type_="https://dcim.internal/errors/conflict",
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.error("unhandled_exception", request_id=request_id, error=str(exc), exc_info=True)
        return _problem(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal Server Error",
            detail="An unexpected error occurred. Reference the request ID when reporting this.",
        )
