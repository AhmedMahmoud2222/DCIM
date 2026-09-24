"""Phase 10A PR-3: catalog lifecycle service (docs/superpowers/specs/2026-09-23-phase-
10a-asset-catalog-designer-design.md §5, aligned plan §3.3).

Holds the logic too complex or too safety-critical to inline in a route handler: the
publish-time required-field validation-rule evaluation (§5.2), the one-transaction publish
sequence (§4.7/§5.3 — parent-row lock, validate, legacy-bridge find-or-create, forward and
reverse bridge writes, publication metadata), and clone's deep-copy-with-stable-key-
preservation (§5.5). Straightforward CRUD (manufacturer/model create, model metadata
PATCH, revision scalar PATCH, child-row add/edit/delete, retire, retire-override) is
inlined directly in `app/api/v1/catalog_designer.py`, matching this repo's existing
`racks.py`/`equipment.py` convention of not delegating simple mutations to a service
module.

Concurrency, by construction, not by retry loops:
- Two concurrent `POST .../revisions` (or clone) calls computing the same
  `max(revision_number) + 1` collide on `catalog_model_revision`'s own
  `UNIQUE(catalog_model_id, revision_number)` constraint; the app-wide `IntegrityError`
  handler (app/core/errors.py) already turns that into a clean 409, so no bespoke handling
  is needed here.
- Two concurrent publishes of the *same* revision serialize through `publish_revision`'s
  own `SELECT ... FOR UPDATE` below: the second blocks until the first commits or rolls
  back, then re-reads a `lifecycle_status` that is no longer `'draft'` and is rejected —
  never a corrupted partial publish, matching PR-1's own `fn_guard_catalog_model_revision_
  lifecycle()` discipline for child writes.
- The legacy-model find-or-create (`_find_or_create_rack_model`/`_find_or_create_
  equipment_model`) uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id` followed by a
  fallback `SELECT`, the same race-safe upsert pattern already used by
  `telemetry_service.py`'s dedup insert — never a bare `SELECT` then unconditional
  `INSERT`, which would raise on a genuine concurrent conflict instead of finding the
  now-existing row.
"""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.catalog.designer_models import (
    CatalogGraphic,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    MonitoringMetricTemplate,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision

_MM_PER_INCH = 25.4
_KG_PER_LB = 0.45359237


# --------------------------------------------------------------------------- Validation


class ValidationIssue(BaseModel):
    field: str
    code: str
    message: str


class ValidationSummary(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []


def validate_revision_for_publish(
    revision: CatalogModelRevision,
    category: str,
    ports: list[NetworkPortTemplate],
    psus: list[PowerSupplyTemplate],
    metrics: list[MonitoringMetricTemplate],
) -> ValidationSummary:
    """Spec §5.2's rule list, representative-not-exhaustive per the spec's own wording.
    Every numeric/enum column here is nullable at the DB level by design (§4.2) so a draft
    can be saved incrementally — required-ness for *this* category is enforced only here,
    at publish (and pre-publish `/validate`) time, never as a DB constraint."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def require(value: object, field_name: str, message: str) -> None:
        if value is None:
            errors.append(ValidationIssue(field=field_name, code="required_for_category", message=message))

    if category in ("rack", "equipment"):
        require(revision.dimension_unit, "dimension_unit", f"Dimension unit is required for {category} models.")
        require(revision.width_value, "width_value", f"Width is required for {category} models.")
        require(revision.height_value, "height_value", f"Height is required for {category} models.")
        require(revision.depth_value, "depth_value", f"Depth is required for {category} models.")
        require(revision.weight_unit, "weight_unit", f"Weight unit is required for {category} models.")
        require(revision.weight_value, "weight_value", f"Weight is required for {category} models.")

    if category == "rack":
        require(revision.rack_unit_height, "rack_unit_height", "Rack unit height is required for rack models.")
    elif category == "equipment":
        placement_types = revision.supported_placement_types or []
        if "rack_mounted" in placement_types:
            require(
                revision.rack_unit_height,
                "rack_unit_height",
                "Rack unit height is required when rack_mounted is a supported placement type.",
            )

    for index, metric in enumerate(metrics):
        if metric.protocol == "snmp" and not metric.oid:
            errors.append(
                ValidationIssue(
                    field=f"monitoring_metrics[{index}].oid", code="missing_oid_for_snmp",
                    message="SNMP metric requires an OID.",
                )
            )

    has_nameplate_power = (
        revision.rated_power_w is not None or revision.typical_power_w is not None or revision.max_power_w is not None
    )
    if has_nameplate_power and not psus:
        errors.append(
            ValidationIssue(
                field="power_supplies", code="power_supply_required",
                message="At least one power supply is required when a nameplate power figure is set.",
            )
        )

    oid_first_index: dict[str, int] = {}
    for index, metric in enumerate(metrics):
        if not metric.oid:
            continue
        if metric.oid in oid_first_index:
            first = metrics[oid_first_index[metric.oid]]
            warnings.append(
                ValidationIssue(
                    field=f"monitoring_metrics[{index}].oid", code="duplicate_oid",
                    message=f"Same OID as '{first.metric_name}' (different stable_key).",
                )
            )
        else:
            oid_first_index[metric.oid] = index

    return ValidationSummary(valid=not errors, errors=errors, warnings=warnings)


class ValidationFailed(Exception):
    """Raised by publish_revision when validate_revision_for_publish finds errors — the
    router catches this and returns the summary as the 422 body verbatim (spec §5.2:
    publish "re-runs this exact check server-side and rejects with 422, the same error
    shape"), never FastAPI's generic RequestValidationError envelope."""

    def __init__(self, summary: ValidationSummary):
        self.summary = summary
        super().__init__("Revision failed publish validation.")


# --------------------------------------------------------------------------- Unit conversion


def _convert_physical(revision: CatalogModelRevision) -> tuple[int | None, int | None, int | None, int | None]:
    """Converts the draft's authored dimension/weight unit to the legacy tables' fixed
    mm/kg integer columns, rounding to the nearest whole unit (the legacy schema's own
    precision, unchanged by this design). `rack_unit_height` is a U count, never a
    physical-unit value, so it passes through unconverted."""

    def _mm(value: float | None) -> int | None:
        if value is None:
            return None
        return round(float(value) * _MM_PER_INCH) if revision.dimension_unit == "in" else round(float(value))

    def _kg(value: float | None) -> int | None:
        if value is None:
            return None
        return round(float(value) * _KG_PER_LB) if revision.weight_unit == "lb" else round(float(value))

    return revision.rack_unit_height, _mm(revision.width_value), _mm(revision.depth_value), _kg(revision.weight_value)


# --------------------------------------------------------------------------- Legacy find-or-create


async def _find_or_create_rack_model(db: AsyncSession, *, manufacturer_name: str, model_name: str) -> uuid.UUID:
    inserted = (
        await db.execute(
            pg_insert(RackModel)
            .values(id=uuid.uuid4(), manufacturer=manufacturer_name, model_name=model_name)
            .on_conflict_do_nothing(constraint="uq_rack_model_manufacturer_model_name")
            .returning(RackModel.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    return (
        await db.execute(
            select(RackModel.id).where(RackModel.manufacturer == manufacturer_name, RackModel.model_name == model_name)
        )
    ).scalar_one()


async def _find_or_create_equipment_model(db: AsyncSession, *, manufacturer_name: str, model_name: str) -> uuid.UUID:
    inserted = (
        await db.execute(
            pg_insert(EquipmentModel)
            .values(id=uuid.uuid4(), manufacturer=manufacturer_name, model_name=model_name)
            .on_conflict_do_nothing(constraint="uq_equipment_model_manufacturer_model_name")
            .returning(EquipmentModel.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    return (
        await db.execute(
            select(EquipmentModel.id).where(
                EquipmentModel.manufacturer == manufacturer_name, EquipmentModel.model_name == model_name
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------- Publish


async def publish_revision(
    db: AsyncSession, *, revision_id: uuid.UUID, user_id: uuid.UUID
) -> CatalogModelRevision:
    """The one-transaction publish sequence (spec §4.7/§5.3):

    1. Lock the revision row (`SELECT ... FOR UPDATE`) *before* validation — the same
       parent-row-locking discipline PR-1's child-write trigger uses, so a concurrent
       publish of this same revision blocks here rather than racing past this check.
    2. Validate (raises ValidationFailed, caught by the router, on any error — nothing
       has been written yet, so the caller's rollback is a no-op cleanup).
    3. category='rack'|'equipment' only (defensive re-check; the API layer already
       refuses any other category at model-creation time, §4.1).
    4. Find-or-create the legacy `RackModel`/`EquipmentModel` row.
    5. INSERT the new legacy `RackModelRevision`/`EquipmentModelRevision` row, physical
       columns unit-converted.
    6. Set the reverse bridge (`bridged_from_catalog_revision_id`) on that new legacy row.
    7. Set the forward bridge, `lifecycle_status='published'`, and publication metadata on
       `revision` in one flush — the single UPDATE PR-1's
       `fn_guard_catalog_model_revision_lifecycle()` accepts for `draft -> published`.

    Raises NotFoundError/ConflictError/ValidationFailed; never commits — the caller
    (the route handler) commits after also writing audit/outbox, and rolls back the
    entire sequence, including the legacy INSERT, on any exception."""
    revision = (
        await db.execute(
            select(CatalogModelRevision).where(CatalogModelRevision.id == revision_id).with_for_update()
        )
    ).scalar_one_or_none()
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    if revision.lifecycle_status != "draft":
        raise ConflictError(
            detail=f"catalog_model_revision {revision_id} is not a draft "
            f"(status={revision.lifecycle_status}) and cannot be published."
        )

    model = await db.get(CatalogModel, revision.catalog_model_id)
    assert model is not None
    manufacturer = await db.get(Manufacturer, model.manufacturer_id)
    assert manufacturer is not None

    ports = list(
        (
            await db.execute(select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == revision_id))
        ).scalars()
    )
    psus = list(
        (
            await db.execute(select(PowerSupplyTemplate).where(PowerSupplyTemplate.catalog_model_revision_id == revision_id))
        ).scalars()
    )
    metrics = list(
        (
            await db.execute(
                select(MonitoringMetricTemplate).where(MonitoringMetricTemplate.catalog_model_revision_id == revision_id)
            )
        ).scalars()
    )

    summary = validate_revision_for_publish(revision, model.category, ports, psus, metrics)
    if not summary.valid:
        raise ValidationFailed(summary)

    if model.category not in ("rack", "equipment"):
        raise ConflictError(detail=f"category {model.category!r} has no legacy bridge in this phase.")

    rack_unit_height, width_mm, depth_mm, weight_kg = _convert_physical(revision)
    legacy_revision: RackModelRevision | EquipmentModelRevision
    if model.category == "rack":
        rack_model_id = await _find_or_create_rack_model(
            db, manufacturer_name=manufacturer.name, model_name=model.model_name
        )
        assert rack_unit_height is not None and width_mm is not None and depth_mm is not None
        legacy_revision = RackModelRevision(
            rack_model_id=rack_model_id, height_u=rack_unit_height, width_mm=width_mm, depth_mm=depth_mm,
            weight_capacity_kg=weight_kg,
        )
        db.add(legacy_revision)
        await db.flush()
        legacy_revision.bridged_from_catalog_revision_id = revision.id
        revision.legacy_rack_model_revision_id = legacy_revision.id
    else:
        equipment_model_id = await _find_or_create_equipment_model(
            db, manufacturer_name=manufacturer.name, model_name=model.model_name
        )
        legacy_revision = EquipmentModelRevision(
            equipment_model_id=equipment_model_id, height_u=rack_unit_height, width_mm=width_mm, depth_mm=depth_mm,
            weight_kg=weight_kg,
        )
        db.add(legacy_revision)
        await db.flush()
        legacy_revision.bridged_from_catalog_revision_id = revision.id
        revision.legacy_equipment_model_revision_id = legacy_revision.id

    revision.lifecycle_status = "published"
    revision.published_at = datetime.now(UTC)
    revision.published_by_user_id = user_id
    await db.flush()
    return revision


# --------------------------------------------------------------------------- Clone


async def clone_revision(db: AsyncSession, *, source_revision_id: uuid.UUID, user_id: uuid.UUID) -> CatalogModelRevision:
    """Deep-copies every scalar field and every child row (ports/PSUs/monitoring keep
    their exact `stable_key`) from a published/retired revision into a brand-new draft,
    per spec §5.5.

    Graphics/markers are deliberately NOT cloned here: PR-3 implements no upload endpoint
    (that is PR-5), so no revision can have a `catalog_graphic` row yet — every clone
    today therefore has zero graphics to copy, correctly. This function still queries for
    them and refuses explicitly (rather than silently dropping them) if any are ever
    found, so a future PR-5 that adds the upload endpoint before also extending clone
    cannot silently ship a clone that loses images — extending this exact function, right
    after the child-template copy and before the final flush, is the only change PR-5's
    own clone support needs to make."""
    source = await db.get(CatalogModelRevision, source_revision_id)
    if source is None:
        raise NotFoundError(f"CatalogModelRevision {source_revision_id} not found.")
    if source.lifecycle_status not in ("published", "retired"):
        raise ConflictError(detail="Only a published or retired revision may be cloned.")

    max_revision_number = (
        await db.execute(
            select(func.max(CatalogModelRevision.revision_number)).where(
                CatalogModelRevision.catalog_model_id == source.catalog_model_id
            )
        )
    ).scalar_one()

    clone = CatalogModelRevision(
        catalog_model_id=source.catalog_model_id,
        revision_number=max_revision_number + 1,
        lifecycle_status="draft",
        dimension_unit=source.dimension_unit,
        width_value=source.width_value,
        height_value=source.height_value,
        depth_value=source.depth_value,
        rack_unit_height=source.rack_unit_height,
        weight_unit=source.weight_unit,
        weight_value=source.weight_value,
        mounting_orientation=source.mounting_orientation,
        supported_placement_types=source.supported_placement_types,
        airflow_direction=source.airflow_direction,
        rated_power_w=source.rated_power_w,
        typical_power_w=source.typical_power_w,
        max_power_w=source.max_power_w,
        heat_dissipation_btu_hr=source.heat_dissipation_btu_hr,
        power_redundancy_mode=source.power_redundancy_mode,
        cloned_from_revision_id=source.id,
        created_by_user_id=user_id,
    )
    db.add(clone)
    await db.flush()

    ports = (
        await db.execute(select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == source.id))
    ).scalars()
    for port in ports:
        db.add(
            NetworkPortTemplate(
                catalog_model_revision_id=clone.id, stable_key=port.stable_key, display_name=port.display_name,
                numbering_pattern=port.numbering_pattern, media_type=port.media_type,
                supported_speeds_mbps=port.supported_speeds_mbps, connector_type=port.connector_type,
                role=port.role, side=port.side, module_group=port.module_group, sort_order=port.sort_order,
            )
        )

    psus = (
        await db.execute(select(PowerSupplyTemplate).where(PowerSupplyTemplate.catalog_model_revision_id == source.id))
    ).scalars()
    for psu in psus:
        db.add(
            PowerSupplyTemplate(
                catalog_model_revision_id=clone.id, stable_key=psu.stable_key, label=psu.label, quantity=psu.quantity,
                redundancy_mode=psu.redundancy_mode, connector_type=psu.connector_type,
                rated_voltage_min=psu.rated_voltage_min, rated_voltage_max=psu.rated_voltage_max,
                rated_frequency_hz=psu.rated_frequency_hz, rated_current_a=psu.rated_current_a,
                hot_swappable=psu.hot_swappable, sort_order=psu.sort_order,
            )
        )

    metrics = (
        await db.execute(
            select(MonitoringMetricTemplate).where(MonitoringMetricTemplate.catalog_model_revision_id == source.id)
        )
    ).scalars()
    for metric in metrics:
        db.add(
            MonitoringMetricTemplate(
                catalog_model_revision_id=clone.id, stable_key=metric.stable_key, protocol=metric.protocol,
                protocol_other_label=metric.protocol_other_label, metric_name=metric.metric_name, oid=metric.oid,
                value_type=metric.value_type, unit=metric.unit, scale=metric.scale, transform=metric.transform,
                offset=metric.offset, default_collection_interval_seconds=metric.default_collection_interval_seconds,
                default_warning_threshold=metric.default_warning_threshold,
                default_critical_threshold=metric.default_critical_threshold, sort_order=metric.sort_order,
            )
        )

    has_graphics = (
        await db.execute(select(CatalogGraphic.id).where(CatalogGraphic.catalog_model_revision_id == source.id).limit(1))
    ).scalar_one_or_none()
    if has_graphics is not None:
        raise ConflictError(detail="Cloning a revision with graphics is not yet supported (implemented in PR-5).")

    await db.flush()
    return clone
