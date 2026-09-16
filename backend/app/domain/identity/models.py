"""ManagedAsset: the identity + lifecycle anchor for every independently-lifecycled
physical asset (ARCHITECTURE_REVIEW.md §4/§4a/§4b). Phase 1 implements this table alone,
as a bare identity anchor with no concrete subtype (Rack/Equipment/PDU/...) attached yet —
those are Phase 2/3/7 (§9 of the Phase 1 prompt). asset_type is deliberately a plain,
extensible string column (not a fixed Postgres ENUM) so a future subtype can be added by
inserting a new allowed value, not by altering a type — the CHECK constraint below is the
one place that list of allowed values lives until a subtype table needs it."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin

ASSET_TYPES = ("rack", "equipment", "pdu", "ups", "generator", "power_panel", "sensor", "cable")

LIFECYCLE_STATUSES = (
    "planned",
    "installed",
    "active",
    "reserved",
    "maintenance",
    "decommissioned",
    "removed",
)

# Phase 2+ will refine this per-subtype; Phase 1 enforces only the universal shape every
# asset_type shares (§10 of the Phase 1 prompt: lifecycle transitions must be auditable
# and not invent states beyond what the architecture defines).
ALLOWED_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"installed", "removed"},
    "installed": {"active", "maintenance", "removed"},
    "active": {"maintenance", "decommissioned"},
    "reserved": {"installed", "removed"},
    "maintenance": {"active", "decommissioned"},
    "decommissioned": {"removed"},
    "removed": set(),
}


class ManagedAsset(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "managed_asset"
    __table_args__ = (
        CheckConstraint(f"asset_type IN {ASSET_TYPES!r}", name="asset_type_allowed"),
        CheckConstraint(f"lifecycle_status IN {LIFECYCLE_STATUSES!r}", name="lifecycle_status_allowed"),
        UniqueConstraint("replaces_asset_id", name="uq_managed_asset_replaces_asset_id"),
    )

    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_tag: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(128))
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    external_ids: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decommissioned_at: Mapped[datetime | None] = mapped_column()

    replaces_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("managed_asset.id", ondelete="RESTRICT"), nullable=True
    )
    replaces_asset: Mapped["ManagedAsset"] = relationship(remote_side="ManagedAsset.id")
