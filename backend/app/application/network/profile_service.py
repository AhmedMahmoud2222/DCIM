"""Persistence and lifecycle for vendor/device profiles.

All writes are audited in the caller's transaction. Content changes require the current
`version` (optimistic concurrency, same `If-Match` convention as `Integration`), retired
profiles are immutable, and a profile that is still referenced is never deleted -- it is
retired once nothing live depends on it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match
from app.application.network.profile_matching import (
    DeviceFacts,
    DeviceView,
    MatchResult,
    VendorView,
    resolve,
)
from app.application.network.profile_schema import (
    DeviceProfileContent,
    MetricMappingContent,
    VendorProfileContent,
    reject_secret_like_keys,
)
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.integration.models import Integration
from app.domain.network.profile_models import DeviceProfile, ProfileMetricMapping, VendorProfile
from app.domain.telemetry.registry import UnitDimensionMismatch, UnknownMetric, UnknownUnit, validate_metric_unit


def _unprocessable(detail: str) -> ApiError:
    return ApiError(status_code=422, title="Invalid Profile", detail=detail, type_="https://dcim.internal/errors/validation")


def _vendor_snapshot(vendor: VendorProfile) -> dict:
    return {
        "code": vendor.code, "name": vendor.name, "status": vendor.status, "version": vendor.version,
        "sys_object_id_prefixes": vendor.sys_object_id_prefixes, "supported_protocols": vendor.supported_protocols,
    }


def _device_snapshot(device: DeviceProfile) -> dict:
    return {
        "code": device.code, "name": device.name, "status": device.status, "version": device.version,
        "vendor_profile_id": str(device.vendor_profile_id), "device_class": device.device_class,
        "priority": device.priority,
    }


# ----------------------------------------------------------------------------- vendors
async def _assert_prefixes_unclaimed(db: AsyncSession, prefixes: list[str], *, exclude_id: uuid.UUID | None) -> None:
    """Two active vendors must never claim the identical enterprise prefix: the matcher
    would have to refuse every device under it. Nested prefixes (a vendor claiming a more
    specific arc) are allowed and resolved by longest-prefix."""
    if not prefixes:
        return
    stmt = select(VendorProfile).where(VendorProfile.status == "active")
    if exclude_id is not None:
        stmt = stmt.where(VendorProfile.id != exclude_id)
    for other in (await db.execute(stmt)).scalars():
        overlap = set(prefixes) & set(other.sys_object_id_prefixes)
        if overlap:
            raise ConflictError(
                f"sysObjectID prefix {sorted(overlap)[0]} is already claimed by vendor profile '{other.code}'."
            )


async def create_vendor(
    db: AsyncSession, *, code: str, content: VendorProfileContent, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> VendorProfile:
    reject_secret_like_keys(content.model_dump())
    if (await db.execute(select(VendorProfile.id).where(VendorProfile.code == code))).first() is not None:
        raise ConflictError(f"A vendor profile with code '{code}' already exists.")
    await _assert_prefixes_unclaimed(db, content.sys_object_id_prefixes, exclude_id=None)
    vendor = VendorProfile(
        id=uuid.uuid4(), code=code, name=content.name, description=content.description, status="active",
        sys_object_id_prefixes=content.sys_object_id_prefixes, supported_protocols=content.supported_protocols,
        discovery_oids=content.discovery_oids, neighbor_discovery=content.neighbor_discovery.model_dump(exclude_none=True),
        version=1,
    )
    db.add(vendor)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.vendor.create", entity_type="vendor_profile",
        entity_id=vendor.id, request_id=request_id, correlation_id=correlation_id, after=_vendor_snapshot(vendor),
    )
    return vendor


async def get_vendor(db: AsyncSession, vendor_id: uuid.UUID, *, for_update: bool = False) -> VendorProfile:
    stmt = select(VendorProfile).where(VendorProfile.id == vendor_id)
    if for_update:
        stmt = stmt.with_for_update()
    vendor = (await db.execute(stmt)).scalar_one_or_none()
    if vendor is None:
        raise NotFoundError(f"VendorProfile {vendor_id} not found.")
    return vendor


async def update_vendor(
    db: AsyncSession, *, vendor_id: uuid.UUID, content: VendorProfileContent, expected_version: int,
    actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> VendorProfile:
    vendor = await get_vendor(db, vendor_id, for_update=True)
    if vendor.status == "retired":
        raise ConflictError("A retired vendor profile is immutable.")
    check_version_match(expected=expected_version, actual=vendor.version)
    reject_secret_like_keys(content.model_dump())
    await _assert_prefixes_unclaimed(db, content.sys_object_id_prefixes, exclude_id=vendor.id)
    before = _vendor_snapshot(vendor)
    vendor.name = content.name
    vendor.description = content.description
    vendor.sys_object_id_prefixes = content.sys_object_id_prefixes
    vendor.supported_protocols = content.supported_protocols
    vendor.discovery_oids = content.discovery_oids
    vendor.neighbor_discovery = content.neighbor_discovery.model_dump(exclude_none=True)
    vendor.version += 1
    await db.flush()
    await db.refresh(vendor)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.vendor.update", entity_type="vendor_profile",
        entity_id=vendor.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after=_vendor_snapshot(vendor),
    )
    return vendor


async def retire_vendor(
    db: AsyncSession, *, vendor_id: uuid.UUID, expected_version: int, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> VendorProfile:
    vendor = await get_vendor(db, vendor_id, for_update=True)
    if vendor.status == "retired":
        raise ConflictError("The vendor profile is already retired.")
    check_version_match(expected=expected_version, actual=vendor.version)
    active_devices = (
        await db.execute(
            select(func.count()).select_from(DeviceProfile).where(
                DeviceProfile.vendor_profile_id == vendor.id, DeviceProfile.status == "active"
            )
        )
    ).scalar_one()
    if active_devices:
        raise ConflictError(f"Retire the {active_devices} active device profile(s) of this vendor first.")
    before = _vendor_snapshot(vendor)
    vendor.status = "retired"
    vendor.retired_at = datetime.now(UTC)
    vendor.version += 1
    await db.flush()
    await db.refresh(vendor)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.vendor.retire", entity_type="vendor_profile",
        entity_id=vendor.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after=_vendor_snapshot(vendor),
    )
    return vendor


# ----------------------------------------------------------------------------- devices
async def create_device(
    db: AsyncSession, *, vendor_id: uuid.UUID, code: str, content: DeviceProfileContent, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> DeviceProfile:
    vendor = await get_vendor(db, vendor_id, for_update=True)
    if vendor.status != "active":
        raise ConflictError("Device profiles can only be created under an active vendor profile.")
    _validate_device_against_vendor(vendor, content)
    duplicate = await db.execute(
        select(DeviceProfile.id).where(DeviceProfile.vendor_profile_id == vendor.id, DeviceProfile.code == code)
    )
    if duplicate.first() is not None:
        raise ConflictError(f"A device profile with code '{code}' already exists for this vendor.")
    device = DeviceProfile(id=uuid.uuid4(), vendor_profile_id=vendor.id, code=code, status="active", version=1)
    _apply_device_content(device, content)
    db.add(device)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.device.create", entity_type="device_profile",
        entity_id=device.id, request_id=request_id, correlation_id=correlation_id, after=_device_snapshot(device),
    )
    return device


def _apply_device_content(device: DeviceProfile, content: DeviceProfileContent) -> None:
    dumped = content.model_dump()
    reject_secret_like_keys(dumped)
    device.name = content.name
    device.description = content.description
    device.device_class = content.device_class
    device.match_criteria = dumped["match_criteria"]
    device.firmware_min = content.firmware_min
    device.firmware_max = content.firmware_max
    device.priority = content.priority
    device.capabilities = dumped["capabilities"]
    device.interface_discovery = dumped["interface_discovery"]
    device.neighbor_behavior = dumped["neighbor_behavior"]


def _validate_device_against_vendor(vendor: VendorProfile, content: DeviceProfileContent) -> None:
    """A device profile may only use behaviour its vendor profile actually defines."""
    spec = vendor.neighbor_discovery or {}
    for protocol in ("lldp", "cdp"):
        wanted = getattr(content.neighbor_behavior, protocol).enabled
        if wanted and not (spec.get(protocol) or {}).get("enabled"):
            raise _unprocessable(
                f"{protocol.upper()} behaviour is enabled but vendor profile '{vendor.code}' does not define it."
            )
    if content.capabilities.snmp_versions and "snmp" not in vendor.supported_protocols:
        raise _unprocessable("snmp_versions declared but the vendor profile does not support the snmp protocol.")


async def get_device(db: AsyncSession, device_id: uuid.UUID, *, for_update: bool = False) -> DeviceProfile:
    stmt = select(DeviceProfile).where(DeviceProfile.id == device_id)
    if for_update:
        stmt = stmt.with_for_update()
    device = (await db.execute(stmt)).scalar_one_or_none()
    if device is None:
        raise NotFoundError(f"DeviceProfile {device_id} not found.")
    return device


async def update_device(
    db: AsyncSession, *, device_id: uuid.UUID, content: DeviceProfileContent, expected_version: int,
    actor_user_id: uuid.UUID, request_id: str | None, correlation_id: str | None,
) -> DeviceProfile:
    device = await get_device(db, device_id, for_update=True)
    if device.status == "retired":
        raise ConflictError("A retired device profile is immutable.")
    check_version_match(expected=expected_version, actual=device.version)
    vendor = await get_vendor(db, device.vendor_profile_id)
    _validate_device_against_vendor(vendor, content)
    before = _device_snapshot(device)
    _apply_device_content(device, content)
    device.version += 1
    await db.flush()
    await db.refresh(device)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.device.update", entity_type="device_profile",
        entity_id=device.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after=_device_snapshot(device),
    )
    return device


async def retire_device(
    db: AsyncSession, *, device_id: uuid.UUID, expected_version: int, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> DeviceProfile:
    device = await get_device(db, device_id, for_update=True)
    if device.status == "retired":
        raise ConflictError("The device profile is already retired.")
    check_version_match(expected=expected_version, actual=device.version)
    bound = (
        await db.execute(select(func.count()).select_from(Integration).where(Integration.device_profile_id == device.id))
    ).scalar_one()
    if bound:
        raise ConflictError(f"{bound} integration(s) are still bound to this device profile; rebind or unbind them first.")
    before = _device_snapshot(device)
    device.status = "retired"
    device.retired_at = datetime.now(UTC)
    device.version += 1
    await db.flush()
    await db.refresh(device)
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="network_profile.device.retire", entity_type="device_profile",
        entity_id=device.id, request_id=request_id, correlation_id=correlation_id, before=before,
        after=_device_snapshot(device),
    )
    return device


# ----------------------------------------------------------------------- metric mappings
def validate_mapping(mapping: MetricMappingContent) -> None:
    try:
        validate_metric_unit(mapping.canonical_metric, mapping.unit)
    except (UnknownMetric, UnknownUnit, UnitDimensionMismatch) as exc:
        raise _unprocessable(str(exc)) from exc


async def replace_metric_mappings(
    db: AsyncSession, *, vendor_id: uuid.UUID | None, device_id: uuid.UUID | None,
    mappings: list[MetricMappingContent], expected_version: int, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None,
) -> list[ProfileMetricMapping]:
    """Replaces the owner's whole mapping set atomically and bumps the owner's version."""
    assert (vendor_id is None) != (device_id is None)
    if vendor_id is not None:
        owner: VendorProfile | DeviceProfile = await get_vendor(db, vendor_id, for_update=True)
    else:
        assert device_id is not None
        owner = await get_device(db, device_id, for_update=True)
    if owner.status == "retired":
        raise ConflictError("A retired profile is immutable.")
    check_version_match(expected=expected_version, actual=owner.version)
    oids = [m.oid for m in mappings]
    metrics = [m.canonical_metric for m in mappings]
    if len(set(oids)) != len(oids):
        raise _unprocessable("Each OID may be mapped at most once per profile.")
    if len(set(metrics)) != len(metrics):
        raise _unprocessable("Each canonical metric may be mapped at most once per profile.")
    for mapping in mappings:
        validate_mapping(mapping)

    owner_column = ProfileMetricMapping.vendor_profile_id if vendor_id is not None else ProfileMetricMapping.device_profile_id
    owner_id = vendor_id if vendor_id is not None else device_id
    for existing in (await db.execute(select(ProfileMetricMapping).where(owner_column == owner_id))).scalars():
        await db.delete(existing)
    await db.flush()
    rows = [
        ProfileMetricMapping(
            id=uuid.uuid4(), vendor_profile_id=vendor_id, device_profile_id=device_id, oid=m.oid,
            canonical_metric=m.canonical_metric, unit=m.unit, scale=m.scale, value_type=m.value_type,
            description=m.description,
        )
        for m in mappings
    ]
    db.add_all(rows)
    owner.version += 1
    await db.flush()
    for row in rows:
        await db.refresh(row)
    await write_audit_log(
        db, actor_user_id=actor_user_id,
        action="network_profile.vendor.mappings" if vendor_id is not None else "network_profile.device.mappings",
        entity_type="vendor_profile" if vendor_id is not None else "device_profile", entity_id=owner.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"mappings": [{"oid": m.oid, "canonical_metric": m.canonical_metric, "unit": m.unit} for m in mappings]},
    )
    return rows


async def list_metric_mappings(
    db: AsyncSession, *, vendor_id: uuid.UUID | None = None, device_id: uuid.UUID | None = None
) -> list[ProfileMetricMapping]:
    stmt = select(ProfileMetricMapping).order_by(ProfileMetricMapping.canonical_metric)
    if vendor_id is not None:
        stmt = stmt.where(ProfileMetricMapping.vendor_profile_id == vendor_id)
    if device_id is not None:
        stmt = stmt.where(ProfileMetricMapping.device_profile_id == device_id)
    return list((await db.execute(stmt)).scalars())


async def effective_metric_mappings(db: AsyncSession, device_profile_id: uuid.UUID) -> list[ProfileMetricMapping]:
    """Vendor defaults, overridden per canonical metric by the device profile."""
    device = await get_device(db, device_profile_id)
    merged: dict[str, ProfileMetricMapping] = {
        row.canonical_metric: row for row in await list_metric_mappings(db, vendor_id=device.vendor_profile_id)
    }
    for row in await list_metric_mappings(db, device_id=device.id):
        merged[row.canonical_metric] = row
    return sorted(merged.values(), key=lambda row: row.canonical_metric)


# ---------------------------------------------------------------------------- matching
async def load_match_views(db: AsyncSession) -> tuple[list[VendorView], list[DeviceView]]:
    vendors = [
        VendorView(id=v.id, code=v.code, prefixes=tuple(v.sys_object_id_prefixes))
        for v in (await db.execute(select(VendorProfile).where(VendorProfile.status == "active"))).scalars()
    ]
    devices = [
        DeviceView(
            id=d.id, vendor_profile_id=d.vendor_profile_id, code=d.code,
            criteria=tuple(
                (c["field"], c["op"], tuple(c["value"]) if isinstance(c["value"], list) else c["value"])
                for c in d.match_criteria
            ),
            firmware_min=d.firmware_min, firmware_max=d.firmware_max, priority=d.priority,
        )
        for d in (await db.execute(select(DeviceProfile).where(DeviceProfile.status == "active"))).scalars()
    ]
    return vendors, devices


async def resolve_profile(db: AsyncSession, facts: DeviceFacts) -> MatchResult:
    vendors, devices = await load_match_views(db)
    return resolve(facts, vendors, devices)


async def bind_integration_profile(
    db: AsyncSession, *, integration: Integration, device_profile_id: uuid.UUID | None,
) -> None:
    """Explicit operator binding. Rejects retired profiles and protocol mismatches; never
    consults the matcher, so an operator can deliberately break a matcher tie."""
    if device_profile_id is None:
        integration.device_profile_id = None
        return
    device = await get_device(db, device_profile_id)
    if device.status != "active":
        raise ConflictError("A retired device profile cannot be bound to an integration.")
    vendor = await get_vendor(db, device.vendor_profile_id)
    if integration.integration_type not in vendor.supported_protocols:
        raise _unprocessable(
            f"Vendor profile '{vendor.code}' does not support the '{integration.integration_type}' protocol."
        )
    version = (integration.config or {}).get("version")
    declared = (device.capabilities or {}).get("snmp_versions") or []
    if integration.integration_type == "snmp" and version and declared and version not in declared:
        raise _unprocessable(f"Device profile '{device.code}' does not support SNMP {version}.")
    integration.device_profile_id = device.id


async def build_plan(db: AsyncSession, device_profile_id: uuid.UUID) -> dict:
    """The non-secret, fully resolved acquisition plan a collector needs: vendor
    discovery defaults, neighbor-discovery table definitions, the device profile's
    behaviour and the effective metric mappings. Contains no credentials."""
    device = await get_device(db, device_profile_id)
    vendor = await get_vendor(db, device.vendor_profile_id)
    mappings = await effective_metric_mappings(db, device.id)
    return {
        "vendor_profile": {"id": str(vendor.id), "code": vendor.code, "version": vendor.version},
        "device_profile": {"id": str(device.id), "code": device.code, "version": device.version},
        "device_class": device.device_class,
        "discovery_oids": vendor.discovery_oids,
        "neighbor_discovery": vendor.neighbor_discovery,
        "neighbor_behavior": device.neighbor_behavior,
        "interface_discovery": device.interface_discovery,
        "capabilities": device.capabilities,
        "metric_mappings": [
            {
                "oid": m.oid, "canonical_metric": m.canonical_metric, "unit": m.unit, "scale": float(m.scale),
                "value_type": m.value_type,
            }
            for m in mappings
        ],
    }
