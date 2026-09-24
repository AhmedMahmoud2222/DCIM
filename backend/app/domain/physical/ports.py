"""Phase 10B: physical port/power-inlet instances instantiated from Phase 10A catalog
templates (`NetworkPortTemplate`/`PowerSupplyTemplate`, app/domain/catalog/designer_models.py),
plus the connectivity edge linking an instantiated network port to another port (a patch
panel or switch interface, itself just another `EquipmentPort` row) or to a PDU outlet's
`PowerNode` (app/domain/power/models.py).

Snapshot decoupling: every row here copies the template's display fields at instantiation
time (app/application/equipment_instantiation_service.py) rather than joining to the
template live. `network_port_template_id`/`power_supply_template_id` are kept only as
read-only provenance — a published revision and its child templates are immutable and
never deleted (designer_models.py's own trigger-enforced guarantee), so the reference
stays valid for the deployed equipment's entire lifetime, but nothing here ever re-reads
it to decide what to display: a future draft revision that edits the same catalog model
has no effect on rows already instantiated from an earlier published revision (the
architectural requirement this module exists to satisfy).

`EquipmentPowerInlet` does not duplicate PowerNode's own bookkeeping — it is a thin,
catalog-aware sibling row (`power_node_id UNIQUE`) pointing at the same
`node_type='equipment_power_input'` `PowerNode` that `app/application/power_graph.py`/
`power_capacity.py` already know how to traverse for redundancy/capacity roll-ups
(app/api/v1/power.py's `POST /power/equipment-feeds` creates that same node_type by hand
today for equipment with no catalog template); instantiation just creates both rows
together instead of requiring a manual follow-up call."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.domain.catalog.designer_models import MEDIA_TYPES, PORT_ROLES, SIDES

PORT_CONNECTION_STATUSES = ("active", "planned", "faulted")


class EquipmentPort(Base, UUIDPkMixin, TimestampMixin):
    """One instantiated network port on a piece of physical equipment. `stable_key`
    mirrors the originating `NetworkPortTemplate.stable_key` (unique within the owning
    equipment, same as the template is unique within its revision) so a future revision
    upgrade action (out of this phase's scope — see the instantiation service's module
    docstring) has a stable join key to reconcile against, exactly like
    `clone_revision`'s own stable_key-based re-pointing (catalog_designer_service.py)."""

    __tablename__ = "equipment_port"
    __table_args__ = (
        UniqueConstraint("equipment_id", "stable_key", name="uq_equipment_port_equipment_id_stable_key"),
        CheckConstraint(f"media_type IN {MEDIA_TYPES!r}", name="media_type_allowed"),
        CheckConstraint(f"role IN {PORT_ROLES!r}", name="role_allowed"),
        CheckConstraint(f"side IN {SIDES!r}", name="side_allowed"),
    )

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    network_port_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("network_port_template.id", ondelete="RESTRICT")
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    supported_speeds_mbps: Mapped[list] = mapped_column(JSONB, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="other", server_default="other")
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    module_group: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class EquipmentPowerInlet(Base, UUIDPkMixin, TimestampMixin):
    """One instantiated power inlet. Each row owns exactly one `equipment_power_input`
    `PowerNode` (see module docstring) — `power_node_id` is UNIQUE, never shared, even
    when a `PowerSupplyTemplate.quantity > 1` instantiates several inlets from the same
    template (one PowerNode, and one row here, per physical inlet)."""

    __tablename__ = "equipment_power_inlet"
    __table_args__ = (UniqueConstraint("equipment_id", "stable_key", name="uq_equipment_power_inlet_equipment_id_stable_key"),)

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    power_supply_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("power_supply_template.id", ondelete="RESTRICT")
    )
    power_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("power_node.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class PortConnection(Base, UUIDPkMixin, TimestampMixin):
    """A physical cabling edge from an instantiated equipment port to either another
    `EquipmentPort` (a patch panel port or an adjacent switch interface — both are
    ordinary `EquipmentPort` rows on their own `Equipment`) or a PDU outlet's `PowerNode`
    (`node_type='pdu_outlet'`, validated by the service layer, not a DB constraint — the
    same division of labor `power_graph.py` already uses for `PowerConnection` cycle
    prevention, per that module's own docstring).

    `source_port_id` is UNIQUE: one instantiated port carries at most one outgoing
    connection record at a time (reconnecting replaces it — see the instantiation
    service's `connect_port`). This does not by itself stop the *target* side from being
    claimed by more than one source; per this repo's established pattern
    (PowerConnection's cycle prevention, PDUOutlet's subtype integrity), that duplicate-
    target case is a service-layer check, not a schema-level one, deferred past this
    phase's scope."""

    __tablename__ = "port_connection"
    __table_args__ = (
        UniqueConstraint("source_port_id", name="uq_port_connection_source_port_id"),
        CheckConstraint("source_port_id <> target_port_id", name="no_self_loop"),
        CheckConstraint(
            "(target_port_id IS NOT NULL AND target_power_node_id IS NULL) OR "
            "(target_port_id IS NULL AND target_power_node_id IS NOT NULL)",
            name="target_exclusive",
        ),
        CheckConstraint(f"status IN {PORT_CONNECTION_STATUSES!r}", name="status_allowed"),
    )

    source_port_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("equipment_port.id", ondelete="CASCADE"), nullable=False)
    target_port_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment_port.id", ondelete="RESTRICT"))
    target_power_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("power_node.id", ondelete="RESTRICT"))
    cable_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
