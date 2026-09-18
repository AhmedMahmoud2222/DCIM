"""Phase 8: Integrations + Collectors (ARCHITECTURE_REVIEW.md §18/§19/§20; master prompt
"Phase 8 — Integrations + Edge Collectors"). Establishes the acquisition/transport
foundation the future Telemetry domain (architecture Phase 9) will consume — this module
owns identity, assignment, health, and the discovery/reconciliation boundary; it does
NOT own telemetry storage, time-series data, or alarms (those remain Phase 9/10-owned,
per the master prompt's explicit "do not build telemetry yet").

**Central DCIM owns**: `Integration` configuration, `Collector` identity, collector
assignment history, reconciliation decisions, and everything below in this file.
**Edge Collector owns** (a separate, not-yet-built deployable — see
`PHASE8_EDGE_COLLECTOR_CONTRACT.md`): local device acquisition, local protocol execution,
temporary durable buffering, secure forwarding. **Edge does not own**: authoritative
inventory, global asset identity, or the final reconciliation decision — a
`DiscoveredDevice` row is never itself authoritative; only a human-approved
`ReconciliationDiff` can link one to a real `ManagedAsset`.

Every table here follows established codebase conventions rather than inventing new
ones: `UUIDPkMixin`/`TimestampMixin` (all domain tables), `version` for optimistic
concurrency on single-row edit targets (§13a's own precedent, e.g. `PowerCapacity`), and
`effective_from`/`effective_to` temporal assignment (`CollectorAssignment` mirrors
`RackPlacement`/`EquipmentPlacement`/`PowerCapacity`'s own "exactly one current row,
full history preserved" pattern) rather than a mutable `Integration.collector_id`
column, which would be a second representation of the same fact this table already
owns (§12 Single Source of Truth)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

COLLECTOR_TYPES = ("central", "edge")
COLLECTOR_STATUSES = ("registered", "active", "disabled")
# Health is *computed* from heartbeat recency (never stored as a fact — see
# app/application/collector_service.py's classify_collector_health), so it is not a
# column here; these are the values that computation can return.
COLLECTOR_HEALTH_STATES = ("healthy", "stale", "offline")

INTEGRATION_TYPES = ("icmp", "snmp", "rest")

DISCOVERED_DEVICE_STATUSES = ("new", "reconciled", "ignored")

RECONCILIATION_DIFF_TYPES = ("new_device", "attribute_mismatch", "missing_in_discovery")
RECONCILIATION_DIFF_STATUSES = ("pending", "accepted", "rejected")


class Collector(Base, UUIDPkMixin, TimestampMixin):
    """§18: `Collector` — PK id, name, site_id FK->Site NULL (NULL = central),
    collector_type ENUM(central/edge). "For Phase 1-8, exactly one Collector row exists
    (collector_type=central)... which is what makes edge collectors (§19) additive
    rather than a redesign" — this phase implements the general model, not just the
    one central row; edge collectors register through the same table and code path.

    `secret_ciphertext` is the collector's own machine-to-machine trust credential (§9
    of the master prompt: "Do not use the normal end-user JWT mechanism as the
    collector trust mechanism"). Deliberately **not** Argon2-hashed like
    `User.password_hash`: every collector request is HMAC-signed
    (`app/application/collector_auth.py`), and verifying an HMAC signature requires the
    server to recompute it, which requires the plaintext secret — a one-way hash cannot
    serve that purpose. Reuses the same reversible Fernet primitive as `Integration.
    credential_ciphertext` (`app/core/secrets.py`) instead, so the raw secret is never
    transmitted per-request (only a derived, per-request signature is), never stored in
    plaintext, and is decrypted only in-memory for the instant a signature is verified.
    Verified via `app/application/collector_auth.py`, never via the user JWT/RBAC path.
    Rotation is single-active-secret (no overlap window) — see `PHASE8_EDGE_COLLECTOR_
    CONTRACT.md`'s open-decisions section for the documented grace-period rotation this
    defers."""

    __tablename__ = "collector"
    __table_args__ = (
        CheckConstraint(f"collector_type IN {COLLECTOR_TYPES!r}", name="collector_type_allowed"),
        CheckConstraint(f"status IN {COLLECTOR_STATUSES!r}", name="status_allowed"),
        CheckConstraint(
            "(collector_type = 'central' AND site_id IS NULL) OR (collector_type = 'edge' AND site_id IS NOT NULL)",
            name="edge_requires_site_central_forbids_site",
        ),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    collector_type: Mapped[str] = mapped_column(String(16), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    version_string: Mapped[str | None] = mapped_column(String(64))
    secret_ciphertext: Mapped[str] = mapped_column(String(1000), nullable=False)
    secret_rotated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CollectorRequestNonce(Base, UUIDPkMixin):
    """§9 of the master prompt ("replay protection"): every signed collector request
    (app/application/collector_auth.py) must present a nonce never seen before from
    that collector, checked via this table's unique constraint as an atomic claim
    (the same INSERT-as-claim pattern `IdempotencyKey` already established) -- a
    captured, valid-signature request cannot be replayed to trigger processing twice.

    OPEN DECISION / deferred (PHASE8_EDGE_COLLECTOR_CONTRACT.md): rows older than the
    signature timestamp window are safe to prune (a replay of an expired-timestamp
    request is already rejected on that basis alone), but no periodic pruning job is
    wired up in this phase -- this table grows unbounded until one is added. Documented,
    not silently ignored."""

    __tablename__ = "collector_request_nonce"
    __table_args__ = (UniqueConstraint("collector_id", "nonce", name="uq_collector_request_nonce_collector_nonce"),)

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CollectorCapability(Base, UUIDPkMixin):
    """§18: declarative capability declaration ("which drivers this collector instance
    can run") — never hardcoded per-integration; an integration's protocol requirement
    is matched against a collector's declared capabilities at assignment time
    (app/application/collector_service.py), not assumed."""

    __tablename__ = "collector_capability"
    __table_args__ = (UniqueConstraint("collector_id", "protocol_code", name="uq_collector_capability_collector_protocol"),)

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), nullable=False, index=True)
    protocol_code: Mapped[str] = mapped_column(String(32), nullable=False)


class CollectorHeartbeat(Base, UUIDPkMixin):
    """§18/§37: "how central knows an edge collector is alive versus silently buffering
    versus genuinely down." Append-only (never updated) — health is derived from the
    most recent row's `ts`, never inferred from device/integration health (master
    prompt §13's explicit "never collapse these into one status")."""

    __tablename__ = "collector_heartbeat"
    __table_args__ = (Index("ix_collector_heartbeat_collector_ts", "collector_id", "ts"),)

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    queue_depth: Mapped[int | None] = mapped_column()
    cpu_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    mem_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")


class Integration(Base, UUIDPkMixin, TimestampMixin):
    """§20: one configured acquisition target. `credential_ciphertext` is Fernet-
    encrypted (app/core/secrets.py) — reversible, unlike the collector's own Argon2
    secret, because a driver must retrieve the plaintext to actually authenticate to the
    device being polled; the encryption key comes from settings, documented as an OPEN
    DECISION for production KMS migration (PHASE8_EDGE_COLLECTOR_CONTRACT.md) rather than
    invented here. Never `site_id`-less by accident: nullable because a central-only
    deployment may poll a device without a formal Site record yet, but every real
    deployment is expected to set it (enforced at the application layer, not the schema,
    to avoid blocking a valid Phase 1-era bootstrap)."""

    __tablename__ = "integration"
    __table_args__ = (CheckConstraint(f"integration_type IN {INTEGRATION_TYPES!r}", name="integration_type_allowed"),)

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    integration_type: Mapped[str] = mapped_column(String(16), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    target_host: Mapped[str] = mapped_column(String(255), nullable=False)
    target_port: Mapped[int | None] = mapped_column()
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    credential_ciphertext: Mapped[str | None] = mapped_column(String(4000))
    poll_interval_seconds: Mapped[int] = mapped_column(nullable=False, default=60)

    # Observability (master prompt §13): last poll/success/failure, kept here (not
    # derived from a telemetry table this phase does not own) since these describe the
    # *acquisition attempt*, not the *reading* — distinct from Phase 9's own concerns.
    last_poll_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(nullable=False, default=0)

    version: Mapped[int] = mapped_column(nullable=False, default=1)


class CollectorAssignment(Base, UUIDPkMixin):
    """§18: "which collector is responsible for acquiring this device" /
    "which integrations are currently assigned to this collector" — an explicit,
    temporal join (mirrors `RackPlacement`/`EquipmentPlacement`/`PowerCapacity`'s own
    established "exactly one current row, full history preserved via effective_to"
    pattern), not a mutable `Integration.collector_id` column. Reassignment = close the
    current row (`effective_to`) and open a new one in the same transaction, exactly
    like `RackPlacement`'s own move operation — never an in-place UPDATE of `collector_id`
    on an existing open row, which would destroy the assignment history the architecture
    explicitly calls for ("avoid ambiguous ownership")."""

    __tablename__ = "collector_assignment"
    __table_args__ = (
        Index(
            "uq_collector_assignment_current_per_integration",
            "integration_id",
            unique=True,
            postgresql_where="effective_to IS NULL",
        ),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="RESTRICT"), nullable=False, index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class DiscoveredDevice(Base, UUIDPkMixin):
    """§7 of the master prompt ("Device Model / Discovery Boundary"): the acquisition-
    side identity of something a driver's `normalize()` step observed — NEVER
    authoritative inventory. `matched_managed_asset_id` is set ONLY by a human-approved
    `ReconciliationDiff` (app/application/discovery_service.py's `accept_reconciliation`),
    never by the discovery/ingestion path itself. No unattended process may write to
    `managed_asset`/`rack`/`equipment`/`pdu`/`ups`/`generator`/`power_panel` from here."""

    __tablename__ = "discovered_device"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_identifier", name="uq_discovered_device_integration_external_id"),
        CheckConstraint(f"status IN {DISCOVERED_DEVICE_STATUSES!r}", name="status_allowed"),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    raw_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    matched_managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"))


class ReconciliationDiff(Base, UUIDPkMixin):
    """§7: the one artifact a human actually acts on. `accepted` is the only status
    that may ever cause `DiscoveredDevice.matched_managed_asset_id` to be set — enforced
    in the application layer (app/application/discovery_service.py), never automatically."""

    __tablename__ = "reconciliation_diff"
    __table_args__ = (
        CheckConstraint(f"diff_type IN {RECONCILIATION_DIFF_TYPES!r}", name="diff_type_allowed"),
        CheckConstraint(f"status IN {RECONCILIATION_DIFF_STATUSES!r}", name="status_allowed"),
    )

    discovered_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovered_device.id", ondelete="CASCADE"), nullable=False, index=True
    )
    diff_type: Mapped[str] = mapped_column(String(32), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(128))
    discovered_value: Mapped[str | None] = mapped_column(String(2000))
    authoritative_value: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(1000))
