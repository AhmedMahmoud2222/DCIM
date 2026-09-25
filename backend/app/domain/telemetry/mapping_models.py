"""Phase 10C: telemetry sensor bindings for instantiated port/power-inlet markers
(app/domain/physical/ports.py's `EquipmentPort`/`EquipmentPowerInlet`, Phase 10B), plus
the cached "latest status" row each binding's poller/ingest path keeps current.

Deliberately a separate module from app/domain/telemetry/models.py rather than an
addition to it: that module's `IntegrationMetricMapping`/`TelemetryReading` are the MVP
acquisition pipeline (§35/§37) keyed on `Integration`/`ManagedAsset` and designed for an
ever-growing historical series (retention job, daily aggregates). This module's
`PortTelemetryBinding` is keyed on the Phase 10B instantiated port/inlet identity
instead, and `TelemetryLatestStatus` is a single-row-per-binding cache, not a series —
"real-time status payloads" (rack-elevation overlays, impact simulation) need only the
current value, never a history, so there is deliberately no append-only reading table or
retention job here. A binding MAY be layered on top of an MVP `IntegrationMetricMapping`
for its actual acquisition transport (SNMP/Modbus poll, or the collector pipeline) or may
be driven directly by `POST /telemetry/port-status/ingest` (a PDU vendor API, a
synthetic/demo feed) — this module does not care which, `protocol`/`external_ref` are
free-form provenance for whichever poller owns this binding, exactly as
`IntegrationMetricMapping.source_identifier` already is for the MVP pipeline.

Target-type/reference-column exclusivity (`target_reference_matches_type`) follows the
same division of labor as `PowerNode.asset_reference_exclusive` (app/domain/power/models.py)
and `PortConnection.target_exclusive` (ports.py): a DB CHECK enforces the shape, while
`status_level`'s vocabulary (`UP`/`DOWN`/`DEGRADED` for `network_port`; `NORMAL`/
`WARNING`/`CRITICAL` for `power_inlet`/`environmental`) is validated by the service layer
(app/application/telemetry_service.py's `evaluate_*` functions) rather than a CHECK,
since which vocabulary applies depends on the binding's own `target_type` — a single
column cannot express a conditional CHECK across two different fixed value sets any more
cleanly than a service-layer validation already does everywhere else in this codebase."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

TELEMETRY_TARGET_TYPES = ("network_port", "power_inlet", "environmental")
TELEMETRY_PROTOCOLS = ("snmp", "modbus", "pdu_outlet", "synthetic")

LINK_STATES = ("UP", "DOWN", "DEGRADED")
THRESHOLD_STATUS_LEVELS = ("NORMAL", "WARNING", "CRITICAL")
# Every status_level a TelemetryLatestStatus row can hold, across both vocabularies —
# used only for a defensive CHECK; which subset applies to a given row is a service-layer
# concern (see module docstring).
ALL_STATUS_LEVELS = LINK_STATES + THRESHOLD_STATUS_LEVELS


class PortTelemetryBinding(Base, UUIDPkMixin, TimestampMixin):
    """One sensor binding for exactly one instantiated port, power inlet, or (for
    `target_type='environmental'`, which has no dedicated instantiated entity of its own)
    a whole piece of equipment. `equipment_port_id`/`equipment_power_inlet_id` are each
    UNIQUE — a marker has at most one live telemetry binding, matching
    `PortConnection.source_port_id`'s own one-cabling-edge-per-port discipline."""

    __tablename__ = "port_telemetry_binding"
    __table_args__ = (
        CheckConstraint(f"target_type IN {TELEMETRY_TARGET_TYPES!r}", name="target_type_allowed"),
        CheckConstraint(f"protocol IN {TELEMETRY_PROTOCOLS!r}", name="protocol_allowed"),
        CheckConstraint(
            "(target_type = 'network_port' AND equipment_port_id IS NOT NULL AND equipment_power_inlet_id IS NULL) OR "
            "(target_type = 'power_inlet' AND equipment_power_inlet_id IS NOT NULL AND equipment_port_id IS NULL) OR "
            "(target_type = 'environmental' AND equipment_port_id IS NULL AND equipment_power_inlet_id IS NULL)",
            name="target_reference_matches_type",
        ),
    )

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    equipment_port_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_port.id", ondelete="CASCADE"), unique=True
    )
    equipment_power_inlet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_power_inlet.id", ondelete="CASCADE"), unique=True
    )
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128))


class TelemetryLatestStatus(Base, UUIDPkMixin):
    """The one cached "current status" row per binding (`binding_id` UNIQUE) that
    `GET /telemetry/port-status/latest` reads and the rack-elevation overlay polls —
    upserted in place on every ingest, never appended to, exactly like `PowerCapacity`'s
    single current-figures row per node rather than `TelemetryReading`'s growing series.

    `payload` holds the metric fields for this binding's `target_type` (network_port:
    `bandwidth_util_pct`, `error_rate_pct`, plus the raw `link_state` the poller reported;
    power_inlet: `current_amps`, `active_power_watts`, `voltage`; environmental:
    `temperature_celsius`, `humidity_pct`) — a JSONB bag rather than one column per
    possible metric across three unrelated target types, the same reasoning
    `TelemetryReading.attributes` already applies to acquisition-specific detail that
    isn't part of the metric's own core identity."""

    __tablename__ = "telemetry_latest_status"
    __table_args__ = (CheckConstraint(f"status_level IN {ALL_STATUS_LEVELS!r}", name="status_level_allowed"),)

    binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("port_telemetry_binding.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status_level: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sampled_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
