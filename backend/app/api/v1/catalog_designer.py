"""Phase 10A PR-3: the typed catalog designer API — manufacturer, model, and revision-
lifecycle endpoints only (docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-
designer-design.md §10, aligned plan §3.3). Graphics/markers (PR-5), import/export
(PR-6), and migration/impact-preview (PR-7) are additions to this same router file in
later PRs — none of them exist here yet.

Every `POST`/`PATCH`/`DELETE` handler depends on
`require_catalog_administrator("catalog:...")` — permission and Administrator role
membership, not permission alone (spec §9.1). Every `GET` handler depends on plain
`require_permission("catalog:...")`, except `GET /catalog/revisions/{id}`, which applies
the conditional check spec §10 describes: `catalog:read_draft` only when the loaded
revision is currently a draft, `catalog:read` otherwise — enforced after the row is
loaded, since which permission applies depends on data only the database has.

Immutability is defense in depth, matching spec §5.4: every mutating handler on a
revision or its child rows checks `lifecycle_status == 'draft'` here, in the application
layer, before ever touching the database — the identical check PR-1's
`fn_reject_write_on_non_draft_revision()`/`fn_guard_catalog_model_revision_lifecycle()`
triggers re-enforce at the database layer regardless of whether this check is bypassed."""

import uuid
from datetime import UTC, datetime
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.api.v1.bulk_import import BulkImportJobOut, dispatch_parse_job_or_fail
from app.application.audit_service import write_audit_log
from app.application.bulk_import.service import create_job as create_bulk_import_job
from app.application.bulk_import.templates import build_catalog_template
from app.application.bulk_import.upload import validate_mode, validate_upload_bytes
from app.application.catalog_designer_service import (
    GraphicRejected,
    ValidationFailed,
    ValidationSummary,
    allocate_revision_number,
    clone_revision,
    lock_draft_revision_for_edit,
    publish_revision,
    thumbnail_storage_key,
    upload_catalog_graphic,
    validate_revision_for_publish,
)
from app.application.concurrency import require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.rbac import AuthContext, get_auth_context, require_catalog_administrator, require_permission
from app.core.config import get_settings
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.catalog.designer_models import (
    CATEGORIES,
    MARKER_TYPES,
    CatalogGraphic,
    CatalogGraphicMarker,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    MonitoringMetricTemplate,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.infrastructure.storage import get_storage_backend

router = APIRouter(prefix="/catalog", tags=["catalog-designer"])

_BRIDGED_CATEGORIES = ("rack", "equipment")


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# --------------------------------------------------------------------- Bulk import
# Registered before "/manufacturers/{manufacturer_id}" etc. is unnecessary here (these
# are new, distinct literal top-level segments — "import-template"/"import-jobs" — under
# /catalog, so there is no existing "/{param}" route at this level for them to collide
# with), but kept near the top of the router, close to the other module-level setup,
# for discoverability.


@router.get("/import-template")
async def download_catalog_import_template(ctx: AuthContext = Depends(require_permission("catalog:read"))) -> StreamingResponse:
    content = build_catalog_template()
    return StreamingResponse(
        BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="catalog-import-template.xlsx"'},
    )


@router.post("/import-jobs", response_model=BulkImportJobOut, status_code=202)
async def upload_catalog_import_job(
    request: Request,
    mode: str = Query(...),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:import")),
) -> BulkImportJobOut:
    validate_mode(mode)
    content = await file.read()
    file_hash = validate_upload_bytes(content)

    storage = get_storage_backend()
    storage_key = f"{file_hash}.xlsx"
    storage.save(storage_key, content)

    job = await create_bulk_import_job(
        db, import_type="catalog", mode=mode, uploaded_by_user_id=ctx.user.id,
        original_filename=(file.filename or "upload.xlsx")[:255], file_hash=file_hash, file_size_bytes=len(content),
        storage_key=storage_key,
    )

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.bulk_import.upload", entity_type="bulk_import_job", entity_id=job.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"filename": job.original_filename, "file_hash": file_hash, "size_bytes": len(content), "mode": mode},
    )
    await db.commit()
    await db.refresh(job)

    await dispatch_parse_job_or_fail(db, job)

    return BulkImportJobOut.from_job(job)


# ============================================================================ Manufacturer


class ManufacturerIn(BaseModel):
    name: str = Field(max_length=128)


class ManufacturerMetadataIn(BaseModel):
    status: str | None = None


class ManufacturerOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/manufacturers", response_model=ManufacturerOut, status_code=201)
async def create_manufacturer(
    body: ManufacturerIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> Manufacturer:
    manufacturer = Manufacturer(name=body.name)
    db.add(manufacturer)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.manufacturer.create", entity_type="manufacturer",
        entity_id=manufacturer.id, request_id=request_id, correlation_id=correlation_id, after={"name": manufacturer.name},
    )
    await write_outbox_event(
        db, event_type="ManufacturerCreated", aggregate_type="manufacturer", aggregate_id=manufacturer.id,
        payload={"name": manufacturer.name}, correlation_id=correlation_id,
    )
    await db.commit()
    return manufacturer


@router.get("/manufacturers", response_model=Page[ManufacturerOut])
async def list_manufacturers(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    q: str | None = None,
    ctx: AuthContext = Depends(require_permission("catalog:read")),
) -> Page:
    stmt = select(Manufacturer)
    count_stmt = select(func.count()).select_from(Manufacturer)
    if q:
        stmt = stmt.where(Manufacturer.name.ilike(f"%{q}%"))
        count_stmt = count_stmt.where(Manufacturer.name.ilike(f"%{q}%"))
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Manufacturer.name).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/manufacturers/{manufacturer_id}", response_model=ManufacturerOut)
async def get_manufacturer(
    manufacturer_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("catalog:read"))
) -> Manufacturer:
    manufacturer = await db.get(Manufacturer, manufacturer_id)
    if manufacturer is None:
        raise NotFoundError(f"Manufacturer {manufacturer_id} not found.")
    return manufacturer


# ============================================================================ CatalogModel


class CatalogModelIn(BaseModel):
    manufacturer_id: uuid.UUID
    category: str
    subtype: str | None = Field(default=None, max_length=64)
    model_name: str = Field(max_length=128)
    model_number: str | None = Field(default=None, max_length=128)
    description: str | None = None
    tags: list[str] = []


class CatalogModelMetadataIn(BaseModel):
    """PATCH /catalog/models/{id} — mutable model metadata only (spec §4.1/§5.4 resolved
    decision, PR-2): description/tags/status. manufacturer_id/category/model_name/
    model_number are never accepted here — they are identity fields, locked by PR-1's
    `fn_reject_catalog_model_identity_change()` once any revision has published, and this
    endpoint never even attempts to touch them."""

    description: str | None = None
    tags: list[str] | None = None
    status: str | None = None


