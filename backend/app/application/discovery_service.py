"""Discovery / reconciliation boundary (master prompt §7): `DiscoveredDevice` is never
authoritative. Nothing in this module ever writes to `managed_asset`, `rack`,
`equipment`, `pdu`, `ups`, `generator`, or `power_panel` -- the ONLY function in this
entire codebase permitted to set `DiscoveredDevice.matched_managed_asset_id` is
`accept_reconciliation`, and even that never creates or mutates the `ManagedAsset` row
itself; it only records that a human decided an already-existing, independently-created
`ManagedAsset` corresponds to this discovered device. Creating that `ManagedAsset` (if
one does not yet exist) remains the normal, audited `POST /api/v1/managed-assets` flow
-- reconciliation *links*, it never fabricates inventory.

Discovery flow (master prompt §7):
`Collector -> Integration -> Discovery -> DiscoveredDevice -> ReconciliationDiff ->
Human decision -> Normal audited inventory mutation`"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.network.neighbor_service import _clamp as normalize_observation_time
from app.application.network.profile_matching import DeviceFacts
from app.application.network.profile_service import resolve_profile
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import DiscoveredDevice, ReconciliationDiff

_FACT_FIELDS = ("sys_object_id", "sys_descr", "model", "hardware_revision", "firmware")
_FACT_MAX_LENGTH = 512
_DEVICE_UNIQUE_CONSTRAINT = "uq_discovered_device_integration_id_external_identifier"
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def extract_facts(raw_attributes: dict) -> DeviceFacts | None:
    """Device facts a collector reported under `raw_attributes["facts"]`. Non-string or
    oversized values are dropped rather than coerced; absent facts stay None."""
    facts = raw_attributes.get("facts")
    if not isinstance(facts, dict):
        return None
    cleaned = {
        name: facts[name]
        for name in _FACT_FIELDS
        if isinstance(facts.get(name), str) and 0 < len(facts[name]) <= _FACT_MAX_LENGTH
    }
    return DeviceFacts(**cleaned) if cleaned else None


async def _refresh_profile_match(db: AsyncSession, device: DiscoveredDevice, raw_attributes: dict) -> None:
    """Advisory only: records what the matcher concluded for the reported facts. Skipped
    when the facts are unchanged since the last evaluation. Never binds anything."""
    facts = extract_facts(raw_attributes)
    if facts is None:
        return
    snapshot = {name: getattr(facts, name) for name in _FACT_FIELDS if getattr(facts, name) is not None}
    if device.profile_match is not None and device.profile_match.get("facts") == snapshot:
        return
    result = await resolve_profile(db, facts)
    device.profile_match = {**result.as_dict(), "facts": snapshot, "evaluated_at": datetime.now(UTC).isoformat()}


def _is_device_key_conflict(exc: IntegrityError) -> bool:
    """True only for a unique-key collision on `(integration_id, external_identifier)`.
    Any other integrity failure (e.g. a foreign key on `integration_id`) is a real error
    and must propagate, never be converted into a recovery path."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if getattr(candidate, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
            name = getattr(candidate, "constraint_name", None)
            return name is None or name == _DEVICE_UNIQUE_CONSTRAINT
    return False


