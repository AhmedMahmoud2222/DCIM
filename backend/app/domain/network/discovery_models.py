"""Issue #101: LLDP/CDP neighbor *evidence*.

A `DiscoveredNeighbor` is what a collector observed on one device port: never authoritative
topology. It carries provenance (collector, integration, protocol, scan, first/last seen,
raw protocol evidence) and a reconciliation state that only an operator can move to
`confirmed`. Nothing in this table writes `port_connection`, `cable` or any inventory row;
turning a confirmed adjacency into a physical cable is a separate, explicit action
(`cable_service.create_cable_from_neighbor`).

States (`reconciliation_state`):

* `unmatched`  - evidence retained, no deterministic match (or insufficient evidence);
* `ambiguous`  - several candidates, or contradictory evidence; never guessed;
* `proposed`   - both ends resolved deterministically; waiting for an operator;
* `conflict`   - resolvable, but contradicts authoritative evidence (a confirmed adjacency
  or live cable); the authoritative side is untouched;
* `confirmed`  - an operator linked `local_port_id` and `remote_port_id`;
* `rejected`   - an operator declared the evidence wrong; re-observations stay rejected.

`status` tracks observation freshness (`active`/`stale`) independently of reconciliation:
a stale confirmed neighbor stays confirmed but is flagged.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

NEIGHBOR_PROTOCOLS = ("lldp", "cdp")
NEIGHBOR_STATUSES = ("active", "stale")
RECONCILIATION_STATES = ("unmatched", "ambiguous", "proposed", "conflict", "confirmed", "rejected")
OPEN_RECONCILIATION_STATES = ("unmatched", "ambiguous", "proposed", "conflict")


class DiscoveredNeighbor(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "discovered_neighbor"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_discovered_neighbor_identity_key"),
        CheckConstraint(f"protocol IN {NEIGHBOR_PROTOCOLS!r}", name="protocol_allowed"),
        CheckConstraint(f"status IN {NEIGHBOR_STATUSES!r}", name="status_allowed"),
        CheckConstraint(f"reconciliation_state IN {RECONCILIATION_STATES!r}", name="reconciliation_state_allowed"),
        CheckConstraint("local_port_name IS NOT NULL OR local_port_ref IS NOT NULL", name="local_port_identified"),
        CheckConstraint("jsonb_typeof(capabilities) = 'array'", name="capabilities_is_array"),
        CheckConstraint("jsonb_typeof(raw_evidence) = 'object'", name="raw_evidence_is_object"),
        CheckConstraint("jsonb_typeof(match_evidence) = 'object'", name="match_evidence_is_object"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        CheckConstraint(
            "local_port_id IS NULL OR remote_port_id IS NULL OR local_port_id <> remote_port_id", name="distinct_linked_ports"
        ),
        Index("ix_discovered_neighbor_integration_status", "integration_id", "status"),
        Index("ix_discovered_neighbor_state", "reconciliation_state", "status"),
        Index("ix_discovered_neighbor_local_port", "local_port_id"),
        Index("ix_discovered_neighbor_remote_port", "remote_port_id"),
    )

    # -- provenance
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integration.id", ondelete="CASCADE"), nullable=False)
    source_collector_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collector.id", ondelete="SET NULL"))
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_id: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="active", server_default="active")

    # -- normalized evidence
    local_port_name: Mapped[str | None] = mapped_column(String(255))
    local_port_ref: Mapped[str | None] = mapped_column(String(64))
    remote_chassis_ident: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_chassis_subtype: Mapped[str | None] = mapped_column(String(32))
    remote_port_ident: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_port_subtype: Mapped[str | None] = mapped_column(String(32))
    remote_port_description: Mapped[str | None] = mapped_column(String(255))
    remote_system_name: Mapped[str | None] = mapped_column(String(255))
    remote_system_description: Mapped[str | None] = mapped_column(String(512))
    remote_platform: Mapped[str | None] = mapped_column(String(255))
    remote_management_address: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    native_vlan: Mapped[int | None] = mapped_column(Integer)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    raw_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # -- reconciliation (operator-controlled)
    reconciliation_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unmatched", server_default="unmatched")
    match_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    local_port_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment_port.id", ondelete="SET NULL"))
    remote_port_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment_port.id", ondelete="SET NULL"))
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
