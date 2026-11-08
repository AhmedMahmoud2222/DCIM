"""Locked close-then-open placement transaction (ARCHITECTURE_REVIEW.md §7c/§8) — the
mechanism protecting RackPlacement/EquipmentPlacement against concurrent moves of the
same asset. Identical pattern applied to both tables:

    SELECT current WHERE effective_to IS NULL FOR UPDATE  -- blocks a concurrent mover
    -- zero rows after unblock (EvalPlanQual re-evaluates WHERE post-commit under READ
    -- COMMITTED) => the row we meant to move was already closed by someone else => conflict
    UPDATE current SET effective_to = now()
    INSERT new row

A second workflow *retiring* an already-retired asset is an idempotent no-op success,
not a 409 (§7c) — both are decommissioning it, so finding it already gone achieves the
caller's intent. A second workflow attempting a *move* still surfaces the conflict via
`PlacementConflict`, since the caller's intended destination might not match reality."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authority_lock import acquire_placement_scope_lock
from app.domain.placement.models import EquipmentPlacement, RackPlacement


async def _lock_asset_placement(db: AsyncSession, kind: str, asset_id: uuid.UUID) -> None:
    """Serialises every placement writer of ONE asset (transaction-scoped advisory lock).

    Version generation looks at the asset's whole placement history, including the case where no row is current
    (first placement, re-placement after an unplace). Row locks on the current row cannot serialise those, because
    there may be no current row to lock, so two concurrent first-placements would both read "no history" and both
    allocate the same generation. Taking this lock before reading the history makes `max(version) + 1` safe.

    Lock order: AUTHORITY(S) (racks only, see authority_lock.py) -> this lock -> placement row locks. Nothing else
    waits for this lock while holding a row lock another placement writer needs."""
    key = f"dcim.placement.{kind}.{asset_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


@dataclass
class PlacementConflict(Exception):
    """Raised when the row this caller expected to be current is no longer current —
    either a genuine concurrent race (another mover won) or a stale client-supplied
    If-Match. `current` (possibly None) is the actual current state, for the caller to
    surface in a 409 response body."""

    current: RackPlacement | EquipmentPlacement | None


def _require_unchanged_while_waiting(
    observed: RackPlacement | EquipmentPlacement | None, current: RackPlacement | EquipmentPlacement | None
) -> None:
    """A mover decided what to do from the placement it observed. If another writer replaced, created or closed that
    placement while this one waited for the per-asset lock, the decision is stale: the loser gets a conflict
    (ARCHITECTURE_REVIEW.md 7c, "zero rows after unblock") instead of silently stacking a second move on top. The
    same rule covers two concurrent first placements and a concurrent re-placement."""
    if (observed.id if observed else None) != (current.id if current else None):
        raise PlacementConflict(current=current)


async def _next_version(
    db: AsyncSession, model: type[RackPlacement] | type[EquipmentPlacement], key_col: Any, asset_id: uuid.UUID
) -> int:
    """Highest version ever issued to this asset (closed rows included) plus one. Callers hold `_lock_asset_placement`."""
    highest = (await db.execute(select(func.max(model.version)).where(key_col == asset_id))).scalar_one_or_none()
    return (highest or 0) + 1


# --------------------------------------------------------------------------- Rack


async def get_current_rack_placement(db: AsyncSession, rack_id: uuid.UUID) -> RackPlacement | None:
    stmt = select(RackPlacement).where(RackPlacement.rack_id == rack_id, RackPlacement.effective_to.is_(None))
    return (await db.execute(stmt)).scalar_one_or_none()


async def move_rack(
    db: AsyncSession,
    *,
    rack_id: uuid.UUID,
    room_id: uuid.UUID,
    x_mm: int | None = None,
    y_mm: int | None = None,
    rotation_deg: int | None = None,
    if_match_version: int | None = None,
) -> RackPlacement:
    """Opens the first placement if none exists yet; otherwise closes the current one and
    opens a new one, atomically. Raises PlacementConflict if `if_match_version` is given
    and doesn't match, or if the row believed current was already closed by a concurrent
    request.

    Takes the shared authority/placement lock first (see authority_lock.py): the site a rack
    sits in is part of every site-restricted user's data scope, so this write must not commit
    while a delegated-administration decision that read the old placement is still open."""
    await acquire_placement_scope_lock(db)
    observed = await get_current_rack_placement(db, rack_id)  # what this caller decided to change, read BEFORE any wait
    await _lock_asset_placement(db, "rack", rack_id)
    current = await get_current_rack_placement(db, rack_id)
    _require_unchanged_while_waiting(observed, current)
    if current is None and if_match_version is not None:
        # A token can only describe a placement that exists. Unplaced + a token means the token comes from an earlier
        # generation (or never existed): it must not be honoured, whatever number it carries.
        raise PlacementConflict(current=None)
    if current is not None:
        locked = (
            await db.execute(
                select(RackPlacement)
                .where(RackPlacement.id == current.id, RackPlacement.effective_to.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            raise PlacementConflict(current=await get_current_rack_placement(db, rack_id))
        if if_match_version is not None and locked.version != if_match_version:
            raise PlacementConflict(current=locked)
        locked.effective_to = datetime.now(UTC)
        await db.flush()
    # Placement versions are strictly monotonic per rack across its WHOLE history (not per row, not per current
    # row): a fresh row restarting at 1, after a move or after an unplace, would let a stale If-Match (or an
    # import's placement_version) captured earlier match the *new* row (ABA) and silently move the rack back.
    next_version = await _next_version(db, RackPlacement, RackPlacement.rack_id, rack_id)

    new_placement = RackPlacement(
        rack_id=rack_id,
        room_id=room_id,
        x_mm=x_mm,
        y_mm=y_mm,
        rotation_deg=rotation_deg,
        version=next_version,
        effective_from=datetime.now(UTC),
        effective_to=None,
    )
    db.add(new_placement)
    await db.flush()
    return new_placement


async def retire_rack_placement(
    db: AsyncSession, *, rack_id: uuid.UUID, if_match_version: int | None = None
) -> RackPlacement | None:
    """Closes the current placement with no replacement. Returns None (idempotent no-op)
    if the rack was already unplaced — by this caller's prior attempt or a concurrent
    one — never a 409 for that case. Takes the same shared lock as `move_rack`."""
    await acquire_placement_scope_lock(db)
    await _lock_asset_placement(db, "rack", rack_id)
    current = await get_current_rack_placement(db, rack_id)
    if current is None:
        return None
    locked = (
        await db.execute(
            select(RackPlacement)
            .where(RackPlacement.id == current.id, RackPlacement.effective_to.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        return None  # someone else already retired/moved it — retiring intent is satisfied
    if if_match_version is not None and locked.version != if_match_version:
        raise PlacementConflict(current=locked)
    locked.effective_to = datetime.now(UTC)
    await db.flush()
    return locked


# --------------------------------------------------------------------- Equipment


async def get_current_equipment_placement(db: AsyncSession, equipment_id: uuid.UUID) -> EquipmentPlacement | None:
    stmt = select(EquipmentPlacement).where(
        EquipmentPlacement.equipment_id == equipment_id, EquipmentPlacement.effective_to.is_(None)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def move_equipment(
    db: AsyncSession,
    *,
    equipment_id: uuid.UUID,
    placement_type: str,
    room_id: uuid.UUID,
    rack_id: uuid.UUID | None = None,
    u_start: int | None = None,
    u_end: int | None = None,
    side: str | None = None,
    rotation_deg: int | None = None,
    mounting_method: str | None = None,
    orientation: str | None = None,
    if_match_version: int | None = None,
    x_mm: int | None = None,
    y_mm: int | None = None,
    position_calibration_id: uuid.UUID | None = None,
) -> EquipmentPlacement:
    """`x_mm` / `y_mm` (Issue #105) carry a room-local position for assets such as environmental sensors and cooling
    units that have no drawn SpatialObject; each close-then-open row keeps its own coordinates."""
    observed = await get_current_equipment_placement(db, equipment_id)  # read BEFORE any wait, see move_rack
    await _lock_asset_placement(db, "equipment", equipment_id)
    current = await get_current_equipment_placement(db, equipment_id)
    _require_unchanged_while_waiting(observed, current)
    if current is None and if_match_version is not None:
        raise PlacementConflict(current=None)  # see move_rack: a token cannot describe a placement that does not exist
    if current is not None:
        locked = (
            await db.execute(
                select(EquipmentPlacement)
                .where(EquipmentPlacement.id == current.id, EquipmentPlacement.effective_to.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            raise PlacementConflict(current=await get_current_equipment_placement(db, equipment_id))
        if if_match_version is not None and locked.version != if_match_version:
            raise PlacementConflict(current=locked)
        locked.effective_to = datetime.now(UTC)
        await db.flush()
    next_version = await _next_version(db, EquipmentPlacement, EquipmentPlacement.equipment_id, equipment_id)

    u_range = Range(u_start, u_end, bounds="[)") if u_start is not None and u_end is not None else None
    new_placement = EquipmentPlacement(
        equipment_id=equipment_id,
        placement_type=placement_type,
        room_id=room_id,
        rack_id=rack_id,
        u_range=u_range,
        side=side,
        rotation_deg=rotation_deg,
        mounting_method=mounting_method,
        orientation=orientation,
        x_mm=x_mm,
        y_mm=y_mm,
        position_calibration_id=position_calibration_id,
        version=next_version,
        effective_from=datetime.now(UTC),
        effective_to=None,
    )
    db.add(new_placement)
    await db.flush()
    return new_placement


async def retire_equipment_placement(
    db: AsyncSession, *, equipment_id: uuid.UUID, if_match_version: int | None = None
) -> EquipmentPlacement | None:
    await _lock_asset_placement(db, "equipment", equipment_id)
    current = await get_current_equipment_placement(db, equipment_id)
    if current is None:
        return None
    locked = (
        await db.execute(
            select(EquipmentPlacement)
            .where(EquipmentPlacement.id == current.id, EquipmentPlacement.effective_to.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        return None
    if if_match_version is not None and locked.version != if_match_version:
        raise PlacementConflict(current=locked)
    locked.effective_to = datetime.now(UTC)
    await db.flush()
    return locked
