import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"
CORRELATION_ID_HEADER = "X-Correlation-Id"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Binds request_id/correlation_id to structlog contextvars and the request state so
    every log line, audit row, and outbox event written during this request can be traced
    back to it (§37/§25 of ARCHITECTURE_REVIEW.md)."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, correlation_id=correlation_id)

        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
