"""Power topology + capacity domain (ARCHITECTURE_REVIEW.md §4b/§13/§13a/§14). Phase 3
implements exactly the schema those sections already specify, plus one additive node
type (`utility_intake`) they did not need to discuss: a real DC's distribution graph
starts above the generator (utility feed), and the master prompt's own examples
("utility input") name it explicitly. This is the one deliberate deviation from the
architecture's literal table — documented here and in PHASE3_IMPLEMENTATION_REPORT.md,
not silently introduced.

`PDU`/`UPS`/`Generator`/`PowerPanel` are `ManagedAsset` subtypes (shared-PK inheritance,
§4/§4b) — they never repeat `asset_tag`/`serial_number`/`lifecycle_status`, exactly like
`Rack`/`Equipment`. `PDUOutlet` is explicitly **not** a `ManagedAsset` (§4b: "an outlet has
no independent lifecycle... identified by its owning PDU's ManagedAsset.id plus its
outlet_number") — it is a `PowerNode` sub-component instead.

`PowerNode` is the one real join target every power-graph participant gets (§13): an
independently-tracked asset sets `managed_asset_id`; an owned sub-component
(`power_circuit`, `pdu_outlet`, `equipment_power_input`) sets `owning_asset_id` to its
parent's `ManagedAsset.id` instead; `utility_intake` (this phase's one addition) sets
neither, since it represents the utility company's feed, not a DC-owned asset.

`PowerConnection` edges are real FKs on both ends (§13), never a dangling typed pair.
Redundant A/B topology is two separate `PowerConnection` rows into two different
`equipment_power_input` nodes with different `feed_label`s (§13) — no schema
special-casing for redundancy.

`PowerCapacity` (§14) applies uniformly at every level (generator/UPS/panel/circuit/PDU/
outlet) — one table, not per-level bespoke columns. `measured_load_kw` is carried in the
schema exactly as the architecture specifies (a future telemetry read), but Phase 3 never
writes it — there is no telemetry yet (master prompt §35/§37), so it is always NULL
("unknown"), never fabricated as zero. "Allocated capacity" (§8/§12 of the master prompt)
is a *different*, newly-introduced concept this table does not itself store: it is the
topology-derived roll-up computed by app/application/power_capacity.py from connected
nodes' rated/configured capacity, since there is no telemetry to measure an actual load
against yet."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

PDU_PROTOCOLS = ("snmp", "rest", "none")

POWER_NODE_TYPES = (
    "utility_intake",
    "generator",
    "ups",
    "power_panel",
    "power_circuit",
    "pdu",
    "pdu_outlet",
    "equipment_power_input",
)
# node_type values that are independently-tracked ManagedAsset subtypes and therefore
# always carry managed_asset_id (never owning_asset_id).
POWER_NODE_ASSET_TYPES = ("generator", "ups", "power_panel", "pdu")
# node_type values that are owned sub-components of another asset and therefore always
# carry owning_asset_id (never managed_asset_id, never independently commissioned).
POWER_NODE_SUBCOMPONENT_TYPES = ("power_circuit", "pdu_outlet", "equipment_power_input")

CONNECTION_TYPES = ("feed", "distribution")
FEED_LABELS = ("A", "B", "single")
PHASES = ("single", "three")
CONNECTION_STATUSES = ("active", "maintenance", "fault")

OUTLET_STATES = ("on", "off", "unknown")

REDUNDANCY_FACTORS = ("N", "N+1", "2N", "2N+1")


class PDU(Base, TimestampMixin):
    """§4b: `PK id FK->ManagedAsset`. `pdu_model_id` (a vendor catalog FK) from the
    architecture's literal column list is deliberately omitted — the master prompt
    explicitly directs "do not create unnecessary vendor-specific power models," and
    nothing else in this phase depends on a PDU catalog existing. `protocol`/`ip_address`
    are retained since they describe *this* PDU's own management interface, not a shared
    vendor model."""

    __tablename__ = "pdu"
    __table_args__ = (CheckConstraint(f"protocol IN {PDU_PROTOCOLS!r}", name="protocol_allowed"),)

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    input_voltage: Mapped[float | None] = mapped_column(Numeric(8, 2))
    rated_current_a: Mapped[float | None] = mapped_column(Numeric(8, 2))
    outlet_count: Mapped[int | None] = mapped_column()
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class UPS(Base, TimestampMixin):
    """§4b: plain FK to Room, not temporal `EquipmentPlacement` — a UPS is a large fixed
    installation relocated only as a major project."""

    __tablename__ = "ups"

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    capacity_kva: Mapped[float | None] = mapped_column(Numeric(10, 2))
    runtime_minutes: Mapped[int | None] = mapped_column()
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class Generator(Base, TimestampMixin):
    """§4b: plain FK to Site — yard/roof fixed installation."""

    __tablename__ = "generator"

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), nullable=False)
    capacity_kw: Mapped[float | None] = mapped_column(Numeric(10, 2))
    fuel_type: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class PowerPanel(Base, TimestampMixin):
    """§4b: plain FK to Room. Serves the master prompt's "RPP"/"panel" examples — one
    generic table, no separate RPP subtype (per §4.1's "do not create unnecessary
    vendor-specific power models... generic extensible domain model")."""

    __tablename__ = "power_panel"

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    capacity_kw: Mapped[float | None] = mapped_column(Numeric(10, 2))
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class PowerNode(Base, UUIDPkMixin, TimestampMixin):
    """The real join target every power-graph participant gets (§13). See module
    docstring for the managed_asset_id / owning_asset_id / neither trichotomy."""

    __tablename__ = "power_node"
    __table_args__ = (
        CheckConstraint(f"node_type IN {POWER_NODE_TYPES!r}", name="node_type_allowed"),
        CheckConstraint(
            "(managed_asset_id IS NOT NULL OR owning_asset_id IS NOT NULL OR node_type = 'utility_intake')"
            " AND NOT (managed_asset_id IS NOT NULL AND owning_asset_id IS NOT NULL)",
            name="asset_reference_exclusive",
        ),
    )

    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_asset.id", ondelete="CASCADE"), unique=True
    )
    owning_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class PDUOutlet(Base, TimestampMixin):
    """§4b: NOT a ManagedAsset. `PK id FK->power_node UNIQUE` — the outlet's identity
    lives entirely in its PowerNode row (node_type='pdu_outlet', owning_asset_id=the
    PDU's ManagedAsset.id); this table holds only the outlet-specific columns.

    F-M3 correction (migration 0007; PHASE3_CORRECTION_DESIGN.md Part 11):
    `pdu_asset_id` is no longer FK'd to `managed_asset.id` alone (satisfiable by any
    ManagedAsset subtype) — `pdu_asset_expected_type` is a generated column fixed to the
    literal `'pdu'`, and the composite FK `(pdu_asset_id, pdu_asset_expected_type) ->
    managed_asset (id, asset_type)` can only be satisfied when the referenced asset's
    own `asset_type` is actually `'pdu'`. The application never writes to
    `pdu_asset_expected_type` — PostgreSQL computes and stores it."""

    __tablename__ = "pdu_outlet"
    __table_args__ = (
        UniqueConstraint("pdu_asset_id", "outlet_number", name="uq_pdu_outlet_pdu_asset_id_outlet_number"),
        CheckConstraint(f"state IN {OUTLET_STATES!r}", name="state_allowed"),
        ForeignKeyConstraint(
            ["pdu_asset_id", "pdu_asset_expected_type"],
            ["managed_asset.id", "managed_asset.asset_type"],
            name="fk_pdu_outlet_pdu_asset_id_managed_asset",
            ondelete="CASCADE",
        ),
    )

    power_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("power_node.id", ondelete="CASCADE"), primary_key=True
    )
    pdu_asset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    pdu_asset_expected_type: Mapped[str] = mapped_column(
        String, Computed("'pdu'", persisted=True), nullable=False
    )
    outlet_number: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")


