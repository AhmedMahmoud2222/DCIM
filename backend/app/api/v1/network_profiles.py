"""Vendor/device profile administration (Issue #101).

Read endpoints need `network_profile:read`; every mutation needs `network_profile:manage`.
Profile documents never contain credentials (see `reject_secret_like_keys`), so responses
return them in full. Content changes use `If-Match: <version>` like `Integration`.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.concurrency import require_if_match
from app.application.network import profile_service as svc
from app.application.network.profile_matching import DeviceFacts
from app.application.network.profile_schema import (
    DeviceProfileContent,
    MetricMappingContent,
    VendorProfileContent,
)
from app.application.network.profile_templates import TEMPLATES
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.network.profile_models import DeviceProfile, ProfileMetricMapping, VendorProfile

router = APIRouter(prefix="/network-profiles", tags=["network-profiles"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# ------------------------------------------------------------------------------ shapes
class MetricMappingOut(BaseModel):
    id: uuid.UUID
    oid: str
    canonical_metric: str
    unit: str
    scale: float
    value_type: str
    description: str | None

    model_config = {"from_attributes": True}


class VendorProfileOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: str
    sys_object_id_prefixes: list[str]
    supported_protocols: list[str]
    discovery_oids: dict
    neighbor_discovery: dict
    version: int
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DeviceProfileOut(BaseModel):
    id: uuid.UUID
    vendor_profile_id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: str
    device_class: str
    match_criteria: list
    firmware_min: str | None
    firmware_max: str | None
    priority: int
    capabilities: dict
    interface_discovery: dict
    neighbor_behavior: dict
    version: int
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VendorProfileIn(VendorProfileContent):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")


class DeviceProfileIn(DeviceProfileContent):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")


class MappingsIn(BaseModel):
    mappings: list[MetricMappingContent] = Field(max_length=256)


# ----------------------------------------------------------------------------- vendors
@router.get("/vendors", response_model=list[VendorProfileOut])
async def list_vendors(
    include_retired: bool = False, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> list[VendorProfileOut]:
    stmt = select(VendorProfile).order_by(VendorProfile.code)
    if not include_retired:
        stmt = stmt.where(VendorProfile.status == "active")
    return [VendorProfileOut.model_validate(v) for v in (await db.execute(stmt)).scalars()]


@router.post("/vendors", response_model=VendorProfileOut, status_code=201)
async def create_vendor(
    body: VendorProfileIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network_profile:manage")),
) -> VendorProfileOut:
    request_id, correlation_id = _request_ids(request)
    vendor = await svc.create_vendor(
        db, code=body.code, content=VendorProfileContent.model_validate(body.model_dump(exclude={"code"})),
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return VendorProfileOut.model_validate(vendor)


@router.get("/vendors/{vendor_id}", response_model=VendorProfileOut)
async def get_vendor(
    vendor_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> VendorProfileOut:
    return VendorProfileOut.model_validate(await svc.get_vendor(db, vendor_id))


@router.put("/vendors/{vendor_id}", response_model=VendorProfileOut)
async def update_vendor(
    vendor_id: uuid.UUID, body: VendorProfileContent, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> VendorProfileOut:
    request_id, correlation_id = _request_ids(request)
    vendor = await svc.update_vendor(
        db, vendor_id=vendor_id, content=body, expected_version=if_match_version, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return VendorProfileOut.model_validate(vendor)


@router.post("/vendors/{vendor_id}/retire", response_model=VendorProfileOut)
async def retire_vendor(
    vendor_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> VendorProfileOut:
    request_id, correlation_id = _request_ids(request)
    vendor = await svc.retire_vendor(
        db, vendor_id=vendor_id, expected_version=if_match_version, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return VendorProfileOut.model_validate(vendor)


@router.get("/vendors/{vendor_id}/metric-mappings", response_model=list[MetricMappingOut])
async def list_vendor_mappings(
    vendor_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> list[MetricMappingOut]:
    await svc.get_vendor(db, vendor_id)
    return [MetricMappingOut.model_validate(m) for m in await svc.list_metric_mappings(db, vendor_id=vendor_id)]


@router.put("/vendors/{vendor_id}/metric-mappings", response_model=list[MetricMappingOut])
async def replace_vendor_mappings(
    vendor_id: uuid.UUID, body: MappingsIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> list[MetricMappingOut]:
    request_id, correlation_id = _request_ids(request)
    rows = await svc.replace_metric_mappings(
        db, vendor_id=vendor_id, device_id=None, mappings=body.mappings, expected_version=if_match_version,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return [MetricMappingOut.model_validate(r) for r in rows]


# ----------------------------------------------------------------------------- devices
@router.get("/devices", response_model=list[DeviceProfileOut])
async def list_devices(
    vendor_profile_id: uuid.UUID | None = None, include_retired: bool = False, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network_profile:read")),
) -> list[DeviceProfileOut]:
    stmt = select(DeviceProfile).order_by(DeviceProfile.code)
    if vendor_profile_id is not None:
        stmt = stmt.where(DeviceProfile.vendor_profile_id == vendor_profile_id)
    if not include_retired:
        stmt = stmt.where(DeviceProfile.status == "active")
    return [DeviceProfileOut.model_validate(d) for d in (await db.execute(stmt)).scalars()]


@router.post("/vendors/{vendor_id}/devices", response_model=DeviceProfileOut, status_code=201)
async def create_device(
    vendor_id: uuid.UUID, body: DeviceProfileIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network_profile:manage")),
) -> DeviceProfileOut:
    request_id, correlation_id = _request_ids(request)
    device = await svc.create_device(
        db, vendor_id=vendor_id, code=body.code,
        content=DeviceProfileContent.model_validate(body.model_dump(exclude={"code"})),
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return DeviceProfileOut.model_validate(device)


@router.get("/devices/{device_id}", response_model=DeviceProfileOut)
async def get_device(
    device_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> DeviceProfileOut:
    return DeviceProfileOut.model_validate(await svc.get_device(db, device_id))


@router.put("/devices/{device_id}", response_model=DeviceProfileOut)
async def update_device(
    device_id: uuid.UUID, body: DeviceProfileContent, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> DeviceProfileOut:
    request_id, correlation_id = _request_ids(request)
    device = await svc.update_device(
        db, device_id=device_id, content=body, expected_version=if_match_version, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return DeviceProfileOut.model_validate(device)


@router.post("/devices/{device_id}/retire", response_model=DeviceProfileOut)
async def retire_device(
    device_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> DeviceProfileOut:
    request_id, correlation_id = _request_ids(request)
    device = await svc.retire_device(
        db, device_id=device_id, expected_version=if_match_version, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return DeviceProfileOut.model_validate(device)


@router.get("/devices/{device_id}/metric-mappings", response_model=list[MetricMappingOut])
async def list_device_mappings(
    device_id: uuid.UUID, effective: bool = False, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("network_profile:read")),
) -> list[MetricMappingOut]:
    await svc.get_device(db, device_id)
    if effective:
        rows: list[ProfileMetricMapping] = await svc.effective_metric_mappings(db, device_id)
    else:
        rows = await svc.list_metric_mappings(db, device_id=device_id)
    return [MetricMappingOut.model_validate(m) for m in rows]


@router.put("/devices/{device_id}/metric-mappings", response_model=list[MetricMappingOut])
async def replace_device_mappings(
    device_id: uuid.UUID, body: MappingsIn, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match), ctx=Depends(require_permission("network_profile:manage")),
) -> list[MetricMappingOut]:
    request_id, correlation_id = _request_ids(request)
    rows = await svc.replace_metric_mappings(
        db, vendor_id=None, device_id=device_id, mappings=body.mappings, expected_version=if_match_version,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return [MetricMappingOut.model_validate(r) for r in rows]


@router.get("/devices/{device_id}/plan")
async def device_plan(
    device_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> dict:
    """The resolved, non-secret acquisition plan (what a collector would execute)."""
    return await svc.build_plan(db, device_id)


# ------------------------------------------------------------------ matching / templates
class MatchIn(BaseModel):
    sys_object_id: str | None = Field(default=None, max_length=255)
    sys_descr: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    hardware_revision: str | None = Field(default=None, max_length=64)
    firmware: str | None = Field(default=None, max_length=64)


@router.post("/match")
async def match_profile(
    body: MatchIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("network_profile:read")),
) -> dict:
    """Dry run of the matcher. An ambiguous result selects nothing and lists candidates."""
    result = await svc.resolve_profile(db, DeviceFacts(**body.model_dump()))
    return result.as_dict()


@router.get("/templates")
async def list_templates(ctx=Depends(require_permission("network_profile:read"))) -> list[dict]:
    """Reviewable starting points (data, not behaviour). Creating from one is an ordinary
    `POST /vendors` with the template's document."""
    return [{"key": key, **value} for key, value in TEMPLATES.items()]


@router.get("/templates/{key}")
async def get_template(key: str, ctx=Depends(require_permission("network_profile:read"))) -> dict:
    template = TEMPLATES.get(key)
    if template is None:
        raise NotFoundError(f"Template {key!r} not found.")
    return {"key": key, **template}