class CatalogModelOut(BaseModel):
    id: uuid.UUID
    manufacturer_id: uuid.UUID
    category: str
    subtype: str | None
    model_name: str
    model_number: str | None
    description: str | None
    tags: list
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RevisionSummaryOut(BaseModel):
    id: uuid.UUID
    revision_number: int
    lifecycle_status: str
    published_at: datetime | None
    retired_at: datetime | None

    model_config = {"from_attributes": True}


class CatalogModelDetailOut(CatalogModelOut):
    revisions: list[RevisionSummaryOut]


@router.post("/models", response_model=CatalogModelOut, status_code=201)
async def create_catalog_model(
    body: CatalogModelIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogModel:
    if body.category not in _BRIDGED_CATEGORIES:
        raise ApiError(
            status_code=422, title="Unsupported Category",
            detail=(
                f"category {body.category!r} is not yet supported by the designer/lifecycle/legacy-bridge "
                f"workflow (spec §4.1) — only {_BRIDGED_CATEGORIES} in this phase."
            ),
        )
    if body.category not in CATEGORIES:
        raise ApiError(status_code=422, title="Invalid Category", detail=f"category must be one of {CATEGORIES}.")
    if await db.get(Manufacturer, body.manufacturer_id) is None:
        raise NotFoundError(f"Manufacturer {body.manufacturer_id} not found.")

    model = CatalogModel(
        manufacturer_id=body.manufacturer_id, category=body.category, subtype=body.subtype, model_name=body.model_name,
        model_number=body.model_number, description=body.description, tags=body.tags,
    )
    db.add(model)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.model.create", entity_type="catalog_model", entity_id=model.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"manufacturer_id": str(model.manufacturer_id), "category": model.category, "model_name": model.model_name},
    )
    await write_outbox_event(
        db, event_type="CatalogModelCreated", aggregate_type="catalog_model", aggregate_id=model.id,
        payload={"category": model.category, "model_name": model.model_name}, correlation_id=correlation_id,
    )
    await db.commit()
    return model


@router.get("/models", response_model=Page[CatalogModelOut])
async def list_catalog_models(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    category: str | None = None,
    status: str | None = None,
    manufacturer_id: uuid.UUID | None = None,
    q: str | None = None,
    ctx: AuthContext = Depends(require_permission("catalog:read")),
) -> Page:
    """Draft-read audit note: `CatalogModelOut` carries no revision-derived field at all
    (no revision count, no "latest status" summary), so there is nothing here for a
    `catalog:read`-only caller to leak — unlike `GET /models/{id}` (below), which does
    embed a revision list and is filtered accordingly. If a future PR adds any
    revision-derived field to this response, it must be filtered the same way."""
    stmt = select(CatalogModel)
    count_stmt = select(func.count()).select_from(CatalogModel)
    filters = []
    if category is not None:
        filters.append(CatalogModel.category == category)
    if status is not None:
        filters.append(CatalogModel.status == status)
    if manufacturer_id is not None:
        filters.append(CatalogModel.manufacturer_id == manufacturer_id)
    if q:
        filters.append(or_(CatalogModel.model_name.ilike(f"%{q}%"), CatalogModel.model_number.ilike(f"%{q}%")))
    for condition in filters:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(CatalogModel.model_name).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/models/{model_id}", response_model=CatalogModelDetailOut)
async def get_catalog_model(
    model_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(require_permission("catalog:read"))
) -> CatalogModelDetailOut:
    """The revision summary list is filtered for a caller who lacks `catalog:read_draft`
    (spec §10: drafts are working material, not general-inventory-readable) — a plain
    `catalog:read` holder must not learn a draft revision exists at all here, matching
    `GET /catalog/revisions/{id}`'s own conditional gate below. Published/retired
    revisions remain listed for every `catalog:read` holder, unchanged."""
    model = await db.get(CatalogModel, model_id)
    if model is None:
        raise NotFoundError(f"CatalogModel {model_id} not found.")
    stmt = select(CatalogModelRevision).where(CatalogModelRevision.catalog_model_id == model_id)
    if not ctx.has_permission("catalog:read_draft"):
        stmt = stmt.where(CatalogModelRevision.lifecycle_status != "draft")
    revisions = (await db.execute(stmt.order_by(CatalogModelRevision.revision_number))).scalars().all()
    return CatalogModelDetailOut(
        **CatalogModelOut.model_validate(model).model_dump(),
        revisions=[RevisionSummaryOut.model_validate(r) for r in revisions],
    )


@router.patch("/models/{model_id}", response_model=CatalogModelOut)
async def update_catalog_model_metadata(
    model_id: uuid.UUID,
    body: CatalogModelMetadataIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogModel:
    """Spec §4.1/§5.4 resolved decision (PR-2), implemented here (PR-3): description/tags/
    status are mutable model metadata, editable at any time regardless of publication
    state — no `lifecycle_status`/If-Match check of any kind, since no database trigger
    locks these columns and no revision-level concurrency applies to a `CatalogModel` row."""
    model = await db.get(CatalogModel, model_id)
    if model is None:
        raise NotFoundError(f"CatalogModel {model_id} not found.")

    before = {"description": model.description, "tags": model.tags, "status": model.status}
    if body.description is not None:
        model.description = body.description
    if body.tags is not None:
        model.tags = body.tags
    if body.status is not None:
        model.status = body.status
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.model.update_metadata", entity_type="catalog_model",
        entity_id=model.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after={"description": model.description, "tags": model.tags, "status": model.status},
    )
    await write_outbox_event(
        db, event_type="CatalogModelMetadataUpdated", aggregate_type="catalog_model", aggregate_id=model.id,
        payload={"status": model.status}, correlation_id=correlation_id,
    )
    await db.commit()
    return model


# ============================================================================ CatalogModelRevision


class CatalogModelRevisionUpdateIn(BaseModel):
    dimension_unit: str | None = None
    width_value: float | None = None
    height_value: float | None = None
    depth_value: float | None = None
    rack_unit_height: int | None = None
    weight_unit: str | None = None
    weight_value: float | None = None
    mounting_orientation: str | None = None
    supported_placement_types: list[str] | None = None
    airflow_direction: str | None = None
    rated_power_w: float | None = None
    typical_power_w: float | None = None
    max_power_w: float | None = None
    heat_dissipation_btu_hr: float | None = None
    power_redundancy_mode: str | None = None


_REVISION_SCALAR_FIELDS = tuple(CatalogModelRevisionUpdateIn.model_fields.keys())


