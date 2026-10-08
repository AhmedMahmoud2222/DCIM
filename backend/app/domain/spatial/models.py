"""FloorPlan / SpatialLayer / SpatialObject (ARCHITECTURE_REVIEW.md §8/§9/§12/§40).
Column lists authored per PHASE2_GAP_ANALYSIS.md §3.1/§3.3 — not published verbatim
anywhere in the architecture document, derived from §8's inline sketch, §9's coordinate
model, and §40's renderer-agnostic requirement (every renderer consumes the same
SpatialObject query result).

Single source of truth (§12): SpatialObject is a VISUALIZATION of authoritative
placement, never the other way around — RackPlacement/EquipmentPlacement own the
authoritative room/x/y; a SpatialObject a placement links to (via
RackPlacement.spatial_object_id / EquipmentPlacement.spatial_object_id) is the drawn
representation of that same fact, kept consistent by the room-match trigger in
migration 0004, never a second place those facts are independently recorded.

`source` distinguishes authoritative DCIM objects from imported/discovered geometry
(Phase 2 prompt §21/§24): an imported drawing's raw shapes land here as
`source='imported'` and are never auto-promoted to represent real inventory — only a
human accept action (via FloorPlanImportCandidate) creates the authoritative
`source='authoritative'` object a Rack/Equipment placement actually links to."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

FLOOR_PLAN_STATUSES = ("draft", "active", "superseded")
SOURCE_FORMATS = ("svg", "png", "jpeg", "dxf", "vsdx")
LAYER_TYPES = ("background", "room_outline", "racks", "equipment", "annotations", "imported")
OBJECT_TYPES = ("rack", "equipment", "room_outline", "annotation", "imported_shape", "wall", "column", "obstacle", "aisle")
BOUNDARY_OBJECT_TYPES = ("room_outline",)
GEOMETRY_TYPES = ("rect", "polygon", "circle", "text", "path")
OBJECT_SOURCES = ("authoritative", "imported", "discovered")


class FloorPlan(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "floor_plan"
    __table_args__ = (
        CheckConstraint(f"status IN {FLOOR_PLAN_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"source_format IS NULL OR source_format IN {SOURCE_FORMATS!r}", name="source_format_allowed"),
        UniqueConstraint("room_id", "revision_number", name="uq_floor_plan_room_revision"),
        Index("ix_floor_plan_room_status", "room_id", "status"),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_file_hash: Mapped[str | None] = mapped_column(String(64))
    source_format: Mapped[str | None] = mapped_column(String(8))

    # Coordinate model (§9): canonical storage is integer mm; calibration is the one
    # place a mm<->px factor is computed and stored, never recomputed implicitly on read.
    calibration_scale_mm_per_px: Mapped[float | None] = mapped_column(Numeric(12, 6))
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    room_width_mm: Mapped[int | None] = mapped_column(Integer)
    room_height_mm: Mapped[int | None] = mapped_column(Integer)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    # Issue #104: the one calibration currently in force. Calibrations are immutable rows; recalibrating
    # inserts a new one and moves this pointer (use_alter: floor_plan <-> floor_plan_calibration is circular).
    current_calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("floor_plan_calibration.id", ondelete="SET NULL", use_alter=True, name="fk_floor_plan_current_calibration")
    )


CALIBRATION_METHODS = ("declared_units", "two_point", "room_dimension", "manual_scale")
CALIBRATION_CONFIDENCE = ("high", "medium", "low")
CALIBRATION_UNITS = ("mm", "cm", "m", "in", "ft", "px", "unitless")


class FloorPlanCalibration(Base, UUIDPkMixin):
    """Immutable record of how source coordinates map to canonical room-local millimetres (UPDATE and DELETE
    are rejected by a trigger in migration 0044). Enough is stored to reproduce the transformation exactly:
    method, scale, origin, axis convention, rotation, the reference measurements and the error bound."""

    __tablename__ = "floor_plan_calibration"
    __table_args__ = (
        CheckConstraint(f"method IN {CALIBRATION_METHODS!r}", name="method_allowed"),
        CheckConstraint(f"confidence IN {CALIBRATION_CONFIDENCE!r}", name="confidence_allowed"),
        CheckConstraint(f"source_units IN {CALIBRATION_UNITS!r}", name="source_units_allowed"),
        CheckConstraint("mm_per_unit > 0", name="scale_positive"),
        CheckConstraint("y_axis IN ('up', 'down')", name="y_axis_allowed"),
        CheckConstraint("rotation_quadrants BETWEEN 0 AND 3", name="rotation_quadrants_range"),
        CheckConstraint("error_bound_mm IS NULL OR error_bound_mm >= 0", name="error_bound_nonnegative"),
        UniqueConstraint("floor_plan_id", "sequence", name="uq_floor_plan_calibration_sequence"),
        Index("ix_floor_plan_calibration_floor_plan", "floor_plan_id"),
    )

    floor_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("floor_plan.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("floor_plan_import_job.id", ondelete="SET NULL"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("floor_plan_calibration.id", ondelete="SET NULL"))
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    source_units: Mapped[str] = mapped_column(String(8), nullable=False)
    mm_per_unit: Mapped[float] = mapped_column(Numeric(18, 9), nullable=False)
    origin_x: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    origin_y: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    y_axis: Mapped[str] = mapped_column(String(4), nullable=False)
    rotation_quadrants: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    error_bound_mm: Mapped[float | None] = mapped_column(Numeric(14, 3))
    relative_error: Mapped[float | None] = mapped_column(Numeric(10, 6))
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    reference: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class SpatialLayer(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "spatial_layer"
    __table_args__ = (CheckConstraint(f"layer_type IN {LAYER_TYPES!r}", name="layer_type_allowed"),)

    floor_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("floor_plan.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    layer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    z_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visible_by_default: Mapped[bool] = mapped_column(nullable=False, default=True)


class SpatialObject(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "spatial_object"
    __table_args__ = (
        CheckConstraint(f"object_type IN {OBJECT_TYPES!r}", name="object_type_allowed"),
        CheckConstraint(f"geometry_type IN {GEOMETRY_TYPES!r}", name="geometry_type_allowed"),
        CheckConstraint(f"source IN {OBJECT_SOURCES!r}", name="source_allowed"),
        Index("ix_spatial_object_layer", "spatial_layer_id"),
    )

    spatial_layer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("spatial_layer.id", ondelete="CASCADE"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # Canonical mm, room-local origin (§9): X right, Y down, rotation clockwise degrees.
    x_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    y_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    height_mm: Mapped[int | None] = mapped_column(Integer)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Additional points for polygon/path geometry types, canonical mm — [[x,y], [x,y], ...]
    geometry_data: Mapped[dict | None] = mapped_column(JSONB)

    label: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="authoritative")
    # Issue #104 lineage for accepted imported geometry: job, candidate, source reference, SIR hash, calibration.
    provenance: Mapped[dict | None] = mapped_column(JSONB)

    version: Mapped[int] = mapped_column(nullable=False, default=1)
