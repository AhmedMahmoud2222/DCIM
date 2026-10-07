"""Issue #101: first-class physical cables.

Three different things describe "port A is connected to port B", deliberately kept apart:

* `Cable` + `CableEndpoint` - the **physical** object: a labelled cable of a type, with two
  endpoints, a lifecycle (`planned` -> `installed` -> `removed`) and provenance. Removed
  cables stay as history.
* `PortConnection` (existing, `app/domain/physical/ports.py`) - the **logical** edge the
  impact/topology services traverse. A live cable *realizes* one `PortConnection` (see
  `cable_service`); a cable never deletes or rewrites a connection it did not create.
* `DiscoveredNeighbor` - LLDP/CDP **evidence**. It becomes a cable only through the
  explicit `create_cable_from_neighbor` action on a confirmed neighbor.

Integrity is enforced by PostgreSQL, not only by services:

* `cable.is_live` is a generated column (`status IN ('planned','installed')`) and
  `cable_endpoint (cable_id, is_live)` references it with `ON UPDATE CASCADE`, so the
  endpoint's liveness can never disagree with its cable;
* a partial unique index allows each port at most one *live* endpoint;
* a deferred constraint trigger requires every cable to have exactly one `A` and one `B`
  endpoint at commit.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

CABLE_TYPES = ("copper_utp", "copper_stp", "coax", "fiber_sm", "fiber_mm", "dac", "aoc", "console", "other")
CABLE_STATUSES = ("planned", "installed", "removed")
LIVE_CABLE_STATUSES = ("planned", "installed")
CABLE_SOURCES = ("manual", "discovery_confirmed", "import")
CABLE_ENDS = ("A", "B")
# which EquipmentPort.media_type values a cable type may terminate on
CABLE_MEDIA_COMPATIBILITY = {
    "copper_utp": ("copper", "other"), "copper_stp": ("copper", "other"), "coax": ("copper", "other"),
    "fiber_sm": ("fiber", "other"), "fiber_mm": ("fiber", "other"),
}


class Cable(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "cable"
    __table_args__ = (
        UniqueConstraint("id", "is_live", name="uq_cable_id_is_live"),
        CheckConstraint(f"cable_type IN {CABLE_TYPES!r}", name="cable_type_allowed"),
        CheckConstraint(f"status IN {CABLE_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"source IN {CABLE_SOURCES!r}", name="source_allowed"),
        CheckConstraint("length_m IS NULL OR length_m > 0", name="length_positive"),
        CheckConstraint("jsonb_typeof(route_metadata) = 'object'", name="route_metadata_is_object"),
        CheckConstraint("btrim(label) <> ''", name="label_not_blank"),
        CheckConstraint(
            "(status = 'planned' AND installed_at IS NULL AND removed_at IS NULL) OR "
            "(status = 'installed' AND installed_at IS NOT NULL AND removed_at IS NULL) OR "
            "(status = 'removed' AND removed_at IS NOT NULL)",
            name="lifecycle_timestamps_match_status",
        ),
        CheckConstraint(
            "installed_at IS NULL OR removed_at IS NULL OR removed_at >= installed_at", name="removed_after_installed"
        ),
        CheckConstraint(
            "source_neighbor_id IS NULL OR source = 'discovery_confirmed'", name="neighbor_provenance_matches_source"
        ),
        Index("uq_cable_live_label", text("lower(label)"), unique=True, postgresql_where=text("is_live")),
        Index("ix_cable_status", "status"),
        Index("ix_cable_type", "cable_type"),
    )

    label: Mapped[str] = mapped_column(String(64), nullable=False)
    cable_type: Mapped[str] = mapped_column(String(16), nullable=False)
    connector_a: Mapped[str | None] = mapped_column(String(32))
    connector_b: Mapped[str | None] = mapped_column(String(32))
    length_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    route_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", server_default="planned")
    is_live: Mapped[bool] = mapped_column(
        Boolean, Computed("status IN ('planned', 'installed')", persisted=True), nullable=False
    )
    installed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="manual", server_default="manual")
    source_neighbor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("discovered_neighbor.id", ondelete="SET NULL"))
    port_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("port_connection.id", ondelete="SET NULL"))
    port_connection_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(String(2000))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class CableEndpoint(Base, UUIDPkMixin):
    __tablename__ = "cable_endpoint"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cable_id", "is_live"], ["cable.id", "cable.is_live"], name="fk_cable_endpoint_cable_liveness",
            onupdate="CASCADE", ondelete="CASCADE",
        ),
        UniqueConstraint("cable_id", "end_label", name="uq_cable_endpoint_cable_end"),
        UniqueConstraint("cable_id", "equipment_port_id", name="uq_cable_endpoint_cable_port"),
        CheckConstraint(f"end_label IN {CABLE_ENDS!r}", name="end_allowed"),
        Index("uq_cable_endpoint_live_port", "equipment_port_id", unique=True, postgresql_where=text("is_live")),
        Index("ix_cable_endpoint_port", "equipment_port_id"),
    )

    cable_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False)
    end_label: Mapped[str] = mapped_column(String(1), nullable=False)
    equipment_port_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("equipment_port.id", ondelete="RESTRICT"), nullable=False)
