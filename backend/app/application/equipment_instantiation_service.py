"""Phase 10B: instantiate a physical `Equipment` row, its `EquipmentPort`/
`EquipmentPowerInlet` children, and the `PowerNode` each inlet owns, from a *published*
`CatalogModelRevision` (docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-
design.md's PR-1..PR-5 groundwork; app/domain/catalog/designer_models.py).

Why this goes through the legacy bridge (`equipment_model_revision`), not the catalog
revision directly: `Equipment.model_revision_id` is a NOT NULL FK to
`equipment_model_revision` (app/domain/physical/models.py), unchanged in shape by this
phase — publishing a category='equipment' revision already mints exactly one bridged
`EquipmentModelRevision` row (`catalog_designer_service.publish_revision`), and only that
category can ever reach this bridge (category in ('rack', 'equipment') is the sole set
the 0020 migration's lifecycle trigger allows to publish at all). Instantiation therefore
requires the revision to be published *and* category='equipment' — draft, retired
(unless explicitly allowed), and non-equipment revisions are all rejected before any row
is written. `Equipment.catalog_model_revision_id` (the new Phase 10B snapshot column) is
set alongside `model_revision_id` so future code can read the richer catalog revision
directly without walking the bridge, while every pre-existing consumer of
`model_revision_id` keeps working unmodified.

Snapshot, not live join: every `EquipmentPort`/`EquipmentPowerInlet` row copies its
template's display fields once, here, at instantiation time. A later draft edit to the
same `CatalogModelRevision`'s ports/PSUs (impossible for *this* revision once published,
but very possible for a later revision of the same `CatalogModel`) never reaches
already-instantiated equipment — there is no live foreign key an evaluator could
dereference at read time, only the historical `network_port_template_id`/
`power_supply_template_id` provenance pointer (see ports.py's own docstring).

Explicitly out of scope for this phase (no code here attempts it): a "revision upgrade"
action that re-points an existing Equipment row's snapshot at a newer published revision
and reconciles its port/inlet rows by stable_key. Nothing in this module ever mutates an
already-instantiated Equipment's ports/inlets — only `instantiate_equipment` (create) and
`connect_port`/`disconnect_port` (cabling) write to these tables."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.domain.catalog.designer_models import (
    CatalogModel,
    CatalogModelRevision,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.catalog.models import EquipmentModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPort, EquipmentPowerInlet, PortConnection
from app.domain.power.models import PowerNode


class InstantiationRejected(Exception):
    """Raised for every reason a revision cannot be instantiated (not published, not an
    'equipment' category model, or retired without `allow_installation_when_retired`).
    The router maps this to a 409, mirroring `ValidationFailed`'s 422 mapping in
    `catalog_designer_service.py` — a clean problem-detail response, never a raw
    IntegrityError or an assertion failure."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def _load_publishable_revision(db: AsyncSession, revision_id: uuid.UUID) -> tuple[CatalogModelRevision, CatalogModel]:
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    model = await db.get(CatalogModel, revision.catalog_model_id)
    assert model is not None

    if model.category != "equipment":
        raise InstantiationRejected(
            f"CatalogModel {model.id} has category {model.category!r}; only 'equipment' models can be instantiated "
            "as physical equipment."
        )
    if revision.lifecycle_status == "draft":
        raise InstantiationRejected(f"CatalogModelRevision {revision_id} is a draft and cannot be instantiated.")
    if revision.lifecycle_status == "retired" and not revision.allow_installation_when_retired:
        raise InstantiationRejected(
            f"CatalogModelRevision {revision_id} is retired and installation has not been re-enabled for it."
        )
    if revision.legacy_equipment_model_revision_id is None:
        # Defensive: every published category='equipment' revision has this set by
        # publish_revision (catalog_designer_service.py) — reachable only if that
        # invariant is ever violated.
        raise InstantiationRejected(f"CatalogModelRevision {revision_id} has no legacy equipment bridge; cannot instantiate.")
    return revision, model


