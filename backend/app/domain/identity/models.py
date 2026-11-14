"""ManagedAsset: the identity + lifecycle anchor for every independently-lifecycled
physical asset (docs/history/ARCHITECTURE_REVIEW.md §4/§4a/§4b). Phase 1 implements this table alone,
as a bare identity anchor with no concrete subtype (Rack/Equipment/PDU/...) attached yet —
those are Phase 2/3/7 (§9 of the Phase 1 prompt). asset_type is deliberately a plain,
extensible string column (not a fixed Postgres ENUM) so a future subtype can be added by
inserting a new allowed value, not by altering a type — the CHECK constraint below is the
one place that list of allowed values lives until a subtype table needs it."""

import copy
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin

ASSET_TYPES = ("rack", "equipment", "pdu", "ups", "generator", "power_panel", "sensor", "cable", "crac", "crah", "chiller")

LIFECYCLE_STATUSES = (
    "planned",
    "installed",
    "active",
    "reserved",
    "maintenance",
    "decommissioned",
    "removed",
)

# Visible states in the equipment lifecycle UI and administrator settings.
# `reserved` is retained internally in LIFECYCLE_STATUSES for backward compatibility
# and historical/legacy records, and is excluded from UI selection.
VISIBLE_LIFECYCLE_STATUSES: tuple[str, ...] = (
    "planned",
    "installed",
    "active",
    "maintenance",
    "decommissioned",
    "removed",
)

LEGACY_LIFECYCLE_STATUSES: tuple[str, ...] = ("reserved",)

LIFECYCLE_BADGE_COLORS: tuple[str, ...] = (
    "neutral",
    "info",
    "ok",
    "caution",
    "serious",
    "danger",
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

MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS: dict[str, list[str]] = {
    status: [
        target
        for target in VISIBLE_LIFECYCLE_STATUSES
        if target in ALLOWED_LIFECYCLE_TRANSITIONS[status]
    ]
    for status in VISIBLE_LIFECYCLE_STATUSES
}

DEFAULT_LIFECYCLE_STATES: list[dict[str, Any]] = [
    {
        "status": "planned",
        "label": "Planned",
        "description": "Equipment is planned or procured and awaiting physical installation.",
        "badge_color": "neutral",
        "display_order": 1,
        "allowed_transitions": ["installed", "removed"],
    },
    {
        "status": "installed",
        "label": "Installed",
        "description": "Equipment is physically mounted or placed and ready for commissioning.",
        "badge_color": "info",
        "display_order": 2,
        "allowed_transitions": ["active", "maintenance", "removed"],
    },
    {
        "status": "active",
        "label": "Active",
        "description": "Equipment is commissioned and in production service.",
        "badge_color": "ok",
        "display_order": 3,
        "allowed_transitions": ["maintenance", "decommissioned"],
    },
    {
        "status": "maintenance",
        "label": "Maintenance",
        "description": "Equipment is undergoing scheduled or corrective maintenance.",
        "badge_color": "caution",
        "display_order": 4,
        "allowed_transitions": ["active", "decommissioned"],
    },
    {
        "status": "decommissioned",
        "label": "Decommissioned",
        "description": "Equipment is permanently retired from service and awaiting physical removal.",
        "badge_color": "serious",
        "display_order": 5,
        "allowed_transitions": ["removed"],
    },
    {
        "status": "removed",
        "label": "Removed",
        "description": "Equipment has been physically removed from the facility.",
        "badge_color": "danger",
        "display_order": 6,
        "allowed_transitions": [],
    },
]


def default_lifecycle_states_config() -> list[dict[str, Any]]:
    return copy.deepcopy(DEFAULT_LIFECYCLE_STATES)


class ManagedAsset(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "managed_asset"
    __table_args__ = (
        CheckConstraint(f"asset_type IN {ASSET_TYPES!r}", name="asset_type_allowed"),
        CheckConstraint(f"lifecycle_status IN {LIFECYCLE_STATUSES!r}", name="lifecycle_status_allowed"),
        # Unknown historical/import dates remain NULL, including terminal states.
        CheckConstraint(
            "decommissioned_at IS NULL OR lifecycle_status IN ('decommissioned', 'removed')",
            name="decommissioned_at_terminal",
        ),
        UniqueConstraint("replaces_asset_id", name="uq_managed_asset_replaces_asset_id"),
        # Migration 0007 (F-M3): composite unique backing pdu_outlet's composite FK —
        # `(pdu_asset_id, pdu_asset_expected_type)` can only reference a ManagedAsset
        # whose own asset_type is 'pdu'.
        UniqueConstraint("id", "asset_type", name="uq_managed_asset_id_asset_type"),
        # Findings M2/M3 (docs/history/PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
        # docs/history/PHASE1_CORRECTION_REPORT.md): an asset cannot replace itself. Longer cycles
        # (A replaces B, B replaces A; or longer chains) cannot be expressed as a
        # single-row CHECK constraint — that invariant is enforced by the
        # trg_managed_asset_replacement_acyclic trigger added in migration 0003, which
        # walks the full replacement chain on INSERT/UPDATE.
        CheckConstraint(
            "replaces_asset_id IS NULL OR replaces_asset_id != id", name="no_self_replacement"
        ),
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


class LifecycleSettings(Base):
    """Singleton persisted administrator configuration for the six visible lifecycle states."""

    __tablename__ = "lifecycle_settings"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    states_config: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=default_lifecycle_states_config
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
