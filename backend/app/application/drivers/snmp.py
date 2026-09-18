"""SNMP driver: the architectural framework the master prompt asks for ("implement the
framework sufficiently for real SNMP polling architecture... do not attempt to
implement every vendor's MIB"), honestly scoped. This module implements the
*structural* separation the architecture requires -- transport / auth / OID / metric
mapping / vendor profile / normalized identity are distinct, swappable concerns -- and
is unit-tested against a `SimulatedSNMPTransport` (a fake, in-memory stand-in for a
real SNMP agent).

**Disclosed, not silently glossed over**: no real SNMP agent exists in this sandbox to
poll against, so nothing here has been exercised against real network SNMP traffic
(GETBULK, real ASN.1/BER encoding, a real device's MIB). Wiring a real transport (e.g.
`pysnmp`'s asyncio API) is a documented, deferred follow-up
(`PHASE8_EDGE_COLLECTOR_CONTRACT.md`'s open decisions), not implemented here -- doing
so is exactly the kind of "attempt every vendor's MIB" scope the master prompt
explicitly says to avoid for this phase. What IS real and tested: the abstraction
boundary itself (`SNMPTransport` protocol, `MetricMapping`, `SNMPDriver`'s own
`connect/poll/disconnect/normalize` contract), so a real transport can be substituted
later without changing anything above this driver."""

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.application.drivers.base import AcquisitionResult, DriverConnectionError, ProtocolDriver, utcnow

SNMP_VERSIONS = ("v1", "v2c", "v3")


@dataclass
class MetricMapping:
    """§20: the "metric mapping" layer -- one OID -> one normalized metric name. A
    vendor/device profile is a list of these; this phase does not implement a
    persistent catalog of them (that is squarely Phase 9's "metric mapping" table, per
    the architecture's own `Integration -> Collector -> ProtocolDriver -> Device ->
    Metric Mapping -> Integration` ownership chain) -- this dataclass is the in-memory
    shape a real catalog would eventually populate."""

    oid: str
    metric_name: str
    unit: str | None = None


class SNMPTransport(Protocol):
    """The one seam a real SNMP library implementation would fill. Deliberately
    minimal -- `get(oid) -> str | None` is the only operation this phase's normalized
    "poll one device, get back named metrics" use case needs; GETBULK/walk semantics
    are a Phase 9 concern (bulk metric collection at telemetry scale), not this
    phase's."""

    async def get(self, oid: str) -> str | None: ...
    async def close(self) -> None: ...


@dataclass
class SimulatedSNMPTransport:
    """Test/demo transport -- an in-memory dict standing in for a real SNMP agent's
    OID tree. NEVER used in production wiring (app/application/collector_service.py
    never constructs this directly for a real integration); exists solely so
    `SNMPDriver`'s own logic (auth/version handling, metric mapping, normalization) is
    genuinely exercised by a test without a real network dependency."""

    oid_values: dict[str, str] = field(default_factory=dict)
    closed: bool = False

    async def get(self, oid: str) -> str | None:
        return self.oid_values.get(oid)

    async def close(self) -> None:
        self.closed = True


class SNMPDriver(ProtocolDriver):
    protocol_code = "snmp"

    def __init__(
        self, integration_id: uuid.UUID, *, metric_mappings: list[MetricMapping] | None = None,
        transport_factory=None,
    ) -> None:
        self.integration_id = integration_id
        self.metric_mappings = metric_mappings or []
        # `transport_factory(target_host, target_port, version, community) -> SNMPTransport`
        # -- injected so tests supply `SimulatedSNMPTransport`; a real deployment would
        # supply a real pysnmp-backed factory (NOT built in this phase).
        self._transport_factory = transport_factory
        self._transport: SNMPTransport | None = None
        self._target_host: str | None = None

    async def connect(self, *, target_host: str, target_port: int | None, config: dict, credential: str | None) -> None:
        self._target_host = target_host
        version = config.get("version", "v2c")
        if version not in SNMP_VERSIONS:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"Unsupported SNMP version: {version!r}")
        if self._transport_factory is None:
            raise DriverConnectionError(
                integration_id=self.integration_id,
                reason=(
                    "No SNMP transport configured -- this phase ships the SNMP driver's "
                    "abstraction only, not a real network transport. See "
                    "PHASE8_EDGE_COLLECTOR_CONTRACT.md."
                ),
            )
        self._transport = self._transport_factory(target_host, target_port, version, credential)

    async def poll(self) -> AcquisitionResult:
        if self._transport is None or self._target_host is None:
            raise DriverConnectionError(integration_id=self.integration_id, reason="poll() called before connect().")
        metrics: dict[str, str] = {}
        raw: dict[str, str | None] = {}
        for mapping in self.metric_mappings:
            value = await self._transport.get(mapping.oid)
            raw[mapping.oid] = value
            if value is not None:
                metrics[mapping.metric_name] = value
        if not raw:
            raise DriverConnectionError(
                integration_id=self.integration_id, reason="No metric mappings configured for this SNMP integration."
            )
        return AcquisitionResult(
            external_identifier=self._target_host,
            observed_at=utcnow(),
            raw_attributes={"protocol": "snmp", "target_host": self._target_host, "oid_values": raw},
            metrics=metrics,
        )

    async def disconnect(self) -> None:
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