class CatalogModelRevisionOut(BaseModel):
    """Never carries `legacy_rack_model_revision_id`/`legacy_equipment_model_revision_id`
    — spec §4.7: "The legacy id is never exposed to the new admin-facing API or UI"."""

    id: uuid.UUID
    catalog_model_id: uuid.UUID
    revision_number: int
    lifecycle_status: str
    dimension_unit: str | None
    width_value: float | None
    height_value: float | None
    depth_value: float | None
    rack_unit_height: int | None
    weight_unit: str | None
    weight_value: float | None
    mounting_orientation: str | None
    supported_placement_types: list | None
    airflow_direction: str | None
    rated_power_w: float | None
    typical_power_w: float | None
    max_power_w: float | None
    heat_dissipation_btu_hr: float | None
    power_redundancy_mode: str | None
    cloned_from_revision_id: uuid.UUID | None
    created_by_user_id: uuid.UUID
    published_at: datetime | None
    published_by_user_id: uuid.UUID | None
    retired_at: datetime | None
    retired_by_user_id: uuid.UUID | None
    retirement_reason: str | None
    allow_installation_when_retired: bool
    version: int
    created_at: datetime

    model_config = {"from_attributes": True}


class NetworkPortTemplateIn(BaseModel):
    stable_key: str = Field(max_length=64)
    display_name: str = Field(max_length=128)
    numbering_pattern: str | None = Field(default=None, max_length=64)
    media_type: str
    supported_speeds_mbps: list[int]
    connector_type: str = Field(max_length=32)
    role: str = "other"
    side: str
    module_group: str | None = Field(default=None, max_length=64)
    sort_order: int = 0


class NetworkPortTemplateUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    numbering_pattern: str | None = Field(default=None, max_length=64)
    media_type: str | None = None
    supported_speeds_mbps: list[int] | None = None
    connector_type: str | None = Field(default=None, max_length=32)
    role: str | None = None
    side: str | None = None
    module_group: str | None = Field(default=None, max_length=64)
    sort_order: int | None = None


class NetworkPortTemplateOut(BaseModel):
    id: uuid.UUID
    catalog_model_revision_id: uuid.UUID
    revision_version: int
    """The parent revision's version *after* this mutation — the request contract for
    child mutations (spec-extended §5.1: child rows carry no version of their own) is
    `If-Match` against the parent revision's version; this is the value to send as
    `If-Match` on the next mutation to this revision or any of its other child rows."""
    stable_key: str
    display_name: str
    numbering_pattern: str | None
    media_type: str
    supported_speeds_mbps: list
    connector_type: str
    role: str
    side: str
    module_group: str | None
    sort_order: int

    model_config = {"from_attributes": True}


def _network_port_out(port: NetworkPortTemplate, revision_version: int) -> NetworkPortTemplateOut:
    return NetworkPortTemplateOut(
        id=port.id, catalog_model_revision_id=port.catalog_model_revision_id, revision_version=revision_version,
        stable_key=port.stable_key, display_name=port.display_name, numbering_pattern=port.numbering_pattern,
        media_type=port.media_type, supported_speeds_mbps=port.supported_speeds_mbps, connector_type=port.connector_type,
        role=port.role, side=port.side, module_group=port.module_group, sort_order=port.sort_order,
    )


class PowerSupplyTemplateIn(BaseModel):
    stable_key: str = Field(max_length=64)
    label: str = Field(max_length=128)
    quantity: int = 1
    redundancy_mode: str = "single"
    connector_type: str = Field(max_length=32)
    rated_voltage_min: float | None = None
    rated_voltage_max: float | None = None
    rated_frequency_hz: float | None = None
    rated_current_a: float | None = None
    hot_swappable: bool | None = None
    sort_order: int = 0


class PowerSupplyTemplateUpdateIn(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    quantity: int | None = None
    redundancy_mode: str | None = None
    connector_type: str | None = Field(default=None, max_length=32)
    rated_voltage_min: float | None = None
    rated_voltage_max: float | None = None
    rated_frequency_hz: float | None = None
    rated_current_a: float | None = None
    hot_swappable: bool | None = None
    sort_order: int | None = None


class PowerSupplyTemplateOut(BaseModel):
    id: uuid.UUID
    catalog_model_revision_id: uuid.UUID
    revision_version: int
    stable_key: str
    label: str
    quantity: int
    redundancy_mode: str
    connector_type: str
    rated_voltage_min: float | None
    rated_voltage_max: float | None
    rated_frequency_hz: float | None
    rated_current_a: float | None
    hot_swappable: bool | None
    sort_order: int

    model_config = {"from_attributes": True}


def _power_supply_out(psu: PowerSupplyTemplate, revision_version: int) -> PowerSupplyTemplateOut:
    return PowerSupplyTemplateOut(
        id=psu.id, catalog_model_revision_id=psu.catalog_model_revision_id, revision_version=revision_version,
        stable_key=psu.stable_key, label=psu.label, quantity=psu.quantity, redundancy_mode=psu.redundancy_mode,
        connector_type=psu.connector_type, rated_voltage_min=psu.rated_voltage_min,
        rated_voltage_max=psu.rated_voltage_max, rated_frequency_hz=psu.rated_frequency_hz,
        rated_current_a=psu.rated_current_a, hot_swappable=psu.hot_swappable, sort_order=psu.sort_order,
    )


class MonitoringMetricTemplateIn(BaseModel):
    stable_key: str = Field(max_length=64)
    protocol: str
    protocol_other_label: str | None = Field(default=None, max_length=64)
    metric_name: str = Field(max_length=128)
    oid: str | None = Field(default=None, max_length=255)
    value_type: str
    unit: str | None = Field(default=None, max_length=32)
    scale: float = 1
    transform: str = "none"
    offset: float | None = None
    default_collection_interval_seconds: int | None = None
    default_warning_threshold: float | None = None
    default_critical_threshold: float | None = None
    sort_order: int = 0


class MonitoringMetricTemplateUpdateIn(BaseModel):
    protocol: str | None = None
    protocol_other_label: str | None = Field(default=None, max_length=64)
    metric_name: str | None = Field(default=None, max_length=128)
    oid: str | None = Field(default=None, max_length=255)
    value_type: str | None = None
    unit: str | None = Field(default=None, max_length=32)
    scale: float | None = None
    transform: str | None = None
    offset: float | None = None
    default_collection_interval_seconds: int | None = None
    default_warning_threshold: float | None = None
    default_critical_threshold: float | None = None
    sort_order: int | None = None


class MonitoringMetricTemplateOut(BaseModel):
    id: uuid.UUID
    catalog_model_revision_id: uuid.UUID
    revision_version: int
    stable_key: str
    protocol: str
    protocol_other_label: str | None
    metric_name: str
    oid: str | None
    value_type: str
    unit: str | None
    scale: float
    transform: str
    offset: float | None
    default_collection_interval_seconds: int | None
    default_warning_threshold: float | None
    default_critical_threshold: float | None
    sort_order: int

    model_config = {"from_attributes": True}


def _monitoring_metric_out(metric: MonitoringMetricTemplate, revision_version: int) -> MonitoringMetricTemplateOut:
    return MonitoringMetricTemplateOut(
        id=metric.id, catalog_model_revision_id=metric.catalog_model_revision_id, revision_version=revision_version,
        stable_key=metric.stable_key, protocol=metric.protocol, protocol_other_label=metric.protocol_other_label,
        metric_name=metric.metric_name, oid=metric.oid, value_type=metric.value_type, unit=metric.unit,
        scale=metric.scale, transform=metric.transform, offset=metric.offset,
        default_collection_interval_seconds=metric.default_collection_interval_seconds,
        default_warning_threshold=metric.default_warning_threshold,
        default_critical_threshold=metric.default_critical_threshold, sort_order=metric.sort_order,
    )


class CatalogGraphicMarkerIn(BaseModel):
    marker_type: str
    network_port_template_id: uuid.UUID | None = None
    power_supply_template_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=128)
    marker_x: float = Field(ge=0.0, le=1.0)
    marker_y: float = Field(ge=0.0, le=1.0)
    sort_order: int = 0

    @field_validator("marker_type")
    @classmethod
    def _marker_type_allowed(cls, value: str) -> str:
        # Mirrors CatalogGraphicMarker's own marker_type_allowed CHECK constraint at the
        # API boundary — a clean 422 for a bad value, not a raw IntegrityError mapped to
        # a generic 409 by the global handler (same reasoning as floor_plans.py's
        # AcceptCandidateIn._object_type_allowed).
        if value not in MARKER_TYPES:
            raise ValueError(f"marker_type must be one of {MARKER_TYPES}")
        return value

    @model_validator(mode="after")
    def _target_matches_type(self) -> "CatalogGraphicMarkerIn":
        # Mirrors marker_target_matches_type (migration 0019_catalog_graphics) — same
        # reasoning as _marker_type_allowed above.
        if self.marker_type == "network_port":
            if self.network_port_template_id is None or self.power_supply_template_id is not None:
                raise ValueError("network_port markers require network_port_template_id and no power_supply_template_id")
        elif self.marker_type == "power_supply":
            if self.power_supply_template_id is None or self.network_port_template_id is not None:
                raise ValueError("power_supply markers require power_supply_template_id and no network_port_template_id")
        elif self.network_port_template_id is not None or self.power_supply_template_id is not None:
            raise ValueError(f"{self.marker_type} markers must not link a network port or power supply")
        return self


