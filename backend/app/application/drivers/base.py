"""Protocol/Driver abstraction (ARCHITECTURE_REVIEW.md §20): "the interface contract
(`connect/poll/disconnect/normalize`)". Every concrete driver (ICMP/SNMP/REST)
implements exactly this interface -- nothing outside `app/application/drivers/`
branches on protocol/vendor identity. The critical architectural rule the master
prompt states directly: "Protocol/vendor-specific behavior must not leak into core
domain logic" -- `app/application/collector_service.py`'s polling orchestrator calls
`driver.poll(...)` uniformly, never `if integration.integration_type == "snmp": ...`.

`normalize()` is the seam between "whatever this protocol/vendor returned" and the
`AcquisitionResult` the rest of the system (discovery, and eventually Phase 9
telemetry) actually consumes -- vendor/protocol quirks are absorbed here, once, per
driver, never downstream."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class DriverConnectionError(Exception):
    """Raised by `connect()`/`poll()` when the target device could not be reached or
    did not respond -- a normal, expected outcome for a single unreachable device, NOT
    a programming error. Callers (the polling orchestrator) must catch this per-device
    and continue with the next one (master prompt §14: "Failure Isolation" -- one
    device's failure must not affect another)."""

    integration_id: uuid.UUID
    reason: str


@dataclass
class AcquisitionResult:
    """The normalized output of one successful poll -- the one shape every driver
    produces, regardless of protocol. `external_identifier` is the acquisition-side
    identity `DiscoveredDevice.external_identifier` is keyed on (never a `ManagedAsset`
    id -- that link, if any, is created only through human reconciliation).
    `raw_attributes` carries whatever protocol/vendor-specific detail was observed,
    opaque to everything outside the driver that produced it."""

    external_identifier: str
    observed_at: datetime
    raw_attributes: dict = field(default_factory=dict)
    # Reserved for Phase 9 (never populated or consumed in this phase): a driver MAY
    # already know a normalized metric name/value pair (e.g. "round_trip_ms" for ICMP),
    # carried here so Phase 9's own ingestion path does not have to re-derive it from
    # raw_attributes, without this phase depending on any Phase 9 storage.
    metrics: dict = field(default_factory=dict)


class ProtocolDriver(ABC):
    """§20's `connect/poll/disconnect/normalize` contract. `poll()` is expected to call
    `connect()` implicitly if not already connected (drivers may be stateless per call,
    e.g. ICMP/REST, or hold a session, e.g. a hypothetical persistent SNMP session) --
    this base class does not mandate which, only that `disconnect()` is always safe to
    call, idempotently, including on a driver that never connected."""

    protocol_code: str

    def __init__(self, integration_id: uuid.UUID) -> None:
        self.integration_id = integration_id

    @abstractmethod
    async def connect(self, *, target_host: str, target_port: int | None, config: dict, credential: str | None) -> None: ...

    @abstractmethod
    async def poll(self) -> AcquisitionResult: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    def normalize(self, raw: dict) -> dict:
        """Default: pass through. Overridden by drivers whose wire format needs real
        translation (e.g. a future vendor-specific SNMP MIB profile)."""
        return raw


def utcnow() -> datetime:
    return datetime.now(UTC)
