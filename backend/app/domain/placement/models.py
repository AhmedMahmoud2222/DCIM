"""RackPlacement / EquipmentPlacement (ARCHITECTURE_REVIEW.md §7/§7a/§7b/§7c/§8): the
authoritative location of every rack and every piece of equipment. Neither `Rack` nor
`Equipment` carries a `room_id`/`rack_id`/`u_position` column — placement is owned
entirely here, temporally (§29): "current" = `effective_to IS NULL`; moving something
closes the current row and opens a new one in the same transaction (§7c).

The GiST exclusion constraints this file's docstrings describe (§7a's front/rear
overlap prevention, §7b's one-timeline-per-asset guarantee) are declared in migration
0004, as raw DDL, not here — SQLAlchemy's declarative `ExcludeConstraint` mapping for
constraints built from computed SQL expressions (`tstzrange(effective_from,
effective_to)`) is fragile enough that Phase 1 already established the precedent
(`trg_managed_asset_replacement_acyclic`) of keeping this class of constraint as
explicit, reviewable SQL in the migration rather than fighting the ORM's declarative
mapping for it. The `occupies_front`/`occupies_rear` generated columns below ARE
declared here (a straightforward `Computed(...)` case) since the exclusion constraints
in the migration reference them by name."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import INT4RANGE, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPkMixin

PLACEMENT_TYPES = ("rack_mounted", "floor_standing", "wall_mounted", "ceiling_mounted", "other")
SIDES = ("front", "rear", "both")


class RackPlacement(Base, UUIDPkMixin):
    """A rack's current/historical room + drawn (x/y/rotation) position. §8: 'Rack' has
    no room_id column — this table is the only place a rack's room lives."""

    __tablename__ = "rack_placement"
    __table_args__ = (Index("ix_rack_placement_rack_effective_to", "rack_id", "effective_to"),)

    rack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="RESTRICT"), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    spatial_object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spatial_object.id", ondelete="SET NULL"), unique=True
    )
    x_mm: Mapped[int | None] = mapped_column(Integer)
    y_mm: Mapped[int | None] = mapped_column(Integer)
    rotation_deg: Mapped[int | None] = mapped_column(Integer)

    version: Mapped[int] = mapped_column(nullable=False, default=1)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class EquipmentPlacement(Base, UUIDPkMixin):
    """Column name `equipment_id` retained per §4c's naming note even though this scopes
    to any placeable ManagedAsset (Equipment, PDU, Sensor — Phase 2 only creates
    Equipment rows, but the column is shared infrastructure future subtypes reuse without
    a schema change)."""

    __tablename__ = "equipment_placement"
    __table_args__ = (
        CheckConstraint(f"placement_type IN {PLACEMENT_TYPES!r}", name="placement_type_allowed"),
        CheckConstraint(f"side IS NULL OR side IN {SIDES!r}", name="side_allowed"),
        # v1.3 correction (finding F1, closes the NULL-bypass that would otherwise let a
        # rack-mounted row escape both partial exclusion constraints in the migration —
        # see PHASE2_GAP_ANALYSIS.md §6). Carried forward exactly, not reintroduced.
        CheckConstraint(
            "placement_type != 'rack_mounted' OR (rack_id IS NOT NULL AND u_range IS NOT NULL AND side IS NOT NULL)",
            name="rack_mounted_requires_rack_u_range_and_side",
        ),
        UniqueConstraint("spatial_object_id", name="uq_equipment_placement_spatial_object_id"),
        Index("ix_equipment_placement_equipment_effective_to", "equipment_id", "effective_to"),
        Index("ix_equipment_placement_rack_effective_to", "rack_id", "effective_to"),
    )

    equipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("managed_asset.id", ondelete="RESTRICT"), nullable=False)
    placement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    rack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("managed_asset.id", ondelete="RESTRICT"))
    u_range: Mapped[str | None] = mapped_column(INT4RANGE)
    side: Mapped[str | None] = mapped_column(String(8))

    # Generated, never independently written — §7a: "side remains the single,
    # only-writable source of truth and the booleans are derived, indexable projections
    # of it, never a second independent representation." COALESCE to false (not NULL)
    # for non-rack-mounted rows, where `side` is legitimately NULL (only rack_mounted
    # rows require it, per the CHECK constraint below) — a floor/wall/ceiling-mounted
    # item occupies neither the front nor the rear of any rack.
    occupies_front: Mapped[bool] = mapped_column(
        Computed("COALESCE(side IN ('front', 'both'), false)", persisted=True), nullable=False
    )
    occupies_rear: Mapped[bool] = mapped_column(
        Computed("COALESCE(side IN ('rear', 'both'), false)", persisted=True), nullable=False
    )

    spatial_object_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("spatial_object.id", ondelete="SET NULL"))
    rotation_deg: Mapped[int | None] = mapped_column(Integer)
    mounting_method: Mapped[str | None] = mapped_column(String(64))
    orientation: Mapped[str | None] = mapped_column(String(32))

    version: Mapped[int] = mapped_column(nullable=False, default=1)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