class CatalogGraphicMarkerUpdateIn(BaseModel):
    """Partial update — the hybrid marker_type/target rule is re-checked against the
    *merged* result in the route handler (create_marker's model-level validator can't see
    the row's existing state, only the fields this request actually sent)."""

    marker_type: str | None = None
    network_port_template_id: uuid.UUID | None = None
    power_supply_template_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=128)
    marker_x: float | None = Field(default=None, ge=0.0, le=1.0)
    marker_y: float | None = Field(default=None, ge=0.0, le=1.0)
    sort_order: int | None = None


class CatalogGraphicMarkerOut(BaseModel):
    id: uuid.UUID
    catalog_graphic_id: uuid.UUID
    revision_version: int
    marker_type: str
    network_port_template_id: uuid.UUID | None
    power_supply_template_id: uuid.UUID | None
    label: str | None
    marker_x: float
    marker_y: float
    sort_order: int

    model_config = {"from_attributes": True}


def _marker_out(marker: CatalogGraphicMarker, revision_version: int) -> CatalogGraphicMarkerOut:
    return CatalogGraphicMarkerOut(
        id=marker.id, catalog_graphic_id=marker.catalog_graphic_id, revision_version=revision_version,
        marker_type=marker.marker_type, network_port_template_id=marker.network_port_template_id,
        power_supply_template_id=marker.power_supply_template_id, label=marker.label,
        marker_x=float(marker.marker_x), marker_y=float(marker.marker_y), sort_order=marker.sort_order,
    )


class CatalogGraphicOut(BaseModel):
    id: uuid.UUID
    catalog_model_revision_id: uuid.UUID
    revision_version: int
    side: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    width_px: int
    height_px: int
    uploaded_at: datetime
    markers: list[CatalogGraphicMarkerOut]

    model_config = {"from_attributes": True}


def _graphic_out(graphic: CatalogGraphic, revision_version: int) -> CatalogGraphicOut:
    return CatalogGraphicOut(
        id=graphic.id, catalog_model_revision_id=graphic.catalog_model_revision_id, revision_version=revision_version,
        side=graphic.side, original_filename=graphic.original_filename, mime_type=graphic.mime_type,
        file_size_bytes=graphic.file_size_bytes, width_px=graphic.width_px, height_px=graphic.height_px,
        uploaded_at=graphic.uploaded_at, markers=[_marker_out(m, revision_version) for m in graphic.markers],
    )


class CatalogModelRevisionDetailOut(CatalogModelRevisionOut):
    network_ports: list[NetworkPortTemplateOut]
    power_supplies: list[PowerSupplyTemplateOut]
    monitoring_metrics: list[MonitoringMetricTemplateOut]
    graphics: list[CatalogGraphicOut]


async def _load_children(
    db: AsyncSession, revision_id: uuid.UUID
) -> tuple[list[NetworkPortTemplate], list[PowerSupplyTemplate], list[MonitoringMetricTemplate]]:
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
    return ports, psus, metrics


async def _load_graphics(db: AsyncSession, revision_id: uuid.UUID) -> list[CatalogGraphic]:
    """`selectinload` eager-loads `markers` — an async session has no implicit lazy
    loading, and every caller of this helper (get_revision, validate_revision_endpoint,
    publish_revision) needs each graphic's markers, not just the graphic rows."""
    return list(
        (
            await db.execute(
                select(CatalogGraphic)
                .options(selectinload(CatalogGraphic.markers))
                .where(CatalogGraphic.catalog_model_revision_id == revision_id)
            )
        ).scalars()
    )


