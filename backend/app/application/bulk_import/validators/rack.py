"""Rack bulk-import row validator. See resolvers.py for the shared lookup helpers and
commit/rack.py's module docstring for why resolution is re-run (not trusted) at commit
time — this validator exists to give the operator an accurate preview/report, not to
hand commit anything it can skip re-checking."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.bulk_import.resolvers import (
    RowRejected,
    cell_str,
    optional_int,
    require_str,
    resolve_managed_asset_by_tag,
    resolve_rack_model_revision,
    resolve_room,
)
from app.application.bulk_import.validators import BatchState, RowValidationResult, error
from app.application.spatial_validation import MAX_COORDINATE_MM, MIN_COORDINATE_MM
from app.domain.physical.models import Rack


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

    for field_name in ("rack_name", "manufacturer", "model_name", "site_code", "building_code", "room_code"):
        try:
            require_str(raw.get(field_name), field_name)
        except RowRejected as exc:
            errors.append(error(exc.field, exc.message))

    try:
        floor_level = optional_int(raw.get("floor_level"), "floor_level")
        if floor_level is None:
            errors.append(error("floor_level", "floor_level is required."))
    except RowRejected as exc:
        errors.append(error(exc.field, exc.message))
        floor_level = None

    for coord_field in ("x_mm", "y_mm"):
        try:
            value = optional_int(raw.get(coord_field), coord_field)
            if value is not None and not (MIN_COORDINATE_MM <= value <= MAX_COORDINATE_MM):
                errors.append(error(coord_field, f"{coord_field} must be within [{MIN_COORDINATE_MM}, {MAX_COORDINATE_MM}]."))
        except RowRejected as exc:
            errors.append(error(exc.field, exc.message))

    try:
        rotation_deg = optional_int(raw.get("rotation_deg"), "rotation_deg")
        if rotation_deg is not None and not (0 <= rotation_deg < 360):
            errors.append(error("rotation_deg", f"rotation_deg must be in [0, 360), got {rotation_deg}."))
    except RowRejected as exc:
        errors.append(error(exc.field, exc.message))

    target_managed_asset_id = None
    expected_version = None
    action = "create"
    if mode == "create_only":
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is not None:
            errors.append(error("asset_tag", f"asset_tag {asset_tag!r} already exists (create_only mode)."))
    else:
        action = "update"
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is None:
            errors.append(error("asset_tag", f"No existing rack with asset_tag {asset_tag!r} to update."))
        elif existing.asset_type != "rack":
            errors.append(error("asset_tag", f"asset_tag {asset_tag!r} does not identify a rack."))
        else:
            target_managed_asset_id = existing.id
            # SEC (Codex PR #50 review, finding #5): snapshot Rack.version now, at the
            # moment this row's target is resolved, so commit can detect a concurrent
            # edit landing between preview and commit instead of always comparing a
            # freshly re-fetched version against itself.
            existing_rack = await db.get(Rack, existing.id)
            if existing_rack is not None:
                expected_version = existing_rack.version

    manufacturer = cell_str(raw.get("manufacturer"))
    model_name = cell_str(raw.get("model_name"))
    if manufacturer and model_name:
        try:
            revision_number = optional_int(raw.get("revision_number"), "revision_number")
            await resolve_rack_model_revision(
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
        expected_version=expected_version,
    )
