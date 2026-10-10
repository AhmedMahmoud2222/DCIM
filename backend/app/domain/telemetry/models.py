"""Thin MVP metric mappings and authoritative telemetry readings.

Discovery remains an acquisition observation; telemetry is stored separately and never
mutates discovered or authoritative inventory state.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    event,
    insert,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.domain.telemetry.contract import conversion_contract_hash
from app.domain.telemetry.registry import METRIC_REGISTRY, REGISTRY_VERSION

CANONICAL_METRICS = tuple(METRIC_REGISTRY)

REVISION_PROVENANCE = ("authored", "backfilled_from_current")
# NULL on both columns = a reading stored before revision pinning (unverified legacy interpretation).
CONTRACT_EVIDENCE = ("pinned", "inferred_single_revision", "operator_resolved")
HOLD_STATUSES = ("held", "expired", "resolved")


def telemetry_series_key(
    integration_id: uuid.UUID, managed_asset_id: uuid.UUID | None, external_identifier: str, metric: str, unit: str,
    registry_version: str | None = None,
) -> str:
    """Return the immutable identity used by raw and downsampled telemetry.

    An external identifier alone is not stable enough: the same integration can
    legitimately report different assets or units with the same label during a
    replacement.  The asset (when present), source identifier, canonical metric
    and unit therefore all participate in the durable series identity.
    """
    asset_component = str(managed_asset_id) if managed_asset_id is not None else "unmanaged"
    parts = (str(integration_id), asset_component, external_identifier, metric, unit)
    # Legacy unit text was unrestricted and can itself end in ":1". Appending
    # a version would collide with those keys. A prefix cannot collide with the
    # UUID-first legacy namespace; existing historical keys remain unchanged.
    return ":".join(("registry", registry_version, *parts)) if registry_version is not None else ":".join(parts)


class IntegrationMetricMapping(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "integration_metric_mapping"
    __table_args__ = (
        UniqueConstraint("integration_id", "source_identifier", name="uq_metric_mapping_integration_source"),
        CheckConstraint(f"canonical_metric IN {CANONICAL_METRICS!r}", name="canonical_metric_allowed"),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The mapping is the narrow, authoritative bridge from an acquired source
    # identifier to inventory.  It deliberately points at ManagedAsset rather
    # than introducing a second monitoring asset identity.
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_asset.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    scale: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=1)
    registry_version: Mapped[str | None] = mapped_column(String(16), nullable=True, default=REGISTRY_VERSION)
    label: Mapped[str | None] = mapped_column(String(128))
    # The mapping row is the *current view*; every contract it has ever had lives in the append-only
    # revision table. The pointer is maintained by the revision service, never edited in place.
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integration_metric_mapping_revision.id", name="fk_metric_mapping_current_revision", use_alter=True),
        nullable=True,
    )


class IntegrationMetricMappingRevision(Base, UUIDPkMixin):
    """One immutable source-to-canonical conversion contract (Issue #128 / G1).

    Append-only: a database trigger rejects every UPDATE and any DELETE except the cascade that removes the
    revisions of a mapping that is itself being deleted. `effective_from` is the Central activation time, never
    retroactive. `provenance = 'backfilled_from_current'` marks a revision seeded from the mapping row as it
    stood when revisions were introduced: it is NOT evidence of the contract in force at any earlier event time.
    `conversion_hash` is documented in app/domain/telemetry/contract.py.
    """

    __tablename__ = "integration_metric_mapping_revision"
    __table_args__ = (
        UniqueConstraint("mapping_id", "revision", name="uq_mapping_revision_mapping_revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(f"provenance IN {REVISION_PROVENANCE!r}", name="provenance_allowed"),
        CheckConstraint("char_length(conversion_hash) = 64", name="conversion_hash_length"),
    )

    mapping_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_metric_mapping.id", ondelete="CASCADE", name="fk_mapping_revision_mapping"),
        nullable=False, index=True
    )
    # Immutable copies: the revision must be verifiable without trusting the mutable mapping row.
    integration_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    source_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    source_scale: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    registry_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    conversion_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="authored")
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


def revision_values(mapping: "IntegrationMetricMapping", *, revision: int, provenance: str, effective_from: datetime) -> dict:
    scale = mapping.scale if mapping.scale is not None else 1
    # An explicit SQL NULL expression (sqlalchemy.null()) marks a pre-registry mapping; it is not a version string.
    registry_version = mapping.registry_version if isinstance(mapping.registry_version, str) else None
    return {
        "id": uuid.uuid4(),
        "mapping_id": mapping.id,
        "integration_id": mapping.integration_id,
        "source_identifier": mapping.source_identifier,
        "revision": revision,
        "canonical_metric": mapping.canonical_metric,
        "source_unit": mapping.unit,
        "source_scale": scale,
        "registry_version": registry_version,
        "conversion_hash": conversion_contract_hash(
            mapping.canonical_metric, mapping.unit, Decimal(str(scale)), registry_version
        ),
        "provenance": provenance,
        "effective_from": effective_from,
        "created_by": None,
        "created_at": effective_from,
    }


class TelemetryReading(Base, UUIDPkMixin):
    __tablename__ = "telemetry_reading"
    __table_args__ = (
        UniqueConstraint("collector_id", "dedup_key", name="uq_telemetry_reading_collector_dedup"),
        Index("ix_telemetry_reading_integration_metric_occurred", "integration_id", "metric", "occurred_at"),
        Index("ix_telemetry_reading_asset_metric_occurred", "managed_asset_id", "metric", "occurred_at"),
        CheckConstraint(
            f"contract_evidence IS NULL OR contract_evidence IN {CONTRACT_EVIDENCE!r}", name="contract_evidence_allowed"
        ),
        CheckConstraint(
            "(mapping_revision_id IS NULL) = (contract_evidence IS NULL)", name="revision_matches_evidence"
        ),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="RESTRICT"), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration_metric_mapping.id", ondelete="RESTRICT"), nullable=False
    )
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"), index=True)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    series_key: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    raw_value: Mapped[float | None] = mapped_column(Numeric(18, 8))
    raw_unit: Mapped[str | None] = mapped_column(String(32))
    source_scale: Mapped[float | None] = mapped_column(Numeric(18, 8))
    registry_version: Mapped[str | None] = mapped_column(String(16), nullable=True, default=REGISTRY_VERSION)
    # Issue #128 / G1: the immutable contract this reading was interpreted under. NULL on both = stored before
    # revision pinning; nothing is claimed about its event-time contract.
    mapping_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integration_metric_mapping_revision.id", ondelete="RESTRICT", name="fk_telemetry_reading_mapping_revision"),
        nullable=True
    )
    contract_evidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DailyTelemetryAggregate(Base, UUIDPkMixin):
    """Long-term, loss-aware daily representation of sensor telemetry only.

    Alarm history deliberately has no relationship to this table or the retention job.
    """

    __tablename__ = "daily_telemetry_aggregate"
    __table_args__ = (
        UniqueConstraint("series_key", "day", name="uq_daily_telemetry_series_day"),
        Index("ix_daily_telemetry_series_day", "series_key", "day"),
        Index("ix_daily_telemetry_integration_metric_day", "integration_id", "metric", "day"),
        Index("ix_daily_telemetry_asset_metric_day", "managed_asset_id", "metric", "day"),
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    managed_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="SET NULL"), nullable=True)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    series_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[date] = mapped_column(nullable=False)
    average_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    minimum_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    maximum_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    sample_count: Mapped[int] = mapped_column(nullable=False)
    registry_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class MonitoringPolicy(Base):
    """Singleton persisted policy; cleanup is always an explicit controlled job."""
    __tablename__ = "monitoring_policy"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    default_poll_interval_seconds: Mapped[int] = mapped_column(nullable=False, default=300)
    raw_retention_days: Mapped[int] = mapped_column(nullable=False, default=365)
    daily_aggregate_retention_days: Mapped[int | None] = mapped_column(nullable=True)
    alarm_history_retention_days: Mapped[int | None] = mapped_column(nullable=True)


class TelemetryContractHold(Base, UUIDPkMixin):
    """A telemetry record Central could not interpret unambiguously and refused to reinterpret (Issue #128 / G1).

    The full record is retained so that bounded retry expiry never loses data: an operator can still release it
    under an explicitly chosen revision. `attempts`/`first_held_at` bound the collector retry loop.
    """

    __tablename__ = "telemetry_contract_hold"
    __table_args__ = (
        UniqueConstraint("collector_id", "dedup_key", name="uq_telemetry_contract_hold_collector_dedup"),
        CheckConstraint(f"status IN {HOLD_STATUSES!r}", name="status_allowed"),
        Index("ix_telemetry_contract_hold_status_last", "status", "last_held_at"),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integration.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    value_text: Mapped[str] = mapped_column(String(64), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="held")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_held_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_held_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    resolved_revision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    resolved_reading_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


@event.listens_for(IntegrationMetricMapping, "after_insert")
def _seed_first_revision(_mapper, connection, mapping: IntegrationMetricMapping) -> None:
    """Every mapping created through the ORM starts with an authored revision 1 in the same transaction.

    Direct SQL inserts bypass this (the Alembic seed covers rows that existed at upgrade); ingest treats a mapping
    without a revision as ambiguous, never as an implicitly trusted contract.
    """
    values = revision_values(mapping, revision=1, provenance="authored", effective_from=datetime.now(UTC))
    revision_table = cast(Table, IntegrationMetricMappingRevision.__table__)
    mapping_table = cast(Table, IntegrationMetricMapping.__table__)
    connection.execute(insert(revision_table).values(**values))
    connection.execute(
        update(mapping_table).where(mapping_table.c.id == mapping.id).values(current_revision_id=values["id"])
    )
    set_committed_value(mapping, "current_revision_id", values["id"])