@router.post("/models/{model_id}/revisions", response_model=CatalogModelRevisionOut, status_code=201)
async def create_draft_revision(
    model_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogModelRevision:
    revision_number = await allocate_revision_number(db, catalog_model_id=model_id)
    revision = CatalogModelRevision(
        catalog_model_id=model_id, revision_number=revision_number, created_by_user_id=ctx.user.id
    )
    db.add(revision)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.create_draft", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        after={"catalog_model_id": str(model_id), "revision_number": revision.revision_number},
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftCreated", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"catalog_model_id": str(model_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return revision


@router.post("/models/{model_id}/revisions/clone", response_model=CatalogModelRevisionOut, status_code=201)
async def clone_revision_endpoint(
    model_id: uuid.UUID,
    from_revision_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogModelRevision:
    source = await db.get(CatalogModelRevision, from_revision_id)
    if source is None or source.catalog_model_id != model_id:
        raise NotFoundError(f"CatalogModelRevision {from_revision_id} not found under model {model_id}.")

    try:
        clone = await clone_revision(db, source_revision_id=from_revision_id, user_id=ctx.user.id)
    except Exception:
        await db.rollback()
        raise

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.clone", entity_type="catalog_model_revision",
        entity_id=clone.id, request_id=request_id, correlation_id=correlation_id,
        after={"cloned_from_revision_id": str(from_revision_id), "revision_number": clone.revision_number},
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftCreated", aggregate_type="catalog_model_revision",
        aggregate_id=clone.id, payload={"cloned_from_revision_id": str(from_revision_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return clone


class FieldDiff(BaseModel):
    field: str
    left: object
    right: object


class ChildDiff(BaseModel):
    collection: str
    stable_key: str
    change: str  # "added" | "removed" | "changed"
    left: dict | None = None
    right: dict | None = None


class CompareOut(BaseModel):
    left_revision_id: uuid.UUID
    right_revision_id: uuid.UUID
    field_diffs: list[FieldDiff]
    child_diffs: list[ChildDiff]


@router.get("/revisions/compare", response_model=CompareOut)
async def compare_revisions(
    left: uuid.UUID,
    right: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> CompareOut:
    """Conditional permission, loaded first since which permission applies depends on
    data only the database has (same shape as `GET /revisions/{id}`'s own gate): plain
    `catalog:read` suffices when both sides are published/retired; `catalog:read_draft`
    is additionally required the moment either side is a draft, closing the leak where a
    caller without draft access could otherwise read a draft's full field/child diff
    through this endpoint even though direct `GET /revisions/{draft_id}` correctly
    rejects them."""
    if not ctx.has_permission("catalog:read"):
        raise ApiError(status_code=403, title="Forbidden", detail="Missing required permission: catalog:read")

    left_revision = await db.get(CatalogModelRevision, left)
    right_revision = await db.get(CatalogModelRevision, right)
    if left_revision is None or right_revision is None:
        raise NotFoundError("Both revisions must exist to compare.")
    if left_revision.catalog_model_id != right_revision.catalog_model_id:
        raise ApiError(
            status_code=422, title="Invalid Comparison",
            detail="Both revisions must belong to the same catalog_model_id.",
        )
    if "draft" in (left_revision.lifecycle_status, right_revision.lifecycle_status) and not ctx.has_permission(
        "catalog:read_draft"
    ):
        raise ApiError(status_code=403, title="Forbidden", detail="Missing required permission: catalog:read_draft")

    field_diffs = []
    for field_name in _REVISION_SCALAR_FIELDS:
        left_value = getattr(left_revision, field_name)
        right_value = getattr(right_revision, field_name)
        if left_value != right_value:
            field_diffs.append(FieldDiff(field=field_name, left=left_value, right=right_value))

    child_diffs: list[ChildDiff] = []
    for collection_name, model_cls, fields in (
        ("network_ports", NetworkPortTemplate, ("display_name", "media_type", "connector_type", "role", "side")),
        ("power_supplies", PowerSupplyTemplate, ("label", "quantity", "redundancy_mode", "connector_type")),
        ("monitoring_metrics", MonitoringMetricTemplate, ("metric_name", "protocol", "oid", "value_type")),
    ):
        left_rows = {
            getattr(row, "stable_key"): row  # noqa: B009
            for row in (await db.execute(select(model_cls).where(model_cls.catalog_model_revision_id == left))).scalars()
        }
        right_rows = {
            getattr(row, "stable_key"): row  # noqa: B009
            for row in (await db.execute(select(model_cls).where(model_cls.catalog_model_revision_id == right))).scalars()
        }
        for stable_key in sorted(set(left_rows) | set(right_rows)):
            left_row = left_rows.get(stable_key)
            right_row = right_rows.get(stable_key)
            if left_row is None:
                right_fields = {f: getattr(right_row, f) for f in fields}
                child_diffs.append(
                    ChildDiff(collection=collection_name, stable_key=stable_key, change="added", right=right_fields)
                )
            elif right_row is None:
                left_fields = {f: getattr(left_row, f) for f in fields}
                child_diffs.append(
                    ChildDiff(collection=collection_name, stable_key=stable_key, change="removed", left=left_fields)
                )
            else:
                left_fields = {f: getattr(left_row, f) for f in fields}
                right_fields = {f: getattr(right_row, f) for f in fields}
                if left_fields != right_fields:
                    child_diffs.append(
                        ChildDiff(
                            collection=collection_name, stable_key=stable_key, change="changed",
                            left=left_fields, right=right_fields,
                        )
                    )

    return CompareOut(left_revision_id=left, right_revision_id=right, field_diffs=field_diffs, child_diffs=child_diffs)


async def _revision_read_dependency(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> tuple[CatalogModelRevision, AuthContext]:
    """The one conditional-permission read in this router (spec §10): `catalog:read_draft`
    only when the loaded revision is currently a draft, `catalog:read` otherwise. Loads the
    row first since which permission applies is itself data-dependent."""
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    required = "catalog:read_draft" if revision.lifecycle_status == "draft" else "catalog:read"
    if not ctx.has_permission(required):
        raise ApiError(status_code=403, title="Forbidden", detail=f"Missing required permission: {required}")
    return revision, ctx


@router.get("/revisions/{revision_id}", response_model=CatalogModelRevisionDetailOut)
async def get_revision(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db), loaded=Depends(_revision_read_dependency)
) -> CatalogModelRevisionDetailOut:
    revision, _ctx = loaded
    ports, psus, metrics = await _load_children(db, revision_id)
    graphics = await _load_graphics(db, revision_id)
    return CatalogModelRevisionDetailOut(
        **CatalogModelRevisionOut.model_validate(revision).model_dump(),
        network_ports=[_network_port_out(p, revision.version) for p in ports],
        power_supplies=[_power_supply_out(p, revision.version) for p in psus],
        monitoring_metrics=[_monitoring_metric_out(m, revision.version) for m in metrics],
        graphics=[_graphic_out(g, revision.version) for g in graphics],
    )


@router.patch("/revisions/{revision_id}", response_model=CatalogModelRevisionOut)
async def update_revision(
    revision_id: uuid.UUID,
    body: CatalogModelRevisionUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogModelRevision:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    before = {f: getattr(revision, f) for f in _REVISION_SCALAR_FIELDS}
    updates = body.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(revision, field_name, value)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.update_draft", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        before={k: (str(v) if v is not None else None) for k, v in before.items()},
        after={k: (str(getattr(revision, k)) if getattr(revision, k) is not None else None) for k in updates},
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftUpdated", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"version": revision.version}, correlation_id=correlation_id,
    )
    await db.commit()
    return revision


@router.delete("/revisions/{revision_id}", status_code=204)
async def delete_draft_revision(
    revision_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> None:
    """PR-3 correction pass, round 2: this used to load the row unlocked and check only
    `lifecycle_status`, with no If-Match at all — two concurrent deletes of the same draft
    both passed that check and both committed a `DELETE` (the second a silent zero-row
    no-op that PostgreSQL does not error on), so both reported 204 and both wrote a full
    audit/outbox pair for an entity already gone. Routing through
    `lock_draft_revision_for_edit` closes this the same way every other mutation in this
    router is closed: the `SELECT ... FOR UPDATE` forces the second request to block behind
    the first's transaction, then re-read row state *after* the first has committed — by
    which point the row no longer exists, so the second gets a clean 404 (via
    `NotFoundError`) instead of a duplicate success. The version bump `lock_draft_revision_
    for_edit` applies is a harmless no-op here (the row is deleted in the same flush, so the
    UPDATE it implies is never actually emitted), but the *lock* and the *If-Match
    precondition* are exactly the discipline this endpoint was missing."""
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.delete_draft", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        before={"catalog_model_id": str(revision.catalog_model_id), "revision_number": revision.revision_number},
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftDeleted", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"catalog_model_id": str(revision.catalog_model_id)}, correlation_id=correlation_id,
    )
    await db.delete(revision)
    await db.commit()


# ---------------------------------------------------------------- Network ports


@router.post("/revisions/{revision_id}/network-ports", response_model=NetworkPortTemplateOut, status_code=201)
async def create_network_port(
    revision_id: uuid.UUID,
    body: NetworkPortTemplateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> NetworkPortTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    port = NetworkPortTemplate(catalog_model_revision_id=revision_id, **body.model_dump())
    db.add(port)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"port_id": str(port.id), "stable_key": port.stable_key},
    )
    await db.commit()
    return _network_port_out(port, revision.version)


@router.patch("/revisions/{revision_id}/network-ports/{port_id}", response_model=NetworkPortTemplateOut)
async def update_network_port(
    revision_id: uuid.UUID,
    port_id: uuid.UUID,
    body: NetworkPortTemplateUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> NetworkPortTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    port = await db.get(NetworkPortTemplate, port_id)
    if port is None or port.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"NetworkPortTemplate {port_id} not found under revision {revision_id}.")

    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(port, field_name, value)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft", after={"port_id": str(port.id)},
    )
    await db.commit()
    return _network_port_out(port, revision.version)


@router.delete("/revisions/{revision_id}/network-ports/{port_id}", status_code=204)
async def delete_network_port(
    revision_id: uuid.UUID,
    port_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> None:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    port = await db.get(NetworkPortTemplate, port_id)
    if port is None or port.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"NetworkPortTemplate {port_id} not found under revision {revision_id}.")

    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.component_remove",
        before={"port_id": str(port.id), "stable_key": port.stable_key},
    )
    await db.delete(port)
    await db.commit()
    response.headers["X-Revision-Version"] = str(revision.version)


# ---------------------------------------------------------------- Power supplies


@router.post("/revisions/{revision_id}/power-supplies", response_model=PowerSupplyTemplateOut, status_code=201)
async def create_power_supply(
    revision_id: uuid.UUID,
    body: PowerSupplyTemplateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> PowerSupplyTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    psu = PowerSupplyTemplate(catalog_model_revision_id=revision_id, **body.model_dump())
    db.add(psu)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"power_supply_id": str(psu.id), "stable_key": psu.stable_key},
    )
    await db.commit()
    return _power_supply_out(psu, revision.version)


@router.patch("/revisions/{revision_id}/power-supplies/{psu_id}", response_model=PowerSupplyTemplateOut)
async def update_power_supply(
    revision_id: uuid.UUID,
    psu_id: uuid.UUID,
    body: PowerSupplyTemplateUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> PowerSupplyTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    psu = await db.get(PowerSupplyTemplate, psu_id)
    if psu is None or psu.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"PowerSupplyTemplate {psu_id} not found under revision {revision_id}.")

    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(psu, field_name, value)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft", after={"power_supply_id": str(psu.id)},
    )
    await db.commit()
    return _power_supply_out(psu, revision.version)


