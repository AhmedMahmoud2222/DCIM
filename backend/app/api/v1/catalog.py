"""Rack/Equipment model catalog (ARCHITECTURE_REVIEW.md §4b). Create + list only —
a `*ModelRevision` is immutable once created (a corrected spec is a new revision, never
an edit), so there is no update endpoint for a revision; a `*Model`'s manufacturer/name
identity is likewise not editable here (create a new catalog entry instead)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision

router = APIRouter(tags=["catalog"])


# ---------------------------------------------------------------- RackModel
class RackModelIn(BaseModel):
    manufacturer: str
    model_name: str


class RackModelOut(BaseModel):
    id: uuid.UUID
    manufacturer: str
    model_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RackModelRevisionIn(BaseModel):
    height_u: int
    width_mm: int
    depth_mm: int
    weight_capacity_kg: int | None = None


class RackModelRevisionOut(BaseModel):
    id: uuid.UUID
    rack_model_id: uuid.UUID
    height_u: int
    width_mm: int
    depth_mm: int
    weight_capacity_kg: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/rack-models", response_model=RackModelOut, status_code=201)
async def create_rack_model(
    body: RackModelIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("rack:manage"))
) -> RackModel:
    model = RackModel(**body.model_dump())
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@router.get("/rack-models", response_model=Page[RackModelOut])
async def list_rack_models(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("rack:read")),
) -> Page:
    total = (await db.execute(select(func.count()).select_from(RackModel))).scalar_one()
    rows = (
        await db.execute(select(RackModel).order_by(RackModel.manufacturer).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/rack-models/{rack_model_id}/revisions", response_model=RackModelRevisionOut, status_code=201)
async def create_rack_model_revision(
    rack_model_id: uuid.UUID,
    body: RackModelRevisionIn,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("rack:manage")),
) -> RackModelRevision:
    if await db.get(RackModel, rack_model_id) is None:
        raise NotFoundError(f"RackModel {rack_model_id} not found.")
    revision = RackModelRevision(rack_model_id=rack_model_id, **body.model_dump())
    db.add(revision)
    await db.commit()
    await db.refresh(revision)
    return revision


@router.get("/rack-models/{rack_model_id}/revisions", response_model=Page[RackModelRevisionOut])
async def list_rack_model_revisions(
    rack_model_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("rack:read")),
) -> Page:
    stmt = select(RackModelRevision).where(RackModelRevision.rack_model_id == rack_model_id)
    count_stmt = (
        select(func.count())
        .select_from(RackModelRevision)
        .where(RackModelRevision.rack_model_id == rack_model_id)
    )
    total = (await db.execute(count_stmt)).scalar_one()
    paged_stmt = (
        stmt.order_by(RackModelRevision.created_at)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged_stmt)).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


# ---------------------------------------------------------------- EquipmentModel
class EquipmentModelIn(BaseModel):
    manufacturer: str
    model_name: str


class EquipmentModelOut(BaseModel):
    id: uuid.UUID
    manufacturer: str
    model_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EquipmentModelRevisionIn(BaseModel):
    height_u: int | None = None
    width_mm: int | None = None
    depth_mm: int | None = None
    weight_kg: int | None = None


class EquipmentModelRevisionOut(BaseModel):
    id: uuid.UUID
    equipment_model_id: uuid.UUID
    height_u: int | None
    width_mm: int | None
    depth_mm: int | None
    weight_kg: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/equipment-models", response_model=EquipmentModelOut, status_code=201)
async def create_equipment_model(
    body: EquipmentModelIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("equipment:manage"))
) -> EquipmentModel:
    model = EquipmentModel(**body.model_dump())
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@router.get("/equipment-models", response_model=Page[EquipmentModelOut])
async def list_equipment_models(
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("equipment:read")),
) -> Page:
    total = (await db.execute(select(func.count()).select_from(EquipmentModel))).scalar_one()
    rows = (
        await db.execute(
            select(EquipmentModel).order_by(EquipmentModel.manufacturer).offset(pagination.offset).limit(pagination.limit)
        )
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/equipment-models/{equipment_model_id}/revisions", response_model=EquipmentModelRevisionOut, status_code=201)
async def create_equipment_model_revision(
    equipment_model_id: uuid.UUID,
    body: EquipmentModelRevisionIn,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("equipment:manage")),
) -> EquipmentModelRevision:
    if await db.get(EquipmentModel, equipment_model_id) is None:
        raise NotFoundError(f"EquipmentModel {equipment_model_id} not found.")
    revision = EquipmentModelRevision(equipment_model_id=equipment_model_id, **body.model_dump())
    db.add(revision)
    await db.commit()
    await db.refresh(revision)
    return revision


@router.get("/equipment-models/{equipment_model_id}/revisions", response_model=Page[EquipmentModelRevisionOut])
async def list_equipment_model_revisions(
    equipment_model_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("equipment:read")),
) -> Page:
    stmt = select(EquipmentModelRevision).where(EquipmentModelRevision.equipment_model_id == equipment_model_id)
    total = (
        await db.execute(
            select(func.count())
            .select_from(EquipmentModelRevision)
            .where(EquipmentModelRevision.equipment_model_id == equipment_model_id)
        )
    ).scalar_one()
    rows = (
        await db.execute(stmt.order_by(EquipmentModelRevision.created_at).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)
