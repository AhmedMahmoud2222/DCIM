import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger
from app.core.request_metrics import observe as observe_request

REQUEST_ID_HEADER = "X-Request-Id"
CORRELATION_ID_HEADER = "X-Correlation-Id"
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
logger = get_logger(__name__)


def _safe_id(value: str | None) -> str:
    """Accept bounded, log-safe caller IDs; replace malformed values with a fresh ID."""
    if value is not None and _SAFE_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Binds request_id/correlation_id to structlog contextvars and the request state so
    every log line, audit row, and outbox event written during this request can be traced
    back to it (§37/§25 of ARCHITECTURE_REVIEW.md)."""

    async def dispatch(self, request: Request, call_next):
        started_at = time.perf_counter()
        request_id = _safe_id(request.headers.get(REQUEST_ID_HEADER))
        raw_correlation_id = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = _safe_id(raw_correlation_id) if raw_correlation_id else request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, correlation_id=correlation_id)

        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        response: Response = await call_next(request)
        route = request.scope.get("route")
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "api_request_completed",
            method=request.method,
            route=getattr(route, "path", "unmatched"),
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        observe_request(
            method=request.method,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