@router.delete("/revisions/{revision_id}/power-supplies/{psu_id}", status_code=204)
async def delete_power_supply(
    revision_id: uuid.UUID,
    psu_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> None:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    psu = await db.get(PowerSupplyTemplate, psu_id)
    if psu is None or psu.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"PowerSupplyTemplate {psu_id} not found under revision {revision_id}.")

    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.component_remove",
        before={"power_supply_id": str(psu.id), "stable_key": psu.stable_key},
    )
    await db.delete(psu)
    await db.commit()
    response.headers["X-Revision-Version"] = str(revision.version)


# ---------------------------------------------------------------- Monitoring metrics


@router.post("/revisions/{revision_id}/monitoring-metrics", response_model=MonitoringMetricTemplateOut, status_code=201)
async def create_monitoring_metric(
    revision_id: uuid.UUID,
    body: MonitoringMetricTemplateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> MonitoringMetricTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    metric = MonitoringMetricTemplate(catalog_model_revision_id=revision_id, **body.model_dump())
    db.add(metric)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"metric_id": str(metric.id), "stable_key": metric.stable_key},
    )
    await db.commit()
    return _monitoring_metric_out(metric, revision.version)


@router.patch("/revisions/{revision_id}/monitoring-metrics/{metric_id}", response_model=MonitoringMetricTemplateOut)
async def update_monitoring_metric(
    revision_id: uuid.UUID,
    metric_id: uuid.UUID,
    body: MonitoringMetricTemplateUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> MonitoringMetricTemplateOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    metric = await db.get(MonitoringMetricTemplate, metric_id)
    if metric is None or metric.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"MonitoringMetricTemplate {metric_id} not found under revision {revision_id}.")

    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(metric, field_name, value)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft", after={"metric_id": str(metric.id)},
    )
    await db.commit()
    return _monitoring_metric_out(metric, revision.version)


@router.delete("/revisions/{revision_id}/monitoring-metrics/{metric_id}", status_code=204)
async def delete_monitoring_metric(
    revision_id: uuid.UUID,
    metric_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> None:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    metric = await db.get(MonitoringMetricTemplate, metric_id)
    if metric is None or metric.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"MonitoringMetricTemplate {metric_id} not found under revision {revision_id}.")

    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.component_remove",
        before={"metric_id": str(metric.id), "stable_key": metric.stable_key},
    )
    await db.delete(metric)
    await db.commit()
    response.headers["X-Revision-Version"] = str(revision.version)


