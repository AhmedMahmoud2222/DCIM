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
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.core.errors import ApiError, NotFoundError
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import DiscoveredDevice, ReconciliationDiff


async def ingest_discovery(
    db: AsyncSession, *, integration_id: uuid.UUID, external_identifier: str, raw_attributes: dict,
    correlation_id: str | None, causation_id: str | None,
) -> tuple[DiscoveredDevice, bool]:
    """Upserts a `DiscoveredDevice` row keyed on `(integration_id, external_identifier)`
    -- the unique constraint on that pair means a device seen again just updates
    `last_seen_at`/`raw_attributes`, never creates a duplicate row. Returns
    `(device, is_new)`. Emits `DeviceDiscovered` (new) via the existing Outbox -- never
    a second event bus."""
    now = datetime.now(UTC)
    existing = (
        await db.execute(
            select(DiscoveredDevice).where(
                DiscoveredDevice.integration_id == integration_id,
                DiscoveredDevice.external_identifier == external_identifier,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.last_seen_at = now
        existing.raw_attributes = raw_attributes
        await db.flush()
        return existing, False

    device = DiscoveredDevice(
        id=uuid.uuid4(), integration_id=integration_id, external_identifier=external_identifier,
        discovered_at=now, last_seen_at=now, raw_attributes=raw_attributes, status="new",
    )
    db.add(device)
    await db.flush()

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
    asset is rejected, not silently accepted as a dangling reference."""
    diff = await db.get(ReconciliationDiff, diff_id)
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
    diff = await db.get(ReconciliationDiff, diff_id)
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
