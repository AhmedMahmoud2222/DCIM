"""Equipment bulk-import row validator. See validators/rack.py's module docstring —
identical reasoning applies here."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.bulk_import.resolvers import (
    RowRejected,
    cell_str,
    optional_int,
    require_str,
    resolve_equipment_model_revision,
    resolve_managed_asset_by_tag,
    resolve_rack_by_asset_tag,
    resolve_room,
)
from app.application.bulk_import.validators import BatchState, RowValidationResult, error
from app.application.spatial_validation import validate_u_range_against_rack_capacity
from app.core.errors import ApiError
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import LIFECYCLE_STATUSES
from app.domain.placement.models import PLACEMENT_TYPES, SIDES


async def validate_row(
    db: AsyncSession, *, row_number: int, raw: dict, mode: str, batch_state: BatchState
) -> RowValidationResult:
    errors: list[dict] = []
    warnings: list[dict] = []

    try:
        asset_tag = require_str(raw.get("asset_tag"), "asset_tag")
    except RowRejected as exc:
        return RowValidationResult(status="invalid", action=None, errors=[error(exc.field, exc.message)])

    if asset_tag in batch_state.seen_asset_tags:
        errors.append(error("asset_tag", f"Duplicate asset_tag {asset_tag!r} within this file."))
    else:
        batch_state.seen_asset_tags.add(asset_tag)

    for field_name in ("manufacturer", "model_name", "site_code", "building_code", "room_code"):
        try:
            require_str(raw.get(field_name), field_name)
        except RowRejected as exc:
            errors.append(error(exc.field, exc.message))

    placement_type = cell_str(raw.get("placement_type"))
    if placement_type is None:
        errors.append(error("placement_type", "placement_type is required."))
    elif placement_type not in PLACEMENT_TYPES:
        errors.append(error("placement_type", f"placement_type must be one of {PLACEMENT_TYPES}, got {placement_type!r}."))

    try:
        floor_level = optional_int(raw.get("floor_level"), "floor_level")
        if floor_level is None:
            errors.append(error("floor_level", "floor_level is required."))
    except RowRejected as exc:
        errors.append(error(exc.field, exc.message))
        floor_level = None

    lifecycle_status = cell_str(raw.get("lifecycle_status"))
    if lifecycle_status is not None and lifecycle_status not in LIFECYCLE_STATUSES:
        errors.append(error("lifecycle_status", f"lifecycle_status must be one of {LIFECYCLE_STATUSES}."))

    side = cell_str(raw.get("side"))
    if side is not None and side not in SIDES:
        errors.append(error("side", f"side must be one of {SIDES}, got {side!r}."))

    u_start = u_end = None
    try:
        u_start = optional_int(raw.get("u_start"), "u_start")
    except RowRejected as exc:
        errors.append(error(exc.field, exc.message))
    try:
        u_end = optional_int(raw.get("u_end"), "u_end")
    except RowRejected as exc:
        errors.append(error(exc.field, exc.message))

    rack_asset_tag = cell_str(raw.get("rack_asset_tag"))
    if placement_type == "rack_mounted":
        if rack_asset_tag is None:
            errors.append(error("rack_asset_tag", "rack_asset_tag is required when placement_type=rack_mounted."))
        if u_start is None or u_end is None:
            errors.append(error("u_start", "u_start and u_end are required when placement_type=rack_mounted."))
        if side is None:
            errors.append(error("side", "side is required when placement_type=rack_mounted."))

        if rack_asset_tag is not None and u_start is not None and u_end is not None:
            try:
                _asset, rack = await resolve_rack_by_asset_tag(db, rack_asset_tag)
                rack_model_revision = await db.get(RackModelRevision, rack.model_revision_id)
                if rack_model_revision is None:
                    errors.append(error("rack_asset_tag", f"Rack {rack_asset_tag!r} has no resolvable model revision."))
                else:
                    validate_u_range_against_rack_capacity(
                        u_start=u_start, u_end=u_end, rack_height_u=rack_model_revision.height_u
                    )
            except RowRejected as exc:
                errors.append(error(exc.field, exc.message))
            except ApiError as exc:
                errors.append(error("u_start", exc.detail))

    target_managed_asset_id = None
    action = "create"
    if mode == "create_only":
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is not None:
            errors.append(error("asset_tag", f"asset_tag {asset_tag!r} already exists (create_only mode)."))
    else:
        action = "update"
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is None:
            errors.append(error("asset_tag", f"No existing equipment with asset_tag {asset_tag!r} to update."))
        elif existing.asset_type != "equipment":
            errors.append(error("asset_tag", f"asset_tag {asset_tag!r} does not identify equipment."))
        else:
            target_managed_asset_id = existing.id

    manufacturer = cell_str(raw.get("manufacturer"))
    model_name = cell_str(raw.get("model_name"))
    if manufacturer and model_name:
        try:
            revision_number = optional_int(raw.get("revision_number"), "revision_number")
            await resolve_equipment_model_revision(
                db, manufacturer=manufacturer, model_name=model_name, revision_number=revision_number
            )
        except RowRejected as exc:
            errors.append(error(exc.field, exc.message))

    site_code = cell_str(raw.get("site_code"))
    building_code = cell_str(raw.get("building_code"))
    room_code = cell_str(raw.get("room_code"))
    if site_code and building_code and room_code and floor_level is not None:
        try:
            await resolve_room(db, site_code=site_code, building_code=building_code, floor_level=floor_level, room_code=room_code)
        except RowRejected as exc:
            errors.append(error(exc.field, exc.message))

    status = "invalid" if errors else "valid"
    return RowValidationResult(
        status=status, action=action, errors=errors, warnings=warnings, target_managed_asset_id=target_managed_asset_id,
    )
