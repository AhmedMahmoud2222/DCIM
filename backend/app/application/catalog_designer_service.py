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
- `allocate_revision_number()` locks the parent `catalog_model` row with `SELECT ... FOR
  UPDATE` before computing `max(revision_number) + 1`, so two concurrent
  create-draft/clone requests under the *same* model can never compute the same number —
  each queues behind the lock and gets a distinct one. A request for a *different* model
  is entirely unaffected (a different row, no shared lock). The app-wide `IntegrityError`
  handler (app/core/errors.py) still stands as a defense-in-depth safety net turning any
  residual constraint violation into a clean 409 rather than a raw 500, but is no longer
  the primary mechanism.
- `lock_draft_revision_for_edit()` is the single atomicity primitive behind both the
  revision's own scalar `PATCH` and every child template create/edit/delete: it locks the
  revision row with `SELECT ... FOR UPDATE` *before* comparing `If-Match` against the
  current version. A plain load-then-compare is not atomic under two simultaneous
  requests — both could read the same pre-mutation version, both pass the check, and the
  second would silently overwrite the first's already-committed change, since its ORM
  object never re-reads the row before computing its own new version. The lock forces the
  second request to block until the first's transaction ends, then re-read the fresh,
  post-commit version and correctly detect the conflict. Child template rows carry no
  version column of their own (spec §4.3/§4.4/§4.6) — the request contract for every
  child mutation is `If-Match` against the *parent revision's* version, which this
  function also increments by one, atomically, as part of the same lock/check.
- Two concurrent publishes of the *same* revision, or a publish racing a concurrent draft/
  child edit, serialize through the same mechanism: `publish_revision`'s own `SELECT ...
  FOR UPDATE` below locks the identical `catalog_model_revision` row
  `lock_draft_revision_for_edit()` locks, so whichever transaction gets there first is
  the one the other waits on and then correctly observes (a) published, if publish won —
  the edit's own draft check rejects it, or (b) still draft with a bumped version, if the
  edit won — publish re-validates against the edited content, never a corrupted partial
  publish or a silently lost edit.
- The legacy-model find-or-create (`_find_or_create_rack_model`/`_find_or_create_
  equipment_model`) uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id` followed by a
  fallback `SELECT`, the same race-safe upsert pattern already used by
  `telemetry_service.py`'s dedup insert — never a bare `SELECT` then unconditional
  `INSERT`, which would raise on a genuine concurrent conflict instead of finding the
  now-existing row.
