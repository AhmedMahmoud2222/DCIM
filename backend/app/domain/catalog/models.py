"""Rack/equipment model catalog (ARCHITECTURE_REVIEW.md §4b: `Rack.model_revision_id
FK→RackModelRevision NOT NULL`, `Equipment.model_revision_id FK→EquipmentModelRevision
NOT NULL`). Column lists for these two catalog tables are not published anywhere in the
architecture document (see PHASE2_GAP_ANALYSIS.md §3.2) — authored here from the
NOT-NULL-FK-to-a-*revision* requirement, which rules out collapsing manufacturer specs
onto a single mutable row: a `*Model` catalog entry (manufacturer + model name) has one
or more immutable `*ModelRevision` rows, and existing Rack/Equipment rows keep pointing
at the revision they were created against even if a later revision corrects the
published dimensions."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class RackModel(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "rack_model"
    __table_args__ = (UniqueConstraint("manufacturer", "model_name", name="uq_rack_model_manufacturer_model_name"),)

    manufacturer: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)

    revisions: Mapped[list["RackModelRevision"]] = relationship(back_populates="rack_model")


class RackModelRevision(Base, UUIDPkMixin, TimestampMixin):
    """Immutable once created — a corrected/updated spec from the manufacturer is a new
    revision row, never an edit to an existing one, so `Rack` rows that already reference
    a revision are never silently reinterpreted under different physical dimensions."""

    __tablename__ = "rack_model_revision"

    rack_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rack_model.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    height_u: Mapped[int] = mapped_column(Integer, nullable=False)
    width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_capacity_kg: Mapped[int | None] = mapped_column(Integer)

    rack_model: Mapped[RackModel] = relationship(back_populates="revisions")


class EquipmentModel(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "equipment_model"
    __table_args__ = (
        UniqueConstraint("manufacturer", "model_name", name="uq_equipment_model_manufacturer_model_name"),
    )

    manufacturer: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)

    revisions: Mapped[list["EquipmentModelRevision"]] = relationship(back_populates="equipment_model")


class EquipmentModelRevision(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "equipment_model_revision"

    equipment_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment_model.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # height_u is the number of rack U this equipment occupies when rack-mounted; still
    # populated (nullable) for non-rack-mounted equipment classes for informational/future
    # use, since a model catalog entry is shared across however an individual unit is
    # eventually placed (EquipmentPlacement.placement_type, not this table, decides that).
    height_u: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    depth_mm: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[int | None] = mapped_column(Integer)

    equipment_model: Mapped[EquipmentModel] = relationship(back_populates="revisions")
