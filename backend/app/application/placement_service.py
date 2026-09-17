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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.placement.models import EquipmentPlacement, RackPlacement


@dataclass
class PlacementConflict(Exception):
    """Raised when the row this caller expected to be current is no longer current —
    either a genuine concurrent race (another mover won) or a stale client-supplied
    If-Match. `current` (possibly None) is the actual current state, for the caller to
    surface in a 409 response body."""

    current: RackPlacement | EquipmentPlacement | None


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
    request."""
    current = await get_current_rack_placement(db, rack_id)
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

    new_placement = RackPlacement(
        rack_id=rack_id,
        room_id=room_id,
        x_mm=x_mm,
        y_mm=y_mm,
        rotation_deg=rotation_deg,
        version=1,
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
    one — never a 409 for that case."""
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
) -> EquipmentPlacement:
    current = await get_current_equipment_placement(db, equipment_id)
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
        version=1,
        effective_from=datetime.now(UTC),
        effective_to=None,
    )
    db.add(new_placement)
    await db.flush()
    return new_placement


async def retire_equipment_placement(
    db: AsyncSession, *, equipment_id: uuid.UUID, if_match_version: int | None = None
) -> EquipmentPlacement | None:
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