async def instantiate_equipment(
    db: AsyncSession,
    *,
    asset_tag: str,
    catalog_model_revision_id: uuid.UUID,
    hostname: str | None,
    ip_address: str | None,
    owner: str | None,
    service: str | None,
    environment: str | None,
    notes: str | None,
) -> Equipment:
    """Creates `ManagedAsset` + `Equipment` (mirroring `POST /equipment`'s own sequence in
    app/api/v1/equipment.py), then one `EquipmentPort` per `NetworkPortTemplate` and one
    `EquipmentPowerInlet` (+ owning `PowerNode`) per unit of `PowerSupplyTemplate.quantity`
    on the revision. Raises NotFoundError/InstantiationRejected; never commits or writes
    audit/outbox — the caller (the route handler) does that, matching every other
    equipment-mutation endpoint's transaction boundary."""
    revision, _model = await _load_publishable_revision(db, catalog_model_revision_id)

    legacy_revision = await db.get(EquipmentModelRevision, revision.legacy_equipment_model_revision_id)
    assert legacy_revision is not None

    asset = ManagedAsset(asset_type="equipment", asset_tag=asset_tag, lifecycle_status="planned")
    db.add(asset)
    await db.flush()

    equipment = Equipment(
        id=asset.id,
        model_revision_id=legacy_revision.id,
        catalog_model_revision_id=revision.id,
        hostname=hostname,
        ip_address=ip_address,
        owner=owner,
        service=service,
        environment=environment,
        notes=notes,
    )
    db.add(equipment)
    await db.flush()

    ports = list(
        (
            await db.execute(
                select(NetworkPortTemplate)
                .where(NetworkPortTemplate.catalog_model_revision_id == revision.id)
                .order_by(NetworkPortTemplate.sort_order)
            )
        ).scalars()
    )
    for template in ports:
        db.add(
            EquipmentPort(
                equipment_id=equipment.id,
                network_port_template_id=template.id,
                stable_key=template.stable_key,
                display_name=template.display_name,
                media_type=template.media_type,
                supported_speeds_mbps=template.supported_speeds_mbps,
                connector_type=template.connector_type,
                role=template.role,
                side=template.side,
                module_group=template.module_group,
                sort_order=template.sort_order,
            )
        )

    psus = list(
        (
            await db.execute(
                select(PowerSupplyTemplate)
                .where(PowerSupplyTemplate.catalog_model_revision_id == revision.id)
                .order_by(PowerSupplyTemplate.sort_order)
            )
        ).scalars()
    )
    for psu_template in psus:
        for unit in range(psu_template.quantity):
            label = psu_template.label if psu_template.quantity == 1 else f"{psu_template.label} {unit + 1}"
            stable_key = (
                psu_template.stable_key if psu_template.quantity == 1 else f"{psu_template.stable_key}-{unit + 1}"
            )
            node = PowerNode(node_type="equipment_power_input", owning_asset_id=equipment.id, label=label)
            db.add(node)
            await db.flush()
            db.add(
                EquipmentPowerInlet(
                    equipment_id=equipment.id,
                    power_supply_template_id=psu_template.id,
                    power_node_id=node.id,
                    stable_key=stable_key,
                    label=label,
                    connector_type=psu_template.connector_type,
                    sort_order=psu_template.sort_order,
                )
            )

    await db.flush()
    return equipment


async def list_equipment_ports(db: AsyncSession, *, equipment_id: uuid.UUID) -> list[EquipmentPort]:
    return list(
        (
            await db.execute(
                select(EquipmentPort).where(EquipmentPort.equipment_id == equipment_id).order_by(EquipmentPort.sort_order)
            )
        ).scalars()
    )


async def list_equipment_power_inlets(db: AsyncSession, *, equipment_id: uuid.UUID) -> list[EquipmentPowerInlet]:
    return list(
        (
            await db.execute(
                select(EquipmentPowerInlet)
                .where(EquipmentPowerInlet.equipment_id == equipment_id)
                .order_by(EquipmentPowerInlet.sort_order)
            )
        ).scalars()
    )


async def list_port_connections(db: AsyncSession, *, port_ids: list[uuid.UUID]) -> dict[uuid.UUID, PortConnection]:
    """Keyed by `source_port_id` — at most one row per port (schema-enforced, ports.py)."""
    if not port_ids:
        return {}
    rows = (
        await db.execute(select(PortConnection).where(PortConnection.source_port_id.in_(port_ids)))
    ).scalars()
    return {row.source_port_id: row for row in rows}


class PortNotFound(Exception):
    pass


class InvalidPortTarget(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def connect_port(
    db: AsyncSession,
    *,
    source_port_id: uuid.UUID,
    target_port_id: uuid.UUID | None,
    target_power_node_id: uuid.UUID | None,
    cable_id: str | None,
    status: str,
) -> PortConnection:
    """Links `source_port_id` (an instantiated port on the equipment being cabled) to
    either `target_port_id` (a patch panel port or adjacent switch interface — any other
    `EquipmentPort` row) or `target_power_node_id` (a PDU outlet's `PowerNode`; validated
    to actually be node_type='pdu_outlet' here, at the service layer, the same division of
    labor `power_graph.py`'s cycle prevention already uses for `PowerConnection` per that
    module's own docstring — never a DB constraint). Exactly one of the two targets must
    be given (mirrors `PortConnection`'s own CHECK). Reconnecting an already-connected
    source port replaces its existing connection row instead of erroring, since
    `source_port_id` is unique and a technician re-patching a cable is the expected,
    ordinary case, not a conflict to reject."""
    source = await db.get(EquipmentPort, source_port_id)
    if source is None:
        raise PortNotFound(f"EquipmentPort {source_port_id} not found.")

    if (target_port_id is None) == (target_power_node_id is None):
        raise InvalidPortTarget("Exactly one of target_port_id or target_power_node_id must be set.")

    if target_port_id is not None:
        if target_port_id == source_port_id:
            raise InvalidPortTarget("A port cannot be connected to itself.")
        target = await db.get(EquipmentPort, target_port_id)
        if target is None:
            raise PortNotFound(f"EquipmentPort {target_port_id} not found.")
    else:
        assert target_power_node_id is not None
        node = await db.get(PowerNode, target_power_node_id)
        if node is None:
            raise PortNotFound(f"PowerNode {target_power_node_id} not found.")
        if node.node_type != "pdu_outlet":
            raise InvalidPortTarget(f"PowerNode {target_power_node_id} is not a pdu_outlet (got {node.node_type!r}).")

    existing = (
        await db.execute(select(PortConnection).where(PortConnection.source_port_id == source_port_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.target_port_id = target_port_id
        existing.target_power_node_id = target_power_node_id
        existing.cable_id = cable_id
        existing.status = status
        connection = existing
    else:
        connection = PortConnection(
            source_port_id=source_port_id, target_port_id=target_port_id, target_power_node_id=target_power_node_id,
            cable_id=cable_id, status=status,
        )
        db.add(connection)

    await db.flush()
    return connection