"""

import hashlib
import io
import uuid
from datetime import UTC, datetime

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.concurrency import check_version_match
from app.core.errors import ConflictError, NotFoundError
from app.domain.catalog.designer_models import (
    SIDES,
    CatalogGraphic,
    CatalogGraphicMarker,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    MonitoringMetricTemplate,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.catalog.document_models import CatalogRevisionDocument
from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision
from app.infrastructure.storage import StorageBackend

_THUMBNAIL_MAX_DIMENSION_PX = 320

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
    graphics: list[CatalogGraphic] | None = None,
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

    # Phase 10A PR-5 architectural directive: missing graphics/markers are advisory,
    # never publish-blocking — a revision publishable today under PR-3/PR-4 must stay
    # publishable after PR-5 ships, so these are warnings only, and never flip `valid`.
    graphics = graphics or []
    sides_present = {g.side for g in graphics}
    for side in ("front", "rear"):
        if side not in sides_present:
            warnings.append(
                ValidationIssue(
                    field=f"graphics.{side}", code="graphic_missing", message=f"No {side} image has been uploaded."
                )
            )
    for graphic in graphics:
        if not graphic.markers:
            warnings.append(
                ValidationIssue(
                    field=f"graphics.{graphic.side}", code="graphic_has_no_markers",
                    message=f"The {graphic.side} image has no markers placed on it.",
                )
            )

    return ValidationSummary(valid=not errors, errors=errors, warnings=warnings)


class ValidationFailed(Exception):
    """Raised by publish_revision when validate_revision_for_publish finds errors — the
    router catches this and returns the summary as the 422 body verbatim (spec §5.2:
    publish "re-runs this exact check server-side and rejects with 422, the same error
    shape"), never FastAPI's generic RequestValidationError envelope."""

    def __init__(self, summary: ValidationSummary):
        self.summary = summary
        super().__init__("Revision failed publish validation.")


# --------------------------------------------------------------------------- Concurrency primitives


async def lock_draft_revision_for_edit(
    db: AsyncSession, *, revision_id: uuid.UUID, if_match_version: int
) -> CatalogModelRevision:
    """The single atomicity primitive behind the revision's own scalar `PATCH` and every
    network-port/power-supply/monitoring-metric create/edit/delete. Request contract for
    every one of these mutations: `If-Match` carries the *parent revision's* current
    `version` (child rows have no version column of their own, spec §4.3/§4.4/§4.6) —
    identical to how the revision's own PATCH already used `If-Match` (spec §5.1), just
    extended to cover child writes too, since an admin editing a port and an admin editing
    a PSU on the same draft are exactly the same race as two admins editing the same
    revision's scalar fields.

    `SELECT ... FOR UPDATE` locks the row *before* comparing versions — necessary for
    atomicity under two simultaneous requests: a plain load-then-compare lets both read
    the same pre-mutation version, both pass the check, and the second silently overwrite
    the first (its ORM object never re-reads the row before computing its own new
    version). The lock forces the second request to block until the first's transaction
    ends, then re-read the fresh, post-commit version and correctly detect the conflict.

    Raises NotFoundError / ConflictError (wrong version, or not a draft — mirrors
    `app/api/v1/catalog_designer.py`'s prior `_require_draft` message exactly, so callers
    changing over to this function are not a behavior change on that path). Increments
    `.version` by one and returns the locked, still-open-transaction revision; the caller
    performs its own specific mutation (revision scalar fields, or a child row's add/
    edit/delete) and flushes once — both changes land in the same statement batch."""
    revision = await db.get(CatalogModelRevision, revision_id, with_for_update=True)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    if revision.lifecycle_status != "draft":
        raise ConflictError(detail="This revision is published and immutable.")
    check_version_match(expected=if_match_version, actual=revision.version)
    revision.version += 1
    return revision


async def allocate_revision_number(db: AsyncSession, *, catalog_model_id: uuid.UUID) -> int:
    """Serializes `revision_number` allocation per `catalog_model_id` via a row lock on
    the parent `CatalogModel` — spec §5.1/§5.5's `revision_number = max(existing) + 1`
    computed and effectively reserved atomically, so two concurrent create-draft/clone
    requests under the same model can never compute the same number and collide on
    `catalog_model_revision`'s own `UNIQUE(catalog_model_id, revision_number)` constraint.
    A concurrent request for a *different* model is entirely unaffected — a different
    row, no shared lock. Callers still insert the new revision row themselves; this
    function only reserves the number by holding the model-row lock across both the read
    and the caller's subsequent insert, within the same transaction."""
    locked = (
        await db.execute(select(CatalogModel.id).where(CatalogModel.id == catalog_model_id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError(f"CatalogModel {catalog_model_id} not found.")
    max_number = (
        await db.execute(
            select(func.max(CatalogModelRevision.revision_number)).where(
                CatalogModelRevision.catalog_model_id == catalog_model_id
            )
        )
    ).scalar_one()
    return (max_number or 0) + 1


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
    graphics = list(
        (
            await db.execute(
                select(CatalogGraphic)
                .options(selectinload(CatalogGraphic.markers))
                .where(CatalogGraphic.catalog_model_revision_id == revision_id)
            )
        ).scalars()
    )

    summary = validate_revision_for_publish(revision, model.category, ports, psus, metrics, graphics)
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


# --------------------------------------------------------------------------- Graphics


class GraphicRejected(Exception):
    """Raised for untrusted-input problems (bad content, wrong side, too large) — the
    router maps this to a 422, mirroring how app/application/svg_sanitizer.py's
    SvgRejected is handled for floor-plan uploads."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _sniff_raster_mime_type(content: bytes) -> str | None:
    """Content-sniffed (magic bytes), never filename/declared-type — the same discipline
    app/api/v1/floor_plans.py already applies to its own raster uploads."""
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def thumbnail_storage_key(storage_key: str) -> str:
    """Deliberately derived from `storage_key` rather than stored as its own column —
    `catalog_graphic` (migration `0019_catalog_graphics`) has no `thumbnail_storage_key`
    field, and a suffix scheme needs no schema change to add one."""
    return f"{storage_key}.thumb.jpg"


async def upload_catalog_graphic(
    db: AsyncSession,
    *,
    revision: CatalogModelRevision,
    side: str,
    content: bytes,
    original_filename: str,
    uploaded_by_user_id: uuid.UUID,
    storage: StorageBackend,
    max_upload_bytes: int,
) -> CatalogGraphic:
    """Content-sniff + size cap (mirrors svg_sanitizer.py's raster path), a
    Pillow-verified re-read of the dimensions (defense in depth beyond the magic-byte
    check — a file with valid magic bytes but a corrupt body is rejected here instead of
    being written to storage with `width_px`/`height_px` this function never actually
    measured), a synchronous thumbnail (PR-5 architectural directive: sync processing —
    bounded, small images at this 10MB cap don't need the async Celery-job machinery
    app/infrastructure/tasks/floorplan_import.py uses for potentially large SVG parsing),
    and content-addressed storage (sha256 of the original bytes is the storage key, so
    re-uploading identical bytes never writes a duplicate object, and a clone of this
    revision — see clone_revision below — can safely reuse the same key rather than
    duplicating bytes on disk).

    Replaces any existing graphic for the same (revision, side): the unique index
    (migration 0019) allows only one row per side, and a new image invalidates the old
    one's markers by construction — marker coordinates only make sense relative to a
    specific image — so replacing it cascades to deleting them
    (`catalog_graphic_marker.catalog_graphic_id` is `ON DELETE CASCADE`).

    Raises GraphicRejected for untrusted-input problems; NotFoundError/ConflictError are
    the caller's responsibility (loading/locking the revision happens before this is
    called, via lock_draft_revision_for_edit, exactly like every other child mutation)."""
    if side not in SIDES:
        raise GraphicRejected(f"side must be one of {SIDES}")
    if len(content) > max_upload_bytes:
        raise GraphicRejected(f"file exceeds the {max_upload_bytes} byte size limit")
    mime_type = _sniff_raster_mime_type(content)
    if mime_type is None:
        raise GraphicRejected("file content is not recognized as PNG or JPEG (checked by content, not filename)")

    try:
        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(content)) as img:
            width_px, height_px = img.size
            thumbnail = img.convert("RGB")
            thumbnail.thumbnail((_THUMBNAIL_MAX_DIMENSION_PX, _THUMBNAIL_MAX_DIMENSION_PX))
            thumbnail_buffer = io.BytesIO()
            thumbnail.save(thumbnail_buffer, format="JPEG", quality=80)
            thumbnail_bytes = thumbnail_buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise GraphicRejected(f"unparseable image: {exc}") from exc

    file_hash = hashlib.sha256(content).hexdigest()
    extension = "png" if mime_type == "image/png" else "jpg"
    storage_key = f"{file_hash}.{extension}"
    storage.save(storage_key, content)
    storage.save(thumbnail_storage_key(storage_key), thumbnail_bytes)

    existing = (
        await db.execute(
            select(CatalogGraphic).where(
                CatalogGraphic.catalog_model_revision_id == revision.id, CatalogGraphic.side == side
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.flush()  # the DELETE must land before the new row's INSERT re-uses the (revision_id, side) unique key

    graphic = CatalogGraphic(
        catalog_model_revision_id=revision.id, side=side, storage_key=storage_key,
        original_filename=original_filename[:255], mime_type=mime_type, file_size_bytes=len(content),
        width_px=width_px, height_px=height_px, uploaded_by_user_id=uploaded_by_user_id,
        uploaded_at=datetime.now(UTC),
    )
    db.add(graphic)
    await db.flush()
    return graphic


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

    revision_number = await allocate_revision_number(db, catalog_model_id=source.catalog_model_id)

    clone = CatalogModelRevision(
        catalog_model_id=source.catalog_model_id,
        revision_number=revision_number,
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

    ports = list(
        (
            await db.execute(select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == source.id))
        ).scalars()
    )
    for port in ports:
        db.add(
            NetworkPortTemplate(
                catalog_model_revision_id=clone.id, stable_key=port.stable_key, display_name=port.display_name,
                numbering_pattern=port.numbering_pattern, media_type=port.media_type,
                supported_speeds_mbps=port.supported_speeds_mbps, connector_type=port.connector_type,
                role=port.role, side=port.side, module_group=port.module_group, sort_order=port.sort_order,
            )
        )

    psus = list(
        (
            await db.execute(select(PowerSupplyTemplate).where(PowerSupplyTemplate.catalog_model_revision_id == source.id))
        ).scalars()
    )
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

    # Phase 10A PR-5: extending the clone right after the child-template copy and before
    # the final flush, exactly as this function's own docstring anticipated. Storage
    # objects are content-addressed and immutable (app/infrastructure/storage/), so the
    # clone's CatalogGraphic row reuses the source's `storage_key` unchanged — no bytes
    # are duplicated on disk. Markers are re-pointed at the *clone's* own port/PSU rows
    # (matched by `stable_key`, which clone preserves exactly, per the loops above),
    # never the source's: `fn_validate_catalog_graphic_marker` (migration
    # `0019_catalog_graphics`) rejects a marker whose port/PSU belongs to a different
    # revision than its graphic, so re-pointing is not an optional nicety — an unmapped
    # marker insert would be rejected by the database outright.
    graphics = list(
        (await db.execute(select(CatalogGraphic).where(CatalogGraphic.catalog_model_revision_id == source.id))).scalars()
    )
    if graphics:
        await db.flush()  # populate clone's port/PSU ids before building the id maps below
        new_port_id_by_stable_key = {
            p.stable_key: p.id
            for p in (
                await db.execute(select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == clone.id))
            ).scalars()
        }
        new_psu_id_by_stable_key = {
            p.stable_key: p.id
            for p in (
                await db.execute(select(PowerSupplyTemplate).where(PowerSupplyTemplate.catalog_model_revision_id == clone.id))
            ).scalars()
        }
        old_port_stable_key_by_id = {p.id: p.stable_key for p in ports}
        old_psu_stable_key_by_id = {p.id: p.stable_key for p in psus}

        for graphic in graphics:
            markers = list(
                (
                    await db.execute(select(CatalogGraphicMarker).where(CatalogGraphicMarker.catalog_graphic_id == graphic.id))
                ).scalars()
            )
            new_graphic = CatalogGraphic(
                catalog_model_revision_id=clone.id, side=graphic.side, storage_key=graphic.storage_key,
                original_filename=graphic.original_filename, mime_type=graphic.mime_type,
                file_size_bytes=graphic.file_size_bytes, width_px=graphic.width_px, height_px=graphic.height_px,
                uploaded_by_user_id=user_id, uploaded_at=datetime.now(UTC),
            )
            db.add(new_graphic)
            await db.flush()  # need new_graphic.id for the markers' FK below

            for marker in markers:
                new_port_id = (
                    new_port_id_by_stable_key[old_port_stable_key_by_id[marker.network_port_template_id]]
                    if marker.network_port_template_id is not None
                    else None
                )
                new_psu_id = (
                    new_psu_id_by_stable_key[old_psu_stable_key_by_id[marker.power_supply_template_id]]
                    if marker.power_supply_template_id is not None
                    else None
                )
                db.add(
                    CatalogGraphicMarker(
                        catalog_graphic_id=new_graphic.id, marker_type=marker.marker_type,
                        network_port_template_id=new_port_id, power_supply_template_id=new_psu_id,
                        label=marker.label, marker_x=marker.marker_x, marker_y=marker.marker_y,
                        sort_order=marker.sort_order,
                    )
                )

    # DCIM01 PDF datasheet import: the clone links the same document rows (files are
    # content-addressed and immutable, so nothing is copied). Each link records the cloning
    # user; the source revision's links are untouched.
    source_links = list(
        (
            await db.execute(
                select(CatalogRevisionDocument).where(CatalogRevisionDocument.catalog_model_revision_id == source.id)
            )
        ).scalars()
    )
    for link in source_links:
        db.add(
            CatalogRevisionDocument(
                catalog_model_revision_id=clone.id, catalog_document_id=link.catalog_document_id,
                attached_by_user_id=user_id, attached_at=datetime.now(UTC),
            )
        )

    await db.flush()
    return clone
