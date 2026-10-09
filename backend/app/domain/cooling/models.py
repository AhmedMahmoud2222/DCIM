# ruff: noqa: E501
"""Cooling and environment domain (Issue #105).

Cooling units (CRAC / CRAH / chiller) and environmental sensors are ManagedAsset subtypes that use the
shared-primary-key pattern (ARCHITECTURE_REVIEW.md §4b): the subtype row's `id` IS the ManagedAsset id and
never repeats asset_tag / serial / lifecycle. A generated type column plus a composite FK to
`managed_asset (id, asset_type)` makes the database reject a subtype row whose ManagedAsset has the wrong type.

Location is not stored here. Cooling units and sensors are placed through the shared `equipment_placement`
infrastructure (close-then-open history); only the zone *geometry* that has no asset identity (served zones,
aisles, containment) lives in `thermal_zone`.

Every engineering value that can be unknown is nullable. NULL means "unknown" and is never read as zero.
All stored physical quantities use the canonical units of the metric registry (kW, m3/s, degC, %, mm).
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

COOLING_UNIT_KINDS = ("crac", "crah", "chiller")
OPERATING_STATUSES = ("online", "standby", "offline", "fault", "unknown")
# A unit contributes capacity only when its lifecycle allows it and its operating status is one of these.
AVAILABLE_OPERATING_STATUSES = ("online", "standby")
AVAILABLE_LIFECYCLE_STATUSES = ("installed", "active")

SENSOR_KINDS = ("temperature", "humidity", "airflow", "differential_pressure", "combined")
SENSOR_ROLES = ("ambient", "rack_inlet", "rack_exhaust", "supply_air", "return_air", "other")
# Canonical metrics each sensor kind may be mapped to (validated by the service layer on mapping).
SENSOR_KIND_METRICS: dict[str, tuple[str, ...]] = {
    "temperature": ("temperature_c", "supply_air_temperature_c", "return_air_temperature_c"),
    "humidity": ("humidity_percent",),
    "airflow": ("airflow_m3_s", "airflow_velocity_m_s"),
    "differential_pressure": ("differential_pressure_pa",),
    "combined": (
        "temperature_c", "humidity_percent", "supply_air_temperature_c", "return_air_temperature_c",
        "airflow_m3_s", "airflow_velocity_m_s", "differential_pressure_pa",
    ),
}

ZONE_KINDS = ("served_zone", "supply_region", "return_region", "hot_aisle", "cold_aisle")
AISLE_ZONE_KINDS = ("hot_aisle", "cold_aisle")
ZONE_GEOMETRY_TYPES = ("rect", "polygon")
CONTAINMENT_STATES = ("none", "contained")
CONTAINMENT_ELEMENT_KINDS = ("boundary", "opening")

RELATION_KINDS = ("serves", "supplies", "returns_from")
RELATION_SEMANTICS = ("authoritative", "configured", "modelled")


class CoolingGroup(Base, UUIDPkMixin, TimestampMixin):
    """A set of cooling units that back each other up (N / N+1). Membership is `cooling_unit.cooling_group_id`;
    the composite (id, site_id) key lets that FK force a unit and its group to share a site."""

    __tablename__ = "cooling_group"
    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_cooling_group_site_name"),
        UniqueConstraint("id", "site_id", name="uq_cooling_group_id_site"),
    )

    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
    retired: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class CoolingUnit(Base, TimestampMixin):
    __tablename__ = "cooling_unit"
    __table_args__ = (
        CheckConstraint(f"unit_kind IN {COOLING_UNIT_KINDS!r}", name="unit_kind_allowed"),
        CheckConstraint(f"operating_status IN {OPERATING_STATUSES!r}", name="operating_status_allowed"),
        CheckConstraint("rated_cooling_capacity_kw IS NULL OR rated_cooling_capacity_kw > 0", name="rated_capacity_positive"),
        CheckConstraint("configured_cooling_capacity_kw IS NULL OR configured_cooling_capacity_kw > 0", name="configured_capacity_positive"),
        CheckConstraint(
            "configured_cooling_capacity_kw IS NULL OR rated_cooling_capacity_kw IS NULL OR "
            "configured_cooling_capacity_kw <= rated_cooling_capacity_kw",
            name="configured_not_above_rated",
        ),
        CheckConstraint("airflow_capacity_m3_s IS NULL OR airflow_capacity_m3_s > 0", name="airflow_capacity_positive"),
        CheckConstraint("supply_air_target_c IS NULL OR supply_air_target_c BETWEEN -50 AND 100", name="supply_target_range"),
        CheckConstraint("return_air_design_c IS NULL OR return_air_design_c BETWEEN -50 AND 100", name="return_design_range"),
        CheckConstraint("humidity_min_percent IS NULL OR humidity_min_percent BETWEEN 0 AND 100", name="humidity_min_range"),
        CheckConstraint("humidity_max_percent IS NULL OR humidity_max_percent BETWEEN 0 AND 100", name="humidity_max_range"),
        CheckConstraint(
            "humidity_min_percent IS NULL OR humidity_max_percent IS NULL OR humidity_min_percent < humidity_max_percent",
            name="humidity_limits_ordered",
        ),
        CheckConstraint("supply_direction_deg IS NULL OR supply_direction_deg BETWEEN 0 AND 359", name="supply_direction_range"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_not_blank"),
        # The ManagedAsset must carry the matching asset_type (crac / crah / chiller).
        ForeignKeyConstraint(
            ["id", "unit_kind"], ["managed_asset.id", "managed_asset.asset_type"], ondelete="CASCADE", name="fk_cooling_unit_managed_asset_type"
        ),
        # A unit and its group must share a site.
        ForeignKeyConstraint(
            ["cooling_group_id", "site_id"], ["cooling_group.id", "cooling_group.site_id"], name="fk_cooling_unit_group_same_site"
        ),
        Index("ix_cooling_unit_site", "site_id"),
        Index("ix_cooling_unit_group", "cooling_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    unit_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
    # Engineering inputs: NULL = unknown. Canonical units (kW, m3/s, degC, %).
    rated_cooling_capacity_kw: Mapped[float | None] = mapped_column(Numeric(12, 3))
    configured_cooling_capacity_kw: Mapped[float | None] = mapped_column(Numeric(12, 3))
    airflow_capacity_m3_s: Mapped[float | None] = mapped_column(Numeric(12, 4))
    supply_air_target_c: Mapped[float | None] = mapped_column(Numeric(6, 2))
    return_air_design_c: Mapped[float | None] = mapped_column(Numeric(6, 2))
    humidity_min_percent: Mapped[float | None] = mapped_column(Numeric(5, 2))
    humidity_max_percent: Mapped[float | None] = mapped_column(Numeric(5, 2))
    # Compass-style bearing in the room's coordinate system (0 = +X / right, clockwise, same as rotation_deg).
    supply_direction_deg: Mapped[int | None] = mapped_column(SmallInteger)
    operating_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", server_default="unknown")
    cooling_group_id: Mapped[uuid.UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class EnvironmentalSensor(Base, TimestampMixin):
    __tablename__ = "environmental_sensor"
    __table_args__ = (
        CheckConstraint(f"sensor_kind IN {SENSOR_KINDS!r}", name="sensor_kind_allowed"),
        CheckConstraint(f"measurement_role IN {SENSOR_ROLES!r}", name="measurement_role_allowed"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("flow_direction_deg IS NULL OR (sensor_kind IN ('airflow', 'combined') AND flow_direction_deg BETWEEN 0 AND 359)", name="flow_direction_valid"),
        CheckConstraint("elevation_mm IS NULL OR elevation_mm >= 0", name="elevation_nonnegative"),
        ForeignKeyConstraint(
            ["id", "asset_type"], ["managed_asset.id", "managed_asset.asset_type"], ondelete="CASCADE", name="fk_environmental_sensor_managed_asset_type"
        ),
        Index("ix_environmental_sensor_site", "site_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(32), Computed("'sensor'", persisted=True), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sensor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    measurement_role: Mapped[str] = mapped_column(String(16), nullable=False, default="ambient", server_default="ambient")
    # Mounting height above the finished floor. Recorded for provenance; the 2D heat map treats every sensor as
    # one horizontal plane and does not model vertical stratification.
    elevation_mm: Mapped[int | None] = mapped_column(Integer)
    # Configured orientation of an airflow sensor's measurement axis (0 = +X, clockwise).
    flow_direction_deg: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(String(2000))
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class ThermalZone(Base, UUIDPkMixin, TimestampMixin):
    """A named region of one room in canonical room-local millimetres: a served zone, a supply/return region, or a
    hot/cold aisle (optionally contained). Aisle type is an operator statement, never inferred from rack
    orientation. A served zone without geometry covers the whole room."""

    __tablename__ = "thermal_zone"
    __table_args__ = (
        CheckConstraint(f"zone_kind IN {ZONE_KINDS!r}", name="zone_kind_allowed"),
        CheckConstraint(f"containment IN {CONTAINMENT_STATES!r}", name="containment_allowed"),
        CheckConstraint(f"geometry_type IS NULL OR geometry_type IN {ZONE_GEOMETRY_TYPES!r}", name="geometry_type_allowed"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("containment = 'none' OR zone_kind IN ('hot_aisle', 'cold_aisle')", name="containment_only_on_aisles"),
        CheckConstraint("geometry_type IS NOT NULL OR zone_kind = 'served_zone'", name="geometry_required_except_served_zone"),
        CheckConstraint(
            "geometry_type IS NULL OR (geometry_type = 'rect' AND x_mm IS NOT NULL AND y_mm IS NOT NULL AND width_mm > 0 AND height_mm > 0 "
            "AND points IS NULL) OR (geometry_type = 'polygon' AND points IS NOT NULL AND x_mm IS NULL AND y_mm IS NULL "
            "AND width_mm IS NULL AND height_mm IS NULL)",
            name="geometry_shape_consistent",
        ),
        UniqueConstraint("room_id", "name", name="uq_thermal_zone_room_name"),
        Index("ix_thermal_zone_room", "room_id"),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("room.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    zone_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    containment: Mapped[str] = mapped_column(String(16), nullable=False, default="none", server_default="none")
    geometry_type: Mapped[str | None] = mapped_column(String(16))
    x_mm: Mapped[int | None] = mapped_column(Integer)
    y_mm: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    height_mm: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    notes: Mapped[str | None] = mapped_column(String(2000))
    retired: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class ContainmentElement(Base, UUIDPkMixin, TimestampMixin):
    """A containment boundary wall or an opening in it, as a line segment in room-local mm."""

    __tablename__ = "containment_element"
    __table_args__ = (
        CheckConstraint(f"element_kind IN {CONTAINMENT_ELEMENT_KINDS!r}", name="element_kind_allowed"),
        CheckConstraint("x1_mm <> x2_mm OR y1_mm <> y2_mm", name="segment_not_degenerate"),
        Index("ix_containment_element_zone", "thermal_zone_id"),
    )

    thermal_zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("thermal_zone.id", ondelete="CASCADE"), nullable=False)
    element_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    x1_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    y1_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    x2_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    y2_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128))


class CoolingUnitZone(Base, UUIDPkMixin, TimestampMixin):
    """Typed, FK-backed relationship between a cooling unit and a zone. `semantics` says how the statement is known:
    authoritative (engineering record), configured (operator assertion) or modelled (derived by a later model)."""

    __tablename__ = "cooling_unit_zone"
    __table_args__ = (
        CheckConstraint(f"relation_kind IN {RELATION_KINDS!r}", name="relation_kind_allowed"),
        CheckConstraint(f"semantics IN {RELATION_SEMANTICS!r}", name="semantics_allowed"),
        UniqueConstraint("cooling_unit_id", "thermal_zone_id", "relation_kind", name="uq_cooling_unit_zone_relation"),
        Index("ix_cooling_unit_zone_zone", "thermal_zone_id"),
    )

    cooling_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cooling_unit.id", ondelete="RESTRICT"), nullable=False)
    thermal_zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("thermal_zone.id", ondelete="RESTRICT"), nullable=False)
    relation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    semantics: Mapped[str] = mapped_column(String(16), nullable=False, default="configured", server_default="configured")
