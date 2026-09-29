"""Rack bulk-import commit. Deliberately re-resolves manufacturer/model/room from
`row.raw_data` rather than trusting anything cached from the validate pass (other than
the cheap `target_managed_asset_id` the validator already persisted for update mode,
which only saves a lookup — commit still verifies the asset is actually a rack). A
meaningful amount of time, and possibly other commits, can pass between validate and
commit, so re-resolution is what lets a genuinely stale reference surface as a clean
row-level failure (via `RowRejected`, or a caught `IntegrityError`/`PlacementConflict`)
instead of a wrong write.

Mirrors `POST /racks` (app/api/v1/racks.py) for the ManagedAsset+Rack construction and
`app/application/placement_service.py::move_rack` for placement — see those modules for
the exact contracts being reused here, not re-derived."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.bulk_import.commit import RowCommitResult
from app.application.bulk_import.resolvers import (
    RowRejected,
    optional_int,
    require_int,
    require_str,
    resolve_managed_asset_by_tag,
    resolve_rack_model_revision,
    resolve_room,
)
from app.application.outbox_service import write_outbox_event
from app.application.placement_service import PlacementConflict, get_current_rack_placement, move_rack
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Rack


async def commit_row(db: AsyncSession, *, job: BulkImportJob, row: BulkImportRow, mode: str) -> RowCommitResult:
    raw = row.raw_data
    asset_tag = require_str(raw.get("asset_tag"), "asset_tag")
    rack_name = require_str(raw.get("rack_name"), "rack_name")
    manufacturer = require_str(raw.get("manufacturer"), "manufacturer")
    model_name = require_str(raw.get("model_name"), "model_name")
    revision_number = optional_int(raw.get("revision_number"), "revision_number")
    site_code = require_str(raw.get("site_code"), "site_code")
    building_code = require_str(raw.get("building_code"), "building_code")
    floor_level = require_int(raw.get("floor_level"), "floor_level")
    room_code = require_str(raw.get("room_code"), "room_code")
    x_mm = optional_int(raw.get("x_mm"), "x_mm")
    y_mm = optional_int(raw.get("y_mm"), "y_mm")
    rotation_deg = optional_int(raw.get("rotation_deg"), "rotation_deg")
    owner = raw.get("owner") or None
    notes = raw.get("notes") or None

    model_revision = await resolve_rack_model_revision(
        db, manufacturer=manufacturer, model_name=model_name, revision_number=revision_number
    )
    room = await resolve_room(db, site_code=site_code, building_code=building_code, floor_level=floor_level, room_code=room_code)

    if mode == "create_only":
        existing = await resolve_managed_asset_by_tag(db, asset_tag)
        if existing is not None:
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} already exists (create_only mode).")
        asset = ManagedAsset(asset_type="rack", asset_tag=asset_tag, lifecycle_status="planned")
        db.add(asset)
        await db.flush()
        rack = Rack(id=asset.id, model_revision_id=model_revision.id, name=rack_name, owner=owner, notes=notes)
        db.add(rack)
        await db.flush()
        action_code = "rack.bulk_import.create"
        event_type = "RackCreated"
    else:
        loaded_asset = await db.get(ManagedAsset, row.target_managed_asset_id) if row.target_managed_asset_id else None
        if loaded_asset is None:
            loaded_asset = await resolve_managed_asset_by_tag(db, asset_tag)
        if loaded_asset is None:
            raise RowRejected("asset_tag", f"No existing rack with asset_tag {asset_tag!r} to update.")
        if loaded_asset.asset_type != "rack":
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} does not identify a rack.")
        asset = loaded_asset
        loaded_rack = await db.get(Rack, asset.id)
        if loaded_rack is None:
            raise RowRejected("asset_tag", f"asset_tag {asset_tag!r} has no rack row.")
        rack = loaded_rack
        # SEC (Codex PR #50 review, finding #5): row.expected_version snapshots
        # rack.version as it was at validate time (validators/rack.py); a live mismatch
        # here means the rack was edited by something else between preview and commit —
        # reject the row rather than silently overwriting/losing that concurrent edit.
        # None means either a create-mode row (never reaches this branch) or a row whose
        # target wasn't actually resolved at validate time — nothing to compare against,
        # so no check is possible for that row (matches this pipeline's pre-existing
        # unconditional-write behavior for such rows).
        if row.expected_version is not None and rack.version != row.expected_version:
            raise RowRejected(
                "asset_tag", "This record changed since it was previewed — re-validate and retry."
            )
        # Mutable fields only — model_revision_id is deliberately never patched here
        # (a bulk update must not silently re-home an existing rack onto a different
        # catalog model/revision).
        rack.name = rack_name
        if owner is not None:
            rack.owner = owner
        if notes is not None:
            rack.notes = notes
        rack.version += 1
        await db.flush()
        action_code = "rack.bulk_import.update"
        event_type = "RackUpdated"

    current_placement = await get_current_rack_placement(db, asset.id)
    needs_move = (
        current_placement is None
        or current_placement.room_id != room.id
        or (x_mm is not None and current_placement.x_mm != x_mm)
        or (y_mm is not None and current_placement.y_mm != y_mm)
        or (rotation_deg is not None and current_placement.rotation_deg != rotation_deg)
    )
    if needs_move:
        try:
            await move_rack(db, rack_id=asset.id, room_id=room.id, x_mm=x_mm, y_mm=y_mm, rotation_deg=rotation_deg)
        except PlacementConflict as exc:
            raise RowRejected("room_code", "Rack placement was changed concurrently; could not place this row.") from exc

    await write_audit_log(
        db, actor_user_id=job.uploaded_by_user_id, action=action_code, entity_type="rack", entity_id=asset.id,
        request_id=None, correlation_id=str(job.id),
        after={"asset_tag": asset_tag, "name": rack_name, "bulk_import_job_id": str(job.id)},
    )
    await write_outbox_event(
        db, event_type=event_type, aggregate_type="rack", aggregate_id=asset.id,
        payload={"asset_tag": asset_tag, "bulk_import_job_id": str(job.id)}, correlation_id=str(job.id),
    )

    return RowCommitResult(status="committed", target_managed_asset_id=asset.id)
