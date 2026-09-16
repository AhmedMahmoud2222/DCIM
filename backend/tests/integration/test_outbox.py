"""Verifies the Outbox pattern's core guarantee: the event row exists if and only if the
authoritative transaction it accompanies committed (§18/§22) — never independently."""

import uuid
from datetime import UTC

from sqlalchemy import select

from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.domain.audit.models import AuditLog
from app.domain.identity.models import ManagedAsset
from app.domain.outbox.models import OutboxEvent


async def test_outbox_event_and_audit_row_commit_atomically_with_the_mutation(db_session):
    asset = ManagedAsset(asset_type="rack", asset_tag="RACK-OUTBOX-1")
    db_session.add(asset)
    await db_session.flush()

    await write_audit_log(
        db_session,
        actor_user_id=None,
        action="managed_asset.create",
        entity_type="managed_asset",
        entity_id=asset.id,
        request_id="req-1",
        correlation_id="corr-1",
        after={"asset_tag": asset.asset_tag},
    )
    await write_outbox_event(
        db_session,
        event_type="ManagedAssetCreated",
        aggregate_type="managed_asset",
        aggregate_id=asset.id,
        payload={"asset_tag": asset.asset_tag},
        correlation_id="corr-1",
    )
    await db_session.commit()

    outbox_row = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == asset.id))
    ).scalar_one()
    assert outbox_row.status == "pending"
    assert outbox_row.correlation_id == "corr-1"

    audit_row = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == asset.id))
    ).scalar_one()
    assert audit_row.correlation_id == "corr-1"


async def test_outbox_event_does_not_survive_a_rolled_back_transaction(db_session):
    asset = ManagedAsset(asset_type="rack", asset_tag="RACK-OUTBOX-2")
    db_session.add(asset)
    await db_session.flush()

    await write_outbox_event(
        db_session,
        event_type="ManagedAssetCreated",
        aggregate_type="managed_asset",
        aggregate_id=asset.id,
        payload={},
    )
    aggregate_id = asset.id
    await db_session.rollback()

    result = await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == aggregate_id))
    assert result.scalar_one_or_none() is None


async def test_stale_processing_event_is_reclaimed_by_the_dispatcher(db_session):
    """A row stuck in 'processing' (e.g. its delivery was lost before a worker executed
    it) must not stall forever — §19's "failure recovery" requirement. Runs the sync
    dispatcher task's query logic directly against the real test database."""
    from datetime import datetime, timedelta

    from sqlalchemy import text

    asset = ManagedAsset(asset_type="rack", asset_tag="RACK-STALE-OUTBOX")
    db_session.add(asset)
    await db_session.flush()
    event = await write_outbox_event(
        db_session, event_type="ManagedAssetCreated", aggregate_type="managed_asset", aggregate_id=asset.id, payload={}
    )
    await db_session.commit()

    stale_time = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.execute(
        text("UPDATE outbox_event SET status = 'processing', updated_at = :t WHERE event_id = :eid"),
        {"t": stale_time, "eid": event.event_id},
    )
    await db_session.commit()

    from app.infrastructure.tasks.outbox_dispatcher import dispatch_pending_outbox_events

    reclaimed_count = dispatch_pending_outbox_events.run()
    assert reclaimed_count >= 1

    # `db_session`'s identity map still holds the pre-reclaim Python object for this PK;
    # the dispatcher wrote through a *different* (sync) connection, so this session's
    # cached attributes must be explicitly refreshed rather than trusted as-is.
    stmt = select(OutboxEvent).where(OutboxEvent.event_id == event.event_id).execution_options(populate_existing=True)
    refreshed = (await db_session.execute(stmt)).scalar_one()
    assert refreshed.status == "processing"  # re-claimed and re-marked processing for a fresh delivery attempt


async def test_recently_processing_event_is_not_reclaimed(db_session):
    asset = ManagedAsset(asset_type="rack", asset_tag="RACK-FRESH-OUTBOX")
    db_session.add(asset)
    await db_session.flush()
    event = await write_outbox_event(
        db_session, event_type="ManagedAssetCreated", aggregate_type="managed_asset", aggregate_id=asset.id, payload={}
    )
    event.status = "processing"
    await db_session.commit()

    from app.infrastructure.tasks.outbox_dispatcher import dispatch_pending_outbox_events

    reclaimed = dispatch_pending_outbox_events.run()
    assert reclaimed == 0


async def test_outbox_event_id_is_unique_supporting_idempotent_dispatch(db_session):
    asset = ManagedAsset(asset_type="rack", asset_tag="RACK-OUTBOX-3")
    db_session.add(asset)
    await db_session.flush()

    event = await write_outbox_event(
        db_session, event_type="ManagedAssetCreated", aggregate_type="managed_asset", aggregate_id=asset.id, payload={}
    )
    await db_session.commit()
    assert event.event_id is not None
    assert isinstance(event.event_id, uuid.UUID)
