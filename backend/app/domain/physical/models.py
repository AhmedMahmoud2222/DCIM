"""Rack and Equipment: the first concrete `ManagedAsset` subtypes (ARCHITECTURE_REVIEW.md
§4b). Shared-primary-key inheritance — `Rack.id`/`Equipment.id` *are* `ManagedAsset.id`,
not a second independent identifier (§4). Per §4b's rule, stated once and followed
exactly here: "`ManagedAsset` = identity + lifecycle anchor. A subtype table =
domain-specific state only — it never repeats `asset_tag`, `serial_number`,
`lifecycle_status`, or `external_ids`." Placement (location) lives entirely in
`RackPlacement`/`EquipmentPlacement` (app/domain/placement/models.py), never here."""

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Rack(Base, TimestampMixin):
    __tablename__ = "rack"

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rack_model_revision.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(String(2000))
    custom_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Optimistic concurrency for the rack's own narrow fields (name/owner/notes) —
    # ARCHITECTURE_REVIEW.md §36: placement/topology contention is protected by
    # RackPlacement's locked close-then-open transaction (§7c), not this column; this
    # column exists for ordinary field edits racing each other.
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    managed_asset = relationship("ManagedAsset", foreign_keys=[id])
    model_revision = relationship("RackModelRevision")


class Equipment(Base, TimestampMixin):
    __tablename__ = "equipment"

    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True)
    model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment_model_revision.id", ondelete="RESTRICT"), nullable=False
    )
    hostname: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(INET)
    mac_address: Mapped[str | None] = mapped_column(String(17))
    owner: Mapped[str | None] = mapped_column(String(128))
    service: Mapped[str | None] = mapped_column(String(128))
    environment: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(String(2000))
    custom_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    managed_asset = relationship("ManagedAsset", foreign_keys=[id])
    model_revision = relationship("EquipmentModelRevision")
