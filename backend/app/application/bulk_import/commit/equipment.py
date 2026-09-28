"""Equipment bulk-import commit. Same re-resolution discipline as commit/rack.py's
module docstring describes. Mirrors `POST /equipment` (app/api/v1/equipment.py) for the
ManagedAsset+Equipment construction, pre-checks U-range capacity with
`app/application/spatial_validation.py::validate_u_range_against_rack_capacity` before
calling `app/application/placement_service.py::move_equipment` — the same order
`move_equipment_endpoint` uses — and relies on the database's GiST exclusion
constraint (via a caught `IntegrityError`) as the authoritative guard against a genuine
front/rear overlap that the cheap capacity pre-check cannot see (two different rows in
the same file targeting the same U range)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.bulk_import.commit import RowCommitResult
from app.application.bulk_import.resolvers import (
    RowRejected,
    cell_str,
    optional_int,
    require_int,
    require_str,
    resolve_equipment_model_revision,
    resolve_managed_asset_by_tag,
    resolve_rack_by_asset_tag,
    resolve_room,
)
from app.application.outbox_service import write_outbox_event
from app.application.placement_service import PlacementConflict, get_current_equipment_placement, move_equipment
from app.application.spatial_validation import validate_u_range_against_rack_capacity
from app.core.errors import ApiError
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.domain.catalog.models import RackModelRevision
from app.domain.identity.models import LIFECYCLE_STATUSES, ManagedAsset
from app.domain.physical.models import Equipment


async def commit_row(db: AsyncSession, *, job: BulkImportJob, row: BulkImportRow, mode: str) -> RowCommitResult:
    raw = row.raw_data
    asset_tag = require_str(raw.get("asset_tag"), "asset_tag")
    manufacturer = require_str(raw.get("manufacturer"), "manufacturer")
    model_name = require_str(raw.get("model_name"), "model_name")
    revision_number = optional_int(raw.get("revision_number"), "revision_number")
    placement_type = require_str(raw.get("placement_type"), "placement_type")
    site_code = require_str(raw.get("site_code"), "site_code")
    building_code = require_str(raw.get("building_code"), "building_code")
    floor_level = require_int(raw.get("floor_level"), "floor_level")
    room_code = require_str(raw.get("room_code"), "room_code")
    hostname = cell_str(raw.get("hostname"))
    ip_address = cell_str(raw.get("ip_address"))
    owner = cell_str(raw.get("owner"))
    service = cell_str(raw.get("service"))
    environment = cell_str(raw.get("environment"))
    notes = cell_str(raw.get("notes"))
    lifecycle_status = cell_str(raw.get("lifecycle_status")) or "planned"
    if lifecycle_status not in LIFECYCLE_STATUSES:
        raise RowRejected("lifecycle_status", f"lifecycle_status must be one of {LIFECYCLE_STATUSES}.")

    model_revision = await resolve_equipment_model_revision(
        db, manufacturer=manufacturer, model_name=model_name, revision_number=revision_number
    )
    room = await resolve_room(db, site_code=site_code, building_code=building_code, floor_level=floor_level, room_code=room_code)

    rack_asset = rack = None
    u_start = u_end = None
    side = cell_str(raw.get("side"))
    if placement_type == "rack_mounted":
        rack_asset_tag = require_str(raw.get("rack_asset_tag"), "rack_asset_tag")
        u_start = require_int(raw.get("u_start"), "u_start")
        u_end = require_int(raw.get("u_end"), "u_end")
        if side is None:
            raise RowRejected("side", "side is required when placement_type=rack_mounted.")
        rack_asset, rack = await resolve_rack_by_asset_tag(db, rack_asset_tag)
        rack_model_revision = await db.get(RackModelRevision, rack.model_revision_id)
        assert rack_model_revision is not None
        try:
            validate_u_range_against_rack_capacity(u_start=u_start, u_end=u_end, rack_height_u=rack_model_revision.height_u)
        except ApiError as exc:
            raise RowRejected("u_start", exc.detail) from exc

    if mode == "create_only":
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is not None:
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} already exists (create_only mode).")
        asset = ManagedAsset(asset_type="equipment", asset_tag=asset_tag, lifecycle_status=lifecycle_status)
        db.add(asset)
        await db.flush()
        equipment = Equipment(
            id=asset.id, model_revision_id=model_revision.id, hostname=hostname, ip_address=ip_address, owner=owner,
            service=service, environment=environment, notes=notes,
        )
        db.add(equipment)
        await db.flush()
        action_code = "equipment.bulk_import.create"
        event_type = "EquipmentCreated"
    else:
        loaded_asset = await db.get(ManagedAsset, row.target_managed_asset_id) if row.target_managed_asset_id else None
        if loaded_asset is None:
            loaded_asset = await resolve_managed_asset_by_tag(db, asset_tag)
        if loaded_asset is None:
            raise RowRejected("asset_tag", f"No existing equipment with asset_tag {asset_tag!r} to update.")
        if loaded_asset.asset_type != "equipment":
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} does not identify equipment.")
        asset = loaded_asset
        loaded_equipment = await db.get(Equipment, asset.id)
        if loaded_equipment is None:
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} has no equipment row.")
        equipment = loaded_equipment
        # Mutable fields only — model_revision_id is never patched here, matching
        # commit/rack.py's own "never re-home onto a different catalog model" rule.
        if hostname is not None:
            equipment.hostname = hostname
        if owner is not None:
            equipment.owner = owner
        if service is not None:
            equipment.service = service
        if environment is not None:
            equipment.environment = environment
        if notes is not None:
            equipment.notes = notes
        equipment.version += 1
        await db.flush()
        action_code = "equipment.bulk_import.update"
        event_type = "EquipmentUpdated"

    current_placement = await get_current_equipment_placement(db, asset.id)
    needs_move = (
        current_placement is None
        or current_placement.placement_type != placement_type
        or current_placement.room_id != room.id
        or current_placement.rack_id != (rack_asset.id if rack_asset is not None else None)
    )
    if not needs_move and placement_type == "rack_mounted" and current_placement is not None:
        u_range = current_placement.u_range
        needs_move = u_range is None or u_range.lower != u_start or u_range.upper != u_end or current_placement.side != side

    if needs_move:
        try:
            await move_equipment(
                db, equipment_id=asset.id, placement_type=placement_type, room_id=room.id,
                rack_id=rack_asset.id if rack_asset is not None else None, u_start=u_start, u_end=u_end, side=side,
            )
        except PlacementConflict as exc:
            raise RowRejected("room_code", "Equipment placement was changed concurrently; could not place this row.") from exc

    await write_audit_log(
        db, actor_user_id=job.uploaded_by_user_id, action=action_code, entity_type="equipment", entity_id=asset.id,
        request_id=None, correlation_id=str(job.id),
        after={"asset_tag": asset_tag, "hostname": hostname, "bulk_import_job_id": str(job.id)},
    )
    await write_outbox_event(
        db, event_type=event_type, aggregate_type="equipment", aggregate_id=asset.id,
        payload={"asset_tag": asset_tag, "bulk_import_job_id": str(job.id)}, correlation_id=str(job.id),
    )

    return RowCommitResult(status="committed", target_managed_asset_id=asset.id)
