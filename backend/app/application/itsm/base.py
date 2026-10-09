"""Provider-neutral ITSM boundary (Issue #103, area D).

The correlation and ticket workflow talks only to `ItsmAdapter`. Nothing outside an adapter module knows a
provider's field names, URLs or status codes, so a second provider is a new adapter, not a change to the
alarm or incident domain."""

from dataclasses import dataclass
from typing import Protocol

CONFIDENCE_URGENCY = {"high": 1, "medium": 2, "low": 3}


class ItsmError(Exception):
    """Fixed-vocabulary failure. `code` is one of `TICKET_FAILURE_CODES`; `retryable` says whether another
    attempt can help. The message never carries provider text, URLs or credentials."""

    def __init__(self, code: str, *, retryable: bool, http_status: int | None = None, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after = retry_after


@dataclass(frozen=True)
class TicketPayload:
    correlation_key: str
    short_description: str
    description: str
    urgency: int
    impact: int


@dataclass(frozen=True)
class RemoteTicket:
    external_id: str
    number: str | None
    state: str  # normalised: new | in_progress | on_hold | resolved | closed | canceled | unknown
    http_status: int | None = None


class ItsmAdapter(Protocol):
    async def find(self, correlation_key: str) -> RemoteTicket | None: ...

    async def create(self, payload: TicketPayload) -> RemoteTicket: ...

    async def update(self, external_id: str, payload: TicketPayload) -> RemoteTicket: ...
