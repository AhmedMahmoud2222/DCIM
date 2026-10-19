"""M4 (Issue #150): instantiate a physical `PDU` row — plus the power-graph nodes that
make it a first-class citizen of the existing topology — from a *published*,
category='pdu' `CatalogModelRevision`. This mirrors
app/application/equipment_instantiation_service.py's Phase 10B contract for equipment;
the differences below are the two the domains genuinely have.

Why there is no legacy bridge here (the inverse of equipment's rule): PDU publication is
designer-side only (Issue #150 M3) — `publish_revision` mints no bridge row for
category='pdu', and migration 0055's lifecycle trigger requires *both* bridge columns to
stay NULL to publish one. So where equipment refuses to instantiate without its bridge,
this loader refuses a PDU revision whose bridge columns are *set*: that state is only
reachable if the M3 invariant was violated out-of-band, and dereferencing a bridge that
must not exist would silently reintroduce the coupling this milestone removed.

Snapshot, not live join (same discipline as equipment): `PDU.input_voltage` /
`PDU.rated_current_a` / `PDU.outlet_count` and every outlet `PowerNode`/`PDUOutlet` pair
copy the published revision's values once, here, at instantiation time. There is no FK
from the power domain back to `pdu_outlet_template` — only the historical provenance of
matching `outlet_number`/`label` — so a later revision of the same `CatalogModel` never
reaches an already-instantiated PDU, and nothing in this module ever mutates one.
`outlet_number` uniqueness is guaranteed twice over: `uq_pdu_outlet_template_cmr_on` at
the catalog layer and `uq_pdu_outlet_pdu_asset_id_outlet_number` at the power layer.

Deliberately not carried over: `pdu_phase` and `mounting_orientation` stay catalog-only —
the Phase 3 `PDU` domain (app/domain/power/models.py) has no columns for them and this
milestone does not invent new ones; per-phase values live on `PowerConnection` once the
PDU is wired to loads.

Atomicity: everything below runs in the caller's transaction and is flushed but never
committed here — the route writes audit + outbox and commits exactly once (every other
mutation endpoint's transaction boundary). Any failure rolls the whole instantiation
back; there is no state in which a `ManagedAsset` exists without its `PDU`/nodes/outlets."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, PDUOutletTemplate
from app.domain.identity.models import ManagedAsset
from app.domain.power.models import PDU, PDUOutlet, PowerNode


class PDUInstantiationRejected(Exception):
    """Raised for every reason a revision cannot be instantiated as a physical PDU (not
    published, not a 'pdu' category model, retired without `allow_installation_when_retired`,
    a legacy bridge column set on a PDU revision, or no outlet templates). The router maps
    this to a 409 — the exact `InstantiationRejected` contract equipment's route uses, a
    clean problem-detail response, never a raw IntegrityError."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def _load_publishable_pdu_revision(
    db: AsyncSession, revision_id: uuid.UUID
) -> tuple[CatalogModelRevision, list[PDUOutletTemplate]]:
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    model = await db.get(CatalogModel, revision.catalog_model_id)
    assert model is not None

    if model.category != "pdu":
        raise PDUInstantiationRejected(
            f"CatalogModel {model.id} has category {model.category!r}; only 'pdu' models can be instantiated as PDUs."
        )
    if revision.lifecycle_status == "draft":
        raise PDUInstantiationRejected(f"CatalogModelRevision {revision_id} is a draft and cannot be instantiated.")
    if revision.lifecycle_status == "retired" and not revision.allow_installation_when_retired:
        raise PDUInstantiationRejected(
            f"CatalogModelRevision {revision_id} is retired and installation has not been re-enabled for it."
        )
    if revision.legacy_rack_model_revision_id is not None or revision.legacy_equipment_model_revision_id is not None:
        # Defensive: a published category='pdu' revision carries no legacy bridge by
        # construction (0055 lifecycle trigger) — reachable only if that invariant is
        # ever violated, and refusing it is the safe side of the ambiguity.
        raise PDUInstantiationRejected(
            f"CatalogModelRevision {revision_id} carries a legacy bridge column; a pdu revision publishes without one "
            "and cannot be instantiated."
        )
    outlets = list(
        (
            await db.execute(
                select(PDUOutletTemplate)
                .where(PDUOutletTemplate.catalog_model_revision_id == revision.id)
                .order_by(PDUOutletTemplate.outlet_number)
            )
        ).scalars()
    )
    if not outlets:
        raise PDUInstantiationRejected(
            f"CatalogModelRevision {revision_id} has no outlet templates; a published pdu revision always has at "
            "least one, so this is a violated invariant."
        )
    return revision, outlets


async def instantiate_pdu(
    db: AsyncSession,
    *,
    asset_tag: str,
    catalog_model_revision_id: uuid.UUID,
    name: str,
    ip_address: str | None,
    protocol: str,
) -> tuple[PowerNode, PDU, list[PowerNode]]:
    """Creates `ManagedAsset(asset_type='pdu')` + the `pdu` graph `PowerNode` + the `PDU`
    subtype (the exact sequence `POST /power/pdus`'s `_create_power_asset` performs in
    app/api/v1/power.py), then one `PowerNode(node_type='pdu_outlet')` + `PDUOutlet` pair
    per `PDUOutletTemplate` on the revision — the mirror image of `instantiate_equipment`
    creating one port/inlet per template. `input_voltage`/`rated_current_a`/`outlet_count`
    come from the published revision, never from the caller. Raises
    NotFoundError/PDUInstantiationRejected; never commits or writes audit/outbox — the
    caller (the route handler) does that."""
    revision, outlets = await _load_publishable_pdu_revision(db, catalog_model_revision_id)

    asset = ManagedAsset(asset_type="pdu", asset_tag=asset_tag, lifecycle_status="planned")
    db.add(asset)
    await db.flush()

    pdu = PDU(
        id=asset.id,
        name=name,
        ip_address=ip_address,
        protocol=protocol,
        input_voltage=revision.pdu_input_voltage_v,
        rated_current_a=revision.pdu_input_current_a,
        outlet_count=len(outlets),
        version=1,
    )
    db.add(pdu)
    node = PowerNode(node_type="pdu", managed_asset_id=asset.id, label=name)
    db.add(node)
    await db.flush()

    outlet_nodes: list[PowerNode] = []
    for template in outlets:
        outlet_node = PowerNode(node_type="pdu_outlet", owning_asset_id=asset.id, label=template.label)
        db.add(outlet_node)
        await db.flush()
        db.add(
            PDUOutlet(
                power_node_id=outlet_node.id,
                pdu_asset_id=asset.id,
                outlet_number=template.outlet_number,
                # The legacy `pdu_outlet.label` column is String(64) while catalog labels
                # allow 128 — the full label always survives on the PowerNode (String(128))
                # above; the outlet row carries the display-truncated form.
                label=template.label[:64],
                state="unknown",
            )
        )
        outlet_nodes.append(outlet_node)

    await db.flush()
    return node, pdu, outlet_nodes
