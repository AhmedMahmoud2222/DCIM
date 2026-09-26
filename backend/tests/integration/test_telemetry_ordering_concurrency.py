"""Genuine concurrency tests for Phase 10C's cached latest-status ordering guard
(`app/application/telemetry_service.py`'s `record_latest_status`). Follows
tests/integration/test_phase8_concurrency.py's own pattern rather than simulating a race
sequentially: the shared `db_session` fixture binds every call to ONE connection, which
can never exercise two pollers ingesting for the same binding at once. Each concurrent
ingest here gets its own AsyncSession on its own connection from a dedicated engine,
against the same real PostgreSQL database the rest of the suite uses.

What these lock in is the property a read-then-write in Python cannot provide: when N
samples for one binding are ingested concurrently in arbitrary order, the row that
survives is the one with the greatest `sampled_at`, never merely the one that committed
last."""

import asyncio
import random
import uuid
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.catalog_designer_service import publish_revision
from app.application.equipment_instantiation_service import instantiate_equipment, list_equipment_ports
from app.application.telemetry_service import create_port_telemetry_binding, record_latest_status
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import (
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    NetworkPortTemplate,
)
from app.domain.telemetry.mapping_models import TelemetryLatestStatus
from tests.conftest import TEST_DATABASE_URL

BASE_SAMPLED_AT = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def concurrent_sessions():
    """A pool wide enough that every concurrent ingest below holds its own real
    connection simultaneously — with a narrower pool the ingests would queue and the
    test would silently degrade into the sequential case it exists to rule out."""
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=25, max_overflow=10)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def _seed_binding(db_session):
    manufacturer = Manufacturer(name=f"Acme-{uuid.uuid4().hex[:8]}")
    db_session.add(manufacturer)
    await db_session.flush()
    model = CatalogModel(
        manufacturer_id=manufacturer.id, category="equipment", model_name=f"Server-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(model)
    await db_session.flush()
    user = User(email=f"svc-{uuid.uuid4().hex[:8]}@test.local", full_name="Service", password_hash=hash_password("x"))
    db_session.add(user)
    await db_session.flush()
    revision = CatalogModelRevision(
        catalog_model_id=model.id, revision_number=1, lifecycle_status="draft",
        dimension_unit="mm", width_value=440, height_value=44.45, depth_value=600,
        weight_unit="kg", weight_value=10, rack_unit_height=1,
        supported_placement_types=["rack_mounted"], created_by_user_id=user.id,
    )
    db_session.add(revision)
    await db_session.flush()
    db_session.add(
        NetworkPortTemplate(
            catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
            supported_speeds_mbps=[1000], connector_type="rj45", side="rear", sort_order=0,
        )
    )
    await db_session.flush()
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user.id)
    await db_session.commit()
    equipment = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id,
        hostname="srv-1", ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    await db_session.commit()
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
        equipment_power_inlet_id=None, protocol="snmp", external_ref="1.3.6.1.2.1.2.2.1.8.1", label=None,
    )
    await db_session.commit()
    return binding


async def _ingest_in_own_session(session_factory, *, binding_id, link_state, sampled_at, offset_seconds):
    async with session_factory() as session:
        await record_latest_status(
            session,
            binding_id=binding_id,
            payload={"link_state": link_state, "bandwidth_util_pct": 10.0,
                     "error_rate_pct": 0.0, "sequence": offset_seconds},
            sampled_at=sampled_at,
        )
        await session.commit()


async def _stored(db_session, binding_id) -> TelemetryLatestStatus:
    row = (
        await db_session.execute(select(TelemetryLatestStatus).where(TelemetryLatestStatus.binding_id == binding_id))
    ).scalar_one()
    await db_session.refresh(row)
    return row


async def test_concurrent_out_of_order_samples_leave_the_newest_stored(db_session, concurrent_sessions):
    """Twelve samples for one binding, each on its own connection, dispatched in shuffled
    order so the commit order is genuinely not the sample order. Exactly one of them —
    the sample with the greatest `sampled_at` — must be the stored row afterwards."""
    binding = await _seed_binding(db_session)
    offsets = list(range(12))
    random.shuffle(offsets)
    newest_offset = max(offsets)

    await asyncio.gather(
        *[
            _ingest_in_own_session(
                concurrent_sessions,
                binding_id=binding.id,
                # Only the newest sample reports DOWN, so a lost update is visible in the
                # status_level itself, not merely in the timestamp bookkeeping.
                link_state="DOWN" if offset == newest_offset else "UP",
                sampled_at=BASE_SAMPLED_AT.replace(second=offset),
                offset_seconds=offset,
            )
            for offset in offsets
        ]
    )

    row = await _stored(db_session, binding.id)
    assert row.sampled_at == BASE_SAMPLED_AT.replace(second=newest_offset)
    assert row.status_level == "DOWN"
    assert row.payload["sequence"] == newest_offset


async def test_a_concurrent_stale_sample_never_wins_against_a_newer_one(db_session, concurrent_sessions):
    """The narrow two-writer race the guard is written for: a slow poller's older reading
    and a fast poller's newer one commit at the same moment. The newer sample must win
    both times, whichever of the two the scheduler happens to let commit last."""
    binding = await _seed_binding(db_session)
    older = BASE_SAMPLED_AT
    newer = BASE_SAMPLED_AT.replace(minute=5)

    for attempt in range(8):
        calls = [
            _ingest_in_own_session(
                concurrent_sessions, binding_id=binding.id, link_state="DOWN",
                sampled_at=older, offset_seconds=0,
            ),
            _ingest_in_own_session(
                concurrent_sessions, binding_id=binding.id, link_state="UP",
                sampled_at=newer, offset_seconds=1,
            ),
        ]
        if attempt % 2:
            calls.reverse()
        await asyncio.gather(*calls)

        row = await _stored(db_session, binding.id)
        assert row.sampled_at == newer, f"stale sample won on attempt {attempt}"
        assert row.status_level == "UP", f"stale DOWN overwrote the newer UP on attempt {attempt}"


async def test_concurrent_duplicate_samples_converge_on_one_row(db_session, concurrent_sessions):
    """Ten concurrent re-deliveries of the *same* sample: exactly one cached row, and its
    `received_at` is whichever writer inserted it — none of the other nine may bump it,
    since an equal timestamp carries no new information."""
    binding = await _seed_binding(db_session)
    sampled_at = BASE_SAMPLED_AT

    await asyncio.gather(
        *[
            _ingest_in_own_session(
                concurrent_sessions, binding_id=binding.id, link_state="UP",
                sampled_at=sampled_at, offset_seconds=index,
            )
            for index in range(10)
        ]
    )

    rows = (
        await db_session.execute(select(TelemetryLatestStatus).where(TelemetryLatestStatus.binding_id == binding.id))
    ).scalars().all()
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    assert rows[0].sampled_at == sampled_at
    assert rows[0].status_level == "UP"

    first_received_at = rows[0].received_at
    await _ingest_in_own_session(
        concurrent_sessions, binding_id=binding.id, link_state="DOWN", sampled_at=sampled_at, offset_seconds=99,
    )
    row = await _stored(db_session, binding.id)
    assert row.received_at == first_received_at
    assert row.status_level == "UP"
