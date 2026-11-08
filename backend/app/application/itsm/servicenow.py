"""ServiceNow Table API adapter (outbound only).

Uses the `incident` table with the native `correlation_id` field as the idempotency key: before creating, the
adapter looks the key up, so a retry after an ambiguous failure (the request may or may not have created a
ticket) adopts the existing record instead of making a second one. Authentication is HTTP Basic with a
dedicated least-privilege integration user (create/read/write on `incident` only); the password arrives
decrypted from the secret store for the duration of one call and is not logged or stored in any exception.

Remote values are untrusted: `sys_id` and `number` must match strict patterns and `state` is mapped to a fixed
enum, so provider text never reaches the database, the logs or the UI."""

import json
import re
from urllib.parse import quote

import httpx

from app.application.itsm.base import ItsmError, RemoteTicket, TicketPayload
from app.application.outbound_http import OutboundError, OutboundResponse, outbound_request

_SYS_ID = re.compile(r"^[0-9a-f]{32}$")
_NUMBER = re.compile(r"^[A-Z]{2,8}[0-9]{4,12}$")
_STATE = {"1": "new", "2": "in_progress", "3": "on_hold", "6": "resolved", "7": "closed", "8": "canceled"}
TABLE = "/api/now/table/incident"


def _error_for_status(response: OutboundResponse) -> ItsmError:
    s = response.status_code
    if s in (401, 403):
        return ItsmError("AUTH_FAILED", retryable=False, http_status=s)
    if s == 429:
        return ItsmError("RATE_LIMITED", retryable=True, http_status=s, retry_after=response.retry_after_seconds)
    if s == 408 or s >= 500:
        return ItsmError("HTTP_5XX", retryable=True, http_status=s, retry_after=response.retry_after_seconds)
    return ItsmError("HTTP_4XX", retryable=False, http_status=s)


def _record(body: bytes, status: int) -> dict:
    try:
        doc = json.loads(body)
        result = doc["result"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ItsmError("MALFORMED_RESPONSE", retryable=False, http_status=status) from exc
    if isinstance(result, list):
        if not result:
            return {}
        result = result[0]
    if not isinstance(result, dict):
        raise ItsmError("MALFORMED_RESPONSE", retryable=False, http_status=status)
    return result


def _remote(record: dict, status: int) -> RemoteTicket:
    sys_id = record.get("sys_id")
    if not isinstance(sys_id, str) or not _SYS_ID.match(sys_id):
        raise ItsmError("MALFORMED_RESPONSE", retryable=False, http_status=status)
    number = record.get("number")
    number = number if isinstance(number, str) and _NUMBER.match(number) else None
    state = record.get("state")
    return RemoteTicket(sys_id, number, _STATE.get(state if isinstance(state, str) else "", "unknown"), status)


class ServiceNowAdapter:
    def __init__(self, base_url: str, username: str, password: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._auth = (username, password)
        self._transport = transport

    def __repr__(self) -> str:  # never expose the password in logs or tracebacks
        return f"ServiceNowAdapter(base={self._base!r})"

    async def _call(self, method: str, path: str, body: dict | None = None) -> OutboundResponse:
        try:
            return await outbound_request(
                method,
                f"{self._base}{path}",
                headers={"Accept": "application/json"},
                json_body=body,
                auth=self._auth,
                transport=self._transport,
            )
        except OutboundError as exc:
            code = exc.code
            if code == "TARGET_BLOCKED":
                raise ItsmError("TARGET_BLOCKED", retryable=False) from exc
            if code == "RESPONSE_TOO_LARGE":
                raise ItsmError("RESPONSE_TOO_LARGE", retryable=False) from exc
            raise ItsmError(code if code in ("TIMEOUT", "CONNECTION_ERROR") else "CONNECTION_ERROR", retryable=True) from exc

    async def find(self, correlation_key: str) -> RemoteTicket | None:
        query = quote(f"correlation_id={correlation_key}", safe="")
        r = await self._call("GET", f"{TABLE}?sysparm_query={query}&sysparm_limit=1&sysparm_fields=sys_id,number,state")
        if not 200 <= r.status_code < 300:
            raise _error_for_status(r)
        record = _record(r.body, r.status_code)
        return _remote(record, r.status_code) if record else None

    async def create(self, payload: TicketPayload) -> RemoteTicket:
        r = await self._call("POST", f"{TABLE}?sysparm_fields=sys_id,number,state", self._body(payload))
        if not 200 <= r.status_code < 300:
            raise _error_for_status(r)
        return _remote(_record(r.body, r.status_code), r.status_code)

    async def update(self, external_id: str, payload: TicketPayload) -> RemoteTicket:
        if not _SYS_ID.match(external_id):
            raise ItsmError("MALFORMED_RESPONSE", retryable=False)
        body = self._body(payload)
        body.pop("correlation_id")  # never rewrite the key that makes retries idempotent
        r = await self._call("PATCH", f"{TABLE}/{external_id}?sysparm_fields=sys_id,number,state", body)
        if not 200 <= r.status_code < 300:
            raise _error_for_status(r)
        return _remote(_record(r.body, r.status_code), r.status_code)

    @staticmethod
    def _body(p: TicketPayload) -> dict:
        return {
            "short_description": p.short_description,
            "description": p.description,
            "correlation_id": p.correlation_key,
            "urgency": str(p.urgency),
            "impact": str(p.impact),
        }