async def _lock_device(db: AsyncSession, integration_id: uuid.UUID, external_identifier: str) -> DiscoveredDevice | None:
    """`SELECT ... FOR UPDATE` on the one device row. `populate_existing` makes a session
    that already holds this instance refresh it from the just-locked (committed) row, so
    the ordering check always sees the state it is actually serialized against."""
    return (
        await db.execute(
            select(DiscoveredDevice)
            .where(
                DiscoveredDevice.integration_id == integration_id,
                DiscoveredDevice.external_identifier == external_identifier,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _apply_if_newer(
    db: AsyncSession, existing: DiscoveredDevice, *, seen: datetime, raw_attributes: dict,
) -> tuple[DiscoveredDevice, bool]:
    """Monotonic update of a row the caller holds `FOR UPDATE`. Strictly newer wins;
    older and equal observations are a complete no-op (equal timestamps: the first
    committed writer wins, so an exact replay of the newest record never inflates
    freshness)."""
    if seen > existing.last_seen_at:
        existing.last_seen_at = seen
        existing.raw_attributes = raw_attributes
        await _refresh_profile_match(db, existing, raw_attributes)
        await db.flush()
    return existing, False


async def ingest_discovery(
    db: AsyncSession, *, integration_id: uuid.UUID, external_identifier: str, raw_attributes: dict,
    correlation_id: str | None, causation_id: str | None, observed_at: datetime | None = None,
) -> tuple[DiscoveredDevice, bool]:
    """Upserts a `DiscoveredDevice` row keyed on `(integration_id, external_identifier)`.
    Returns `(device, is_new)`. Emits `DeviceDiscovered` (new) via the existing Outbox --
    never a second event bus.

    Observation-order contract (DISC-ORDER-01): `DiscoveredDevice.last_seen_at` is the
    normalized *observation* time of the newest accepted observation (not receipt time;
    `discovered_at` remains the first Central receipt time). `observed_at` is that time:
    collectors pass the record's `occurred_at`, central polling the driver's
    `AcquisitionResult.observed_at`; `None` means "now". Naive values are treated as UTC
    and values beyond `MAX_FUTURE_SKEW` ahead are clamped so a fast clock cannot pin a
    device against later observations. Old observations are legitimate and unbounded.

    An existing row is updated only if `observed_at > last_seen_at` (strict). An older or
    equal observation is a total no-op: `raw_attributes`, `profile_match`, `last_seen_at`,
    reconciliation state and the managed-asset association are untouched, and no events
    are produced. The existing row is read `FOR UPDATE`, so concurrent out-of-order
    deliveries serialize on it and converge on the newest observation regardless of
    arrival or commit order. A first-time insert runs inside a SAVEPOINT; if it loses a
    unique-key race, the winner's row is locked and the same monotonic rule applies, and
    only the creating call emits creation events. Non-duplicate integrity errors
    propagate unchanged.

    Transition note: rows written before this contract hold receipt time in
    `last_seen_at`, which can be slightly later than the true observation; a genuinely
    newer record that falls inside that gap and arrives out of order is dropped once and
    superseded by the next observation."""
    now = datetime.now(UTC)
    seen = normalize_observation_time(observed_at if observed_at is not None else now)

    existing = await _lock_device(db, integration_id, external_identifier)
    if existing is not None:
        return await _apply_if_newer(db, existing, seen=seen, raw_attributes=raw_attributes)

    device = DiscoveredDevice(
        id=uuid.uuid4(), integration_id=integration_id, external_identifier=external_identifier,
        discovered_at=now, last_seen_at=seen, raw_attributes=raw_attributes, status="new",
    )
    await _refresh_profile_match(db, device, raw_attributes)
    try:
        async with db.begin_nested():
            db.add(device)
            await db.flush()
    except IntegrityError as exc:
        if not _is_device_key_conflict(exc):
            raise
        winner = await _lock_device(db, integration_id, external_identifier)
        if winner is None:
            raise
        return await _apply_if_newer(db, winner, seen=seen, raw_attributes=raw_attributes)

    await write_outbox_event(
        db, event_type="DeviceDiscovered", aggregate_type="discovered_device", aggregate_id=device.id,
        payload={"integration_id": str(integration_id), "external_identifier": external_identifier},
        correlation_id=correlation_id, causation_id=causation_id,
    )

    diff = ReconciliationDiff(
        id=uuid.uuid4(), discovered_device_id=device.id, diff_type="new_device", status="pending",
    )
    db.add(diff)
    await db.flush()
    await write_outbox_event(
        db, event_type="ReconciliationRequired", aggregate_type="reconciliation_diff", aggregate_id=diff.id,
        payload={"discovered_device_id": str(device.id), "diff_type": "new_device"},
        correlation_id=correlation_id, causation_id=causation_id,
    )
    return device, True


async def accept_reconciliation(
    db: AsyncSession, *, diff_id: uuid.UUID, matched_managed_asset_id: uuid.UUID | None, actor_user_id: uuid.UUID,
    request_id: str | None, correlation_id: str | None, reason: str | None = None,
) -> ReconciliationDiff:
    """The ONE place `DiscoveredDevice.matched_managed_asset_id` may be set. Requires an
    ALREADY-EXISTING `ManagedAsset` (never creates one) -- linking to a nonexistent
    asset is rejected, not silently accepted as a dangling reference.

    Finding I3 (docs/history/PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): a plain `db.get()` followed by
    an app-level `if diff.status != "pending"` check does not serialize two concurrent
    decisions (e.g. accept and reject) on the same diff -- under READ COMMITTED both
    requests can read `status == "pending"` before either commits, so both proceed,
    both write a "the decision" audit entry, and whichever commits last silently
    overwrites the other's terminal state. `SELECT ... FOR UPDATE` (same pattern as the
    existing Phase 3 F-C1 concurrency fix elsewhere in this codebase) makes the row lock
    the point of serialization instead: the second concurrent request blocks on this
    query until the first request's transaction commits (releasing the lock), then sees
    the now-committed non-"pending" status and gets a clean 409 -- no schema change, no
    new column, this is a pure locking-strategy fix."""
    diff = (
        await db.execute(select(ReconciliationDiff).where(ReconciliationDiff.id == diff_id).with_for_update())
    ).scalar_one_or_none()
    if diff is None:
        raise NotFoundError(f"ReconciliationDiff {diff_id} not found.")
    if diff.status != "pending":
        raise ApiError(status_code=409, title="Already Decided", detail=f"This diff was already {diff.status}.")

    device = await db.get(DiscoveredDevice, diff.discovered_device_id)
    assert device is not None  # FK guarantees this

    if matched_managed_asset_id is not None:
        asset = await db.get(ManagedAsset, matched_managed_asset_id)
        if asset is None:
            raise NotFoundError(f"ManagedAsset {matched_managed_asset_id} not found -- cannot link to a nonexistent asset.")
        device.matched_managed_asset_id = matched_managed_asset_id
        device.status = "reconciled"
    else:
        device.status = "ignored"

    diff.status = "accepted"
    diff.decided_by_user_id = actor_user_id
    diff.decided_at = datetime.now(UTC)
    diff.reason = reason

    linked_asset_id = str(matched_managed_asset_id) if matched_managed_asset_id else None
    await write_audit_log(
        db, actor_user_id=actor_user_id, action="reconciliation.accept", entity_type="reconciliation_diff",
        entity_id=diff.id, request_id=request_id, correlation_id=correlation_id,
        before={"status": "pending"},
        after={"status": "accepted", "matched_managed_asset_id": linked_asset_id},
    )
    await db.flush()
    return diff


async def reject_reconciliation(
    db: AsyncSession, *, diff_id: uuid.UUID, actor_user_id: uuid.UUID, request_id: str | None,
    correlation_id: str | None, reason: str | None = None,
) -> ReconciliationDiff:
    """Finding I3 (docs/history/PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): same `SELECT ... FOR UPDATE`
    fix as `accept_reconciliation` -- see that function's docstring for the full
    rationale. The row lock is what serializes a concurrent accept-vs-reject (or
    reject-vs-reject) race on the same diff, not the `status` check by itself."""
    diff = (
        await db.execute(select(ReconciliationDiff).where(ReconciliationDiff.id == diff_id).with_for_update())
    ).scalar_one_or_none()
    if diff is None:
        raise NotFoundError(f"ReconciliationDiff {diff_id} not found.")
    if diff.status != "pending":
        raise ApiError(status_code=409, title="Already Decided", detail=f"This diff was already {diff.status}.")

    diff.status = "rejected"
    diff.decided_by_user_id = actor_user_id
    diff.decided_at = datetime.now(UTC)
    diff.reason = reason

    await write_audit_log(
        db, actor_user_id=actor_user_id, action="reconciliation.reject", entity_type="reconciliation_diff",
        entity_id=diff.id, request_id=request_id, correlation_id=correlation_id,
        before={"status": "pending"}, after={"status": "rejected"}, reason=reason,
    )
    await db.flush()
    return diff