# ---------------------------------------------------------------- Graphics (Phase 10A PR-5)


async def _get_graphic_or_404(db: AsyncSession, *, revision_id: uuid.UUID, graphic_id: uuid.UUID) -> CatalogGraphic:
    graphic = await db.get(CatalogGraphic, graphic_id, options=[selectinload(CatalogGraphic.markers)])
    if graphic is None or graphic.catalog_model_revision_id != revision_id:
        raise NotFoundError(f"CatalogGraphic {graphic_id} not found under revision {revision_id}.")
    return graphic


async def _validate_marker_target_revision(
    db: AsyncSession, *, revision_id: uuid.UUID, network_port_template_id: uuid.UUID | None,
    power_supply_template_id: uuid.UUID | None,
) -> None:
    """Same defense-in-depth reasoning as every other check in this module (module
    docstring): `fn_validate_catalog_graphic_marker` (migration 0019_catalog_graphics)
    enforces this at the database layer regardless, but a clean 422 here is a better
    caller experience than a raw trigger exception surfacing as a generic error."""
    if network_port_template_id is not None:
        port = await db.get(NetworkPortTemplate, network_port_template_id)
        if port is None or port.catalog_model_revision_id != revision_id:
            raise ApiError(
                status_code=422, title="Unprocessable Entity",
                detail=f"network_port_template_id {network_port_template_id} does not belong to this revision.",
            )
    if power_supply_template_id is not None:
        psu = await db.get(PowerSupplyTemplate, power_supply_template_id)
        if psu is None or psu.catalog_model_revision_id != revision_id:
            raise ApiError(
                status_code=422, title="Unprocessable Entity",
                detail=f"power_supply_template_id {power_supply_template_id} does not belong to this revision.",
            )


@router.post("/revisions/{revision_id}/graphics/{side}", response_model=CatalogGraphicOut, status_code=201)
async def upload_graphic(
    revision_id: uuid.UUID,
    side: Literal["front", "rear"],
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogGraphicOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)

    content = await file.read()
    settings = get_settings()
    storage = get_storage_backend()
    try:
        graphic = await upload_catalog_graphic(
            db, revision=revision, side=side, content=content, original_filename=file.filename or "upload",
            uploaded_by_user_id=ctx.user.id, storage=storage, max_upload_bytes=settings.catalog_graphics_max_upload_bytes,
        )
    except GraphicRejected as exc:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=exc.reason) from exc

    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"graphic_id": str(graphic.id), "side": side, "original_filename": graphic.original_filename},
    )
    await db.commit()
    # A freshly-uploaded graphic can have no markers yet — even a same-side re-upload
    # went through upload_catalog_graphic's delete-then-recreate, so `graphic` here is
    # always a brand-new row. Building the response with markers=[] directly, rather
    # than calling _graphic_out (which reads `graphic.markers`), avoids an implicit
    # lazy-load: this ORM object's `markers` relationship was never eagerly loaded, and
    # async SQLAlchemy has no implicit lazy loading (unlike the sync ORM).
    return CatalogGraphicOut(
        id=graphic.id, catalog_model_revision_id=graphic.catalog_model_revision_id, revision_version=revision.version,
        side=graphic.side, original_filename=graphic.original_filename, mime_type=graphic.mime_type,
        file_size_bytes=graphic.file_size_bytes, width_px=graphic.width_px, height_px=graphic.height_px,
        uploaded_at=graphic.uploaded_at, markers=[],
    )


@router.get("/revisions/{revision_id}/graphics/{side}/file")
async def get_graphic_file(
    revision_id: uuid.UUID, side: Literal["front", "rear"], db: AsyncSession = Depends(get_db),
    loaded=Depends(_revision_read_dependency),
) -> Response:
    _revision, _ctx = loaded
    graphic = (
        await db.execute(
            select(CatalogGraphic).where(
                CatalogGraphic.catalog_model_revision_id == revision_id, CatalogGraphic.side == side
            )
        )
    ).scalar_one_or_none()
    if graphic is None:
        raise NotFoundError(f"No {side} graphic uploaded for revision {revision_id}.")
    content = get_storage_backend().read(graphic.storage_key)
    return Response(content=content, media_type=graphic.mime_type)


@router.get("/revisions/{revision_id}/graphics/{side}/thumbnail")
async def get_graphic_thumbnail(
    revision_id: uuid.UUID, side: Literal["front", "rear"], db: AsyncSession = Depends(get_db),
    loaded=Depends(_revision_read_dependency),
) -> Response:
    _revision, _ctx = loaded
    graphic = (
        await db.execute(
            select(CatalogGraphic).where(
                CatalogGraphic.catalog_model_revision_id == revision_id, CatalogGraphic.side == side
            )
        )
    ).scalar_one_or_none()
    if graphic is None:
        raise NotFoundError(f"No {side} graphic uploaded for revision {revision_id}.")
    content = get_storage_backend().read(thumbnail_storage_key(graphic.storage_key))
    return Response(content=content, media_type="image/jpeg")


@router.post("/revisions/{revision_id}/graphics/{graphic_id}/markers", response_model=CatalogGraphicMarkerOut, status_code=201)
async def create_marker(
    revision_id: uuid.UUID,
    graphic_id: uuid.UUID,
    body: CatalogGraphicMarkerIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogGraphicMarkerOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    graphic = await _get_graphic_or_404(db, revision_id=revision_id, graphic_id=graphic_id)
    await _validate_marker_target_revision(
        db, revision_id=revision_id, network_port_template_id=body.network_port_template_id,
        power_supply_template_id=body.power_supply_template_id,
    )

    marker = CatalogGraphicMarker(catalog_graphic_id=graphic.id, **body.model_dump())
    db.add(marker)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"marker_id": str(marker.id), "graphic_id": str(graphic.id), "marker_type": marker.marker_type},
    )
    await db.commit()
    return _marker_out(marker, revision.version)