class PowerConnection(Base, UUIDPkMixin):
    """§13/§13a. A single-row edit target (unlike placement) — `version`/`If-Match`
    optimistic concurrency, not the close-then-open pattern. Self-loop prevention is a
    direct CHECK constraint; duplicate-active-edge prevention is a partial unique index;
    cycle prevention cannot be a single-row constraint and is enforced by
    app/application/power_graph.py's transactional service instead (see that module's
    own docstring for exactly how)."""

    __tablename__ = "power_connection"
    __table_args__ = (
        CheckConstraint("source_node_id <> target_node_id", name="no_self_loop"),
        CheckConstraint(f"connection_type IN {CONNECTION_TYPES!r}", name="connection_type_allowed"),
        CheckConstraint(f"feed_label IN {FEED_LABELS!r}", name="feed_label_allowed"),
        CheckConstraint(f"phase IS NULL OR phase IN {PHASES!r}", name="phase_allowed"),
        CheckConstraint(f"status IN {CONNECTION_STATUSES!r}", name="status_allowed"),
        CheckConstraint("voltage IS NULL OR voltage > 0", name="voltage_positive"),
        CheckConstraint("rated_current_a IS NULL OR rated_current_a > 0", name="rated_current_a_positive"),
        Index("ix_power_connection_source_effective_to", "source_node_id", "effective_to"),
        Index("ix_power_connection_target_effective_to", "target_node_id", "effective_to"),
    )

    source_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("power_node.id", ondelete="RESTRICT"), nullable=False)
    target_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("power_node.id", ondelete="RESTRICT"), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(16), nullable=False, default="feed")
    feed_label: Mapped[str] = mapped_column(String(8), nullable=False, default="single")
    phase: Mapped[str | None] = mapped_column(String(8))
    voltage: Mapped[float | None] = mapped_column(Numeric(8, 2))
    rated_current_a: Mapped[float | None] = mapped_column(Numeric(8, 2))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class PowerCapacity(Base, UUIDPkMixin):
    """§14. `measured_load_kw` is always NULL in Phase 3 (no telemetry yet, §35/§37 of
    the master prompt) — never fabricated as zero. `version` (an addition beyond the
    architecture's literal §14 column list, needed because the master prompt explicitly
    names "capacity configuration changes" as a contention surface, §25) protects
    concurrent threshold/configured-capacity edits the same way PowerConnection's does."""

    __tablename__ = "power_capacity"
    __table_args__ = (
        CheckConstraint("rated_capacity_kw IS NULL OR rated_capacity_kw >= 0", name="rated_capacity_kw_non_negative"),
        CheckConstraint(
            "configured_capacity_kw IS NULL OR configured_capacity_kw >= 0", name="configured_capacity_kw_non_negative"
        ),
        CheckConstraint("measured_load_kw IS NULL OR measured_load_kw >= 0", name="measured_load_kw_non_negative"),
        CheckConstraint(
            "warning_threshold_pct IS NULL OR (warning_threshold_pct >= 0 AND warning_threshold_pct <= 100)",
            name="warning_threshold_pct_range",
        ),
        CheckConstraint(
            "critical_threshold_pct IS NULL OR (critical_threshold_pct >= 0 AND critical_threshold_pct <= 100)",
            name="critical_threshold_pct_range",
        ),
        CheckConstraint(
            "warning_threshold_pct IS NULL OR critical_threshold_pct IS NULL"
            " OR warning_threshold_pct <= critical_threshold_pct",
            name="warning_le_critical",
        ),
        CheckConstraint(
            f"redundancy_factor IS NULL OR redundancy_factor IN {REDUNDANCY_FACTORS!r}", name="redundancy_factor_allowed"
        ),
        Index("ix_power_capacity_power_node_effective_to", "power_node_id", "effective_to"),
    )

    power_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("power_node.id", ondelete="CASCADE"), nullable=False)
    rated_capacity_kw: Mapped[float | None] = mapped_column(Numeric(10, 3))
    configured_capacity_kw: Mapped[float | None] = mapped_column(Numeric(10, 3))
    measured_load_kw: Mapped[float | None] = mapped_column(Numeric(10, 3))
    warning_threshold_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    critical_threshold_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    redundancy_factor: Mapped[str | None] = mapped_column(String(8))
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
