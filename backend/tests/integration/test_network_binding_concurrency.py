"""Issue #101 review B4: binding an integration to a device profile must serialise with retiring or
redefining that profile, in both interleavings, so no active integration is ever left bound to a retired
profile (or to one whose vendor no longer supports it).

Two real sessions on separate connections drive the service layer. The first transaction stays open at its
synchronisation point (it holds the profile row lock); the second is started and PostgreSQL itself must report
it blocked on that lock before the first commits. Against the old unlocked bind, the second never blocks, the
barrier assertion fails, and the end state shows a retired profile with a bound integration."""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.network import profile_service
from app.application.network.profile_schema import VendorProfileContent
from app.core.errors import ApiError, ConflictError
from app.domain.integration.models import Integration
from tests.api._network_helpers import create_device, create_vendor, vendor_doc
from tests.api._phase8_helpers import create_integration

BARRIER_TIMEOUT_S = 20


@pytest.fixture
def sessions(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    return factory


async def _actor(db_engine) -> uuid.UUID:
    async with db_engine.connect() as conn:
        return (await conn.execute(text("SELECT id FROM app_user LIMIT 1"))).scalar_one()


async def _wait_for_blocked_backend(admin_engine, task: asyncio.Task) -> None:
    deadline = asyncio.get_running_loop().time() + BARRIER_TIMEOUT_S
    while True:
        async with admin_engine.connect() as conn:
            blocked = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'active' AND wait_event_type = 'Lock'"))).scalar_one()
        if blocked >= 1:
            return
        assert not task.done(), "the second transaction finished without waiting: the profile is not lock-protected"
        assert asyncio.get_running_loop().time() < deadline, "the second transaction never blocked on the profile lock"
        await asyncio.sleep(0.05)


async def _scenario(client, headers):
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"])
    integration = await create_integration(client, headers, integration_type="snmp", config={"version": "v2c"})
    return vendor, device, integration


async def _bound_to_retired(db_engine) -> int:
    async with db_engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT count(*) FROM integration i JOIN device_profile d ON d.id = i.device_profile_id WHERE d.status = 'retired'"
        ))).scalar_one()


async def _bind(sessions, integration_id, device_id):
    async with sessions() as db:
        integration = await db.get(Integration, uuid.UUID(integration_id))
        await profile_service.bind_integration_profile(db, integration=integration, device_profile_id=uuid.UUID(device_id))
        await db.commit()


async def _retire(sessions, device, actor):
    async with sessions() as db:
        await profile_service.retire_device(
            db, device_id=uuid.UUID(device["id"]), expected_version=device["version"], actor_user_id=actor,
            request_id=None, correlation_id=None)
        await db.commit()


async def test_retire_first_then_bind_is_refused_and_nothing_is_left_bound(client, auth_headers, db_engine, _admin_engine, sessions):
    headers = await auth_headers("Administrator")
    _vendor, device, integration = await _scenario(client, headers)
    actor = await _actor(db_engine)
    async with sessions() as retiring:
        await profile_service.retire_device(
            retiring, device_id=uuid.UUID(device["id"]), expected_version=device["version"], actor_user_id=actor,
            request_id=None, correlation_id=None)
        # retire holds the device row lock and has not committed
        binder = asyncio.create_task(_bind(sessions, integration["id"], device["id"]))
        await _wait_for_blocked_backend(_admin_engine, binder)
        await retiring.commit()
    with pytest.raises(ConflictError):
        await binder
    assert await _bound_to_retired(db_engine) == 0


async def test_bind_first_then_retire_is_refused_and_the_binding_survives(client, auth_headers, db_engine, _admin_engine, sessions):
    headers = await auth_headers("Administrator")
    _vendor, device, integration = await _scenario(client, headers)
    actor = await _actor(db_engine)
    async with sessions() as binding:
        row = await binding.get(Integration, uuid.UUID(integration["id"]))
        await profile_service.bind_integration_profile(binding, integration=row, device_profile_id=uuid.UUID(device["id"]))
        await binding.flush()  # bound and validated under a share lock, not yet committed
        retirer = asyncio.create_task(_retire(sessions, device, actor))
        await _wait_for_blocked_backend(_admin_engine, retirer)
        await binding.commit()
    with pytest.raises(ConflictError, match="still bound"):
        await retirer
    assert await _bound_to_retired(db_engine) == 0
    async with db_engine.connect() as conn:
        bound = (await conn.execute(text("SELECT device_profile_id FROM integration WHERE id = :i"), {"i": integration["id"]})).scalar_one()
    assert str(bound) == device["id"]


async def test_vendor_update_dropping_a_protocol_waits_for_an_inflight_bind_then_is_refused(
    client, auth_headers, db_engine, _admin_engine, sessions
):
    headers = await auth_headers("Administrator")
    vendor, device, integration = await _scenario(client, headers)
    actor = await _actor(db_engine)
    content = VendorProfileContent.model_validate(
        {k: v for k, v in vendor_doc(vendor["code"], neighbor=False).items() if k != "code"} | {"supported_protocols": ["icmp"]})

    async def update():
        async with sessions() as db:
            await profile_service.update_vendor(
                db, vendor_id=uuid.UUID(vendor["id"]), content=content, expected_version=vendor["version"], actor_user_id=actor,
                request_id=None, correlation_id=None)
            await db.commit()

    async with sessions() as binding:
        row = await binding.get(Integration, uuid.UUID(integration["id"]))
        await profile_service.bind_integration_profile(binding, integration=row, device_profile_id=uuid.UUID(device["id"]))
        await binding.flush()
        updater = asyncio.create_task(update())
        await _wait_for_blocked_backend(_admin_engine, updater)
        await binding.commit()
    with pytest.raises(ConflictError, match="no longer be valid"):
        await updater
    async with db_engine.connect() as conn:
        protocols = (await conn.execute(text("SELECT supported_protocols FROM vendor_profile WHERE id = :v"), {"v": vendor["id"]})).scalar_one()
    assert "snmp" in protocols  # the stranding update changed nothing


async def test_bind_after_a_vendor_update_revalidates_against_the_new_vendor(client, auth_headers, db_engine, _admin_engine, sessions):
    headers = await auth_headers("Administrator")
    vendor, device, integration = await _scenario(client, headers)
    actor = await _actor(db_engine)
    content = VendorProfileContent.model_validate(
        {k: v for k, v in vendor_doc(vendor["code"], neighbor=False).items() if k != "code"} | {"supported_protocols": ["icmp"]})
    async with sessions() as updating:
        await profile_service.update_vendor(
            updating, vendor_id=uuid.UUID(vendor["id"]), content=content, expected_version=vendor["version"], actor_user_id=actor,
            request_id=None, correlation_id=None)
        await updating.flush()
        binder = asyncio.create_task(_bind(sessions, integration["id"], device["id"]))
        await _wait_for_blocked_backend(_admin_engine, binder)
        await updating.commit()
    with pytest.raises(ApiError) as caught:
        await binder
    assert caught.value.status_code == 422  # the bind saw the committed vendor, not a stale copy
    async with sessions() as db:
        assert (await db.execute(select(Integration.device_profile_id).where(Integration.id == uuid.UUID(integration["id"])))).scalar_one() is None