@router.patch("/revisions/{revision_id}/graphics/{graphic_id}/markers/{marker_id}", response_model=CatalogGraphicMarkerOut)
async def update_marker(
    revision_id: uuid.UUID,
    graphic_id: uuid.UUID,
    marker_id: uuid.UUID,
    body: CatalogGraphicMarkerUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> CatalogGraphicMarkerOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    await _get_graphic_or_404(db, revision_id=revision_id, graphic_id=graphic_id)
    marker = await db.get(CatalogGraphicMarker, marker_id)
    if marker is None or marker.catalog_graphic_id != graphic_id:
        raise NotFoundError(f"CatalogGraphicMarker {marker_id} not found under graphic {graphic_id}.")

    updates = body.model_dump(exclude_unset=True)
    merged_type = updates.get("marker_type", marker.marker_type)
    merged_port_id = updates.get("network_port_template_id", marker.network_port_template_id)
    merged_psu_id = updates.get("power_supply_template_id", marker.power_supply_template_id)
    if merged_type not in MARKER_TYPES:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"marker_type must be one of {MARKER_TYPES}")
    if merged_type == "network_port" and (merged_port_id is None or merged_psu_id is not None):
        raise ApiError(
            status_code=422, title="Unprocessable Entity",
            detail="network_port markers require network_port_template_id and no power_supply_template_id",
        )
    if merged_type == "power_supply" and (merged_psu_id is None or merged_port_id is not None):
        raise ApiError(
            status_code=422, title="Unprocessable Entity",
            detail="power_supply markers require power_supply_template_id and no network_port_template_id",
        )
    if merged_type in ("module", "other") and (merged_port_id is not None or merged_psu_id is not None):
        raise ApiError(
            status_code=422, title="Unprocessable Entity",
            detail=f"{merged_type} markers must not link a network port or power supply",
        )
    await _validate_marker_target_revision(
        db, revision_id=revision_id, network_port_template_id=merged_port_id, power_supply_template_id=merged_psu_id,
    )

    for field_name, value in updates.items():
        setattr(marker, field_name, value)
    await db.flush()
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft", after={"marker_id": str(marker.id)},
    )
    await db.commit()
    return _marker_out(marker, revision.version)


@router.delete("/revisions/{revision_id}/graphics/{graphic_id}/markers/{marker_id}", status_code=204)
async def delete_marker(
    revision_id: uuid.UUID,
    graphic_id: uuid.UUID,
    marker_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> None:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    await _get_graphic_or_404(db, revision_id=revision_id, graphic_id=graphic_id)
    marker = await db.get(CatalogGraphicMarker, marker_id)
    if marker is None or marker.catalog_graphic_id != graphic_id:
        raise NotFoundError(f"CatalogGraphicMarker {marker_id} not found under graphic {graphic_id}.")

    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.component_remove",
        before={"marker_id": str(marker.id), "graphic_id": str(graphic_id)},
    )
    await db.delete(marker)
    await db.commit()
    response.headers["X-Revision-Version"] = str(revision.version)


async def _write_child_audit(
    db: AsyncSession, request: Request, ctx: AuthContext, revision_id: uuid.UUID, *, action: str,
    before: dict | None = None, after: dict | None = None,
) -> None:
    """Shared audit+outbox write for every network-port/power-supply/monitoring-metric
    create/edit/delete — spec §8's table funnels all of these through the revision-level
    `catalog.revision.update_draft` (create/edit) or `catalog.revision.component_remove`
    (delete) actions, both emitting `CatalogModelRevisionDraftUpdated`, entity_type/id
    scoped to the parent revision (the child row's own identity travels in before/after)."""
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action=action, entity_type="catalog_model_revision", entity_id=revision_id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=after,
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftUpdated", aggregate_type="catalog_model_revision",
        aggregate_id=revision_id, payload=after or before or {}, correlation_id=correlation_id,
    )


# ---------------------------------------------------------------- Validate / publish / retire / compare


@router.post("/revisions/{revision_id}/validate", response_model=ValidationSummary)
async def validate_revision_endpoint(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:read_draft")),
) -> ValidationSummary:
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    model = await db.get(CatalogModel, revision.catalog_model_id)
    assert model is not None
    ports, psus, metrics = await _load_children(db, revision_id)
    graphics = await _load_graphics(db, revision_id)
    return validate_revision_for_publish(revision, model.category, ports, psus, metrics, graphics)


@router.post("/revisions/{revision_id}/publish")
async def publish_revision_endpoint(
    revision_id: uuid.UUID,
    request: Request,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:publish")),
):
    try:
        revision = await publish_revision(db, revision_id=revision_id, user_id=ctx.user.id)
    except ValidationFailed as exc:
        await db.rollback()
        return JSONResponse(status_code=422, content=exc.summary.model_dump())
    except Exception:
        await db.rollback()
        raise

    assert revision.published_at is not None
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.publish", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        after={"lifecycle_status": revision.lifecycle_status, "published_at": revision.published_at.isoformat()},
        reason=reason,
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionPublished", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"catalog_model_id": str(revision.catalog_model_id)}, correlation_id=correlation_id,
    )
    await db.commit()
    return CatalogModelRevisionOut.model_validate(revision)


class RetireIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    allow_installation_when_retired: bool = False


@router.post("/revisions/{revision_id}/retire", response_model=CatalogModelRevisionOut)
async def retire_revision(
    revision_id: uuid.UUID,
    body: RetireIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:retire")),
) -> CatalogModelRevision:
    revision = (
        await db.execute(select(CatalogModelRevision).where(CatalogModelRevision.id == revision_id).with_for_update())
    ).scalar_one_or_none()
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    if revision.lifecycle_status != "published":
        raise ConflictError(
            detail=f"catalog_model_revision {revision_id} is not published "
            f"(status={revision.lifecycle_status}) and cannot be retired."
        )

    revision.lifecycle_status = "retired"
    revision.retired_at = datetime.now(UTC)
    revision.retired_by_user_id = ctx.user.id
    revision.retirement_reason = body.reason
    revision.allow_installation_when_retired = body.allow_installation_when_retired
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.retire", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        after={"lifecycle_status": "retired"}, reason=body.reason,
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionRetired", aggregate_type="catalog_model_revision", aggregate_id=revision.id,
        payload={"reason": body.reason}, correlation_id=correlation_id,
    )
    await db.commit()
    return revision


class RetireOverrideIn(BaseModel):
    allow_installation_when_retired: bool
    reason: str = Field(min_length=1, max_length=1000)


@router.patch("/revisions/{revision_id}/retire-override", response_model=CatalogModelRevisionOut)
async def update_retire_override(
    revision_id: uuid.UUID,
    body: RetireOverrideIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:retire")),
) -> CatalogModelRevision:
    revision = await db.get(CatalogModelRevision, revision_id)
    if revision is None:
        raise NotFoundError(f"CatalogModelRevision {revision_id} not found.")
    if revision.lifecycle_status != "retired":
        raise ConflictError(
            detail=f"catalog_model_revision {revision_id} is not retired (status={revision.lifecycle_status})."
        )

    before = revision.allow_installation_when_retired
    revision.allow_installation_when_retired = body.allow_installation_when_retired
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.revision.retire_override", entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=request_id, correlation_id=correlation_id,
        before={"allow_installation_when_retired": before},
        after={"allow_installation_when_retired": revision.allow_installation_when_retired}, reason=body.reason,
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionRetireOverrideChanged", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"allow_installation_when_retired": revision.allow_installation_when_retired},
        correlation_id=correlation_id,
    )
    await db.commit()
    return revision


