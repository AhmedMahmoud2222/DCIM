"""Phase 10A Asset Catalog Designer aggregate (docs/superpowers/specs/
2026-09-23-phase-10a-asset-catalog-designer-design.md §4, §6.1). PR-1 scope only: schema
and DB guards — no application code reads or writes these tables yet (that is PR-3
onward). Kept in a sibling module to `app/domain/catalog/models.py` rather than growing
that file, per the aligned implementation plan §3.1: the legacy `RackModel`/
`RackModelRevision`/`EquipmentModel`/`EquipmentModelRevision` tables in that file are
untouched in shape by this design (only `bridged_from_catalog_revision_id` is added to
the two revision tables, in `models.py` itself) and remain the tables installed
`Rack`/`Equipment` rows actually reference — see §4.7 ("the legacy bridge") for why a
second, richer aggregate exists instead of altering those tables directly.

`CatalogComponentOverride` (spec §6.1) and `CatalogImportJob` (spec §13.2) are
deliberately NOT part of this module — they belong to PR-7 and PR-6 respectively, in
their own migrations, per the aligned plan's corrected §1.2 item 1 (an earlier version of
that finding bundled all ten new tables into "every schema PR," which was wrong; only the
eight tables below are PR-1's).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin

IDENTITY_STATUSES = ("active", "deprecated")
CATEGORIES = ("rack", "equipment", "network_device", "pdu", "ups", "power_panel", "sensor")
LIFECYCLE_STATUSES = ("draft", "published", "retired")
DIMENSION_UNITS = ("mm", "in")
WEIGHT_UNITS = ("kg", "lb")
AIRFLOW_DIRECTIONS = ("front_to_rear", "front_to_top", "side_to_side", "other")
POWER_REDUNDANCY_MODES = ("single", "1+1", "n+1")
MEDIA_TYPES = ("copper", "fiber", "other")
PORT_ROLES = ("uplink", "access", "management", "stack", "other")
SIDES = ("front", "rear")
MARKER_TYPES = ("network_port", "power_supply", "module", "other")
MONITORING_PROTOCOLS = ("snmp", "other")
VALUE_TYPES = ("integer", "float", "string", "boolean", "counter", "gauge")
TRANSFORMS = ("none", "scale", "offset", "scale_and_offset")
MIME_TYPES = ("image/png", "image/jpeg")


class Manufacturer(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "manufacturer"
    __table_args__ = (CheckConstraint(f"status IN {IDENTITY_STATUSES!r}", name="status_allowed"),)

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")

    models: Mapped[list["CatalogModel"]] = relationship(back_populates="manufacturer")


class CatalogModel(Base, UUIDPkMixin, TimestampMixin):
    """The identity anchor — manufacturer, category, model name/number. Immutable once any
    revision under it has been published (§5.4's identity-lock trigger, migration
    `0017_catalog_manufacturer_and_model`'s `fn_reject_catalog_model_identity_change`);
    `description`/`tags`/`status` are never locked, since they carry no published meaning
    of their own (§5.4)."""

    __tablename__ = "catalog_model"
    __table_args__ = (
        CheckConstraint(f"category IN {CATEGORIES!r}", name="category_allowed"),
        CheckConstraint(f"status IN {IDENTITY_STATUSES!r}", name="status_allowed"),
        UniqueConstraint("manufacturer_id", "model_name", name="uq_catalog_model_manufacturer_id_model_name"),
    )

    manufacturer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manufacturer.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_number: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")

    manufacturer: Mapped[Manufacturer] = relationship(back_populates="models")
    revisions: Mapped[list["CatalogModelRevision"]] = relationship(back_populates="catalog_model")


class CatalogModelRevision(Base, UUIDPkMixin, TimestampMixin):
    """The immutable-once-published aggregate root (spec §4.2). Every physical/electrical
    column is nullable at the DB level on purpose — category-appropriate required-ness is
    a publish-time application validation (§5.2, PR-3), not a DB constraint, so a draft can
    be saved incrementally. What the DB enforces unconditionally: positive
    dimensions/weights/power values, enum membership, and (from migration
    `0020_catalog_legacy_bridge`) the legacy-bridge XOR. Immutability past `draft` is
    enforced by `fn_guard_catalog_model_revision_lifecycle` (migration `0017`), not by
    anything declared here — SQLAlchemy has no portable way to express that trigger."""

    __tablename__ = "catalog_model_revision"
    __table_args__ = (
        UniqueConstraint("catalog_model_id", "revision_number", name="uq_catalog_model_revision_catalog_model_id"),
        CheckConstraint(f"lifecycle_status IN {LIFECYCLE_STATUSES!r}", name="lifecycle_status_allowed"),
        CheckConstraint(f"dimension_unit IS NULL OR dimension_unit IN {DIMENSION_UNITS!r}", name="dimension_unit_allowed"),
        CheckConstraint("rack_unit_height IS NULL OR rack_unit_height > 0", name="rack_unit_height_positive"),
        CheckConstraint(f"weight_unit IS NULL OR weight_unit IN {WEIGHT_UNITS!r}", name="weight_unit_allowed"),
        CheckConstraint(
            f"airflow_direction IS NULL OR airflow_direction IN {AIRFLOW_DIRECTIONS!r}", name="airflow_direction_allowed"
        ),
        CheckConstraint("rated_power_w IS NULL OR rated_power_w >= 0", name="rated_power_w_non_negative"),
        CheckConstraint("typical_power_w IS NULL OR typical_power_w >= 0", name="typical_power_w_non_negative"),
        CheckConstraint("max_power_w IS NULL OR max_power_w >= 0", name="max_power_w_non_negative"),
        CheckConstraint(
            f"power_redundancy_mode IS NULL OR power_redundancy_mode IN {POWER_REDUNDANCY_MODES!r}",
            name="power_redundancy_mode_allowed",
        ),
        CheckConstraint(
            "NOT (legacy_rack_model_revision_id IS NOT NULL AND legacy_equipment_model_revision_id IS NOT NULL)",
            name="legacy_bridge_exclusive",
        ),
    )

    catalog_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")

    dimension_unit: Mapped[str | None] = mapped_column(String(4))
    width_value: Mapped[float | None] = mapped_column(Numeric(10, 3))
    height_value: Mapped[float | None] = mapped_column(Numeric(10, 3))
    depth_value: Mapped[float | None] = mapped_column(Numeric(10, 3))
    rack_unit_height: Mapped[int | None] = mapped_column(Integer)
    weight_unit: Mapped[str | None] = mapped_column(String(4))
    weight_value: Mapped[float | None] = mapped_column(Numeric(10, 3))
    mounting_orientation: Mapped[str | None] = mapped_column(String(32))
    supported_placement_types: Mapped[list | None] = mapped_column(JSONB)
    airflow_direction: Mapped[str | None] = mapped_column(String(16))

    rated_power_w: Mapped[float | None] = mapped_column(Numeric(10, 2))
    typical_power_w: Mapped[float | None] = mapped_column(Numeric(10, 2))
    max_power_w: Mapped[float | None] = mapped_column(Numeric(10, 2))
    heat_dissipation_btu_hr: Mapped[float | None] = mapped_column(Numeric(10, 2))
    power_redundancy_mode: Mapped[str | None] = mapped_column(String(8))

    cloned_from_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    retirement_reason: Mapped[str | None] = mapped_column(String(1000))
    allow_installation_when_retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # legacy_rack_model_revision_id / legacy_equipment_model_revision_id and their XOR CHECK
    # are added below, mapped as columns on this same class but created by migration
    # 0020_catalog_legacy_bridge — not migration 0017, which creates this table — since
    # they are additive and the migration that adds them must run after
    # rack_model_revision/equipment_model_revision already exist (spec §4.7).
    legacy_rack_model_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rack_model_revision.id", ondelete="RESTRICT"), unique=True
    )
    legacy_equipment_model_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_model_revision.id", ondelete="RESTRICT"), unique=True
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    catalog_model: Mapped[CatalogModel] = relationship(back_populates="revisions", foreign_keys=[catalog_model_id])


class NetworkPortTemplate(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "network_port_template"
    __table_args__ = (
        UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_network_port_template_catalog_model_revision_id"
        ),
        CheckConstraint(f"media_type IN {MEDIA_TYPES!r}", name="media_type_allowed"),
        CheckConstraint(f"role IN {PORT_ROLES!r}", name="role_allowed"),
        CheckConstraint(f"side IN {SIDES!r}", name="side_allowed"),
    )

    catalog_model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    numbering_pattern: Mapped[str | None] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    supported_speeds_mbps: Mapped[list] = mapped_column(JSONB, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="other", server_default="other")
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    module_group: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class PowerSupplyTemplate(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "power_supply_template"
    __table_args__ = (
        UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_power_supply_template_catalog_model_revision_id"
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(f"redundancy_mode IN {POWER_REDUNDANCY_MODES!r}", name="redundancy_mode_allowed"),
    )

    catalog_model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    redundancy_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="single", server_default="single")
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rated_voltage_min: Mapped[float | None] = mapped_column(Numeric(6, 1))
    rated_voltage_max: Mapped[float | None] = mapped_column(Numeric(6, 1))
    rated_frequency_hz: Mapped[float | None] = mapped_column(Numeric(5, 1))
    rated_current_a: Mapped[float | None] = mapped_column(Numeric(6, 2))
    hot_swappable: Mapped[bool | None] = mapped_column(Boolean)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class CatalogGraphic(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "catalog_graphic"
    __table_args__ = (
        UniqueConstraint("catalog_model_revision_id", "side", name="uq_catalog_graphic_catalog_model_revision_id"),
        CheckConstraint(f"side IN {SIDES!r}", name="side_allowed"),
        CheckConstraint(f"mime_type IN {MIME_TYPES!r}", name="mime_type_allowed"),
    )

    catalog_model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    markers: Mapped[list["CatalogGraphicMarker"]] = relationship(back_populates="catalog_graphic")


class CatalogGraphicMarker(Base, UUIDPkMixin, TimestampMixin):
    """Immutability and cross-revision integrity are enforced by
    `fn_validate_catalog_graphic_marker` (migration `0019_catalog_graphics`), not by
    anything declarative here — the parent revision is reached through
    `catalog_graphic_id`, which this table does not duplicate as its own
    `catalog_model_revision_id` column (spec §4.5: "marker position lives only here...
    avoiding two coordinate stores that could drift" — the same reasoning extends to not
    duplicating the revision reference)."""

    __tablename__ = "catalog_graphic_marker"
    __table_args__ = (
        CheckConstraint(f"marker_type IN {MARKER_TYPES!r}", name="marker_type_allowed"),
        CheckConstraint(
            "(marker_type = 'network_port' AND network_port_template_id IS NOT NULL AND power_supply_template_id IS NULL) OR "
            "(marker_type = 'power_supply' AND power_supply_template_id IS NOT NULL AND network_port_template_id IS NULL) OR "
            "(marker_type IN ('module', 'other') AND network_port_template_id IS NULL AND power_supply_template_id IS NULL)",
            name="marker_target_matches_type",
        ),
        CheckConstraint("marker_x >= 0 AND marker_x <= 1", name="marker_x_normalized"),
        CheckConstraint("marker_y >= 0 AND marker_y <= 1", name="marker_y_normalized"),
    )

    catalog_graphic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_graphic.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marker_type: Mapped[str] = mapped_column(String(16), nullable=False)
    network_port_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("network_port_template.id", ondelete="CASCADE")
    )
    power_supply_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("power_supply_template.id", ondelete="CASCADE")
    )
    label: Mapped[str | None] = mapped_column(String(128))
    marker_x: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False)
    marker_y: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    catalog_graphic: Mapped[CatalogGraphic] = relationship(back_populates="markers")


class MonitoringMetricTemplate(Base, UUIDPkMixin, TimestampMixin):
    """`default_warning_threshold`/`default_critical_threshold` are suggested seed values
    only — alarm evaluation must never read this table at evaluation time (spec §4.6);
    dereferenced exactly once, at installed-asset seed time, entirely outside PR-1's
    scope."""

    __tablename__ = "monitoring_metric_template"
    __table_args__ = (
        UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_monitoring_metric_template_catalog_model_revision_id"
        ),
        CheckConstraint(f"protocol IN {MONITORING_PROTOCOLS!r}", name="protocol_allowed"),
        CheckConstraint(
            "(protocol = 'other' AND protocol_other_label IS NOT NULL) OR "
            "(protocol <> 'other' AND protocol_other_label IS NULL)",
            name="protocol_other_label_matches_protocol",
        ),
        CheckConstraint(f"value_type IN {VALUE_TYPES!r}", name="value_type_allowed"),
        CheckConstraint(f"transform IN {TRANSFORMS!r}", name="transform_allowed"),
        CheckConstraint(
            "default_collection_interval_seconds IS NULL OR default_collection_interval_seconds > 0",
            name="default_collection_interval_seconds_positive",
        ),
    )

    catalog_model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)
    protocol_other_label: Mapped[str | None] = mapped_column(String(64))
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    oid: Mapped[str | None] = mapped_column(String(255))
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    scale: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=1, server_default="1")
    transform: Mapped[str] = mapped_column(String(16), nullable=False, default="none", server_default="none")
    offset: Mapped[float | None] = mapped_column(Numeric(18, 8))
    default_collection_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    default_warning_threshold: Mapped[float | None] = mapped_column(Numeric(18, 4))
    default_critical_threshold: Mapped[float | None] = mapped_column(Numeric(18, 4))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
