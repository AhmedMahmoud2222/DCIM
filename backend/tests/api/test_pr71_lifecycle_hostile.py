"""Hostile lifecycle tests for PR #71 (decommission timestamp).

Two requests run on independent database sessions. A barrier placed in the audit
writer (after the status change, before commit) forces both requests to have read the
asset before either commits. A row lock on the asset serialises them; the barrier then
times out for the second request and the test still completes.
"""
import asyncio
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import managed_assets as module
from app.db.session import get_db
from app.main import app

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def race_client(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def _active_asset(client, headers):
    created = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "rack", "asset_tag": f"R-{uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    for status in ("installed", "active"):
        r = await client.post(f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": status}, headers=headers)
        assert r.status_code == 200, r.text
    return asset_id


async def _headers(race_client, make_user):
    user = await make_user(f"life-{uuid.uuid4().hex[:6]}@example.com", PASSWORD, "Engineer")
    login = await race_client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _install_barrier(monkeypatch, parties=2):
    barrier = asyncio.Barrier(parties)
    original = module.write_audit_log

    async def gated(*args, **kwargs):
        try:
            await asyncio.wait_for(barrier.wait(), timeout=3)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "write_audit_log", gated)


async def _transition(client, headers, asset_id, to_status):
    return await client.post(
        f"/api/v1/managed-assets/{asset_id}/lifecycle-transition", json={"to_status": to_status}, headers=headers
    )


async def test_concurrent_decommission_is_applied_once(race_client, make_user, db_engine, monkeypatch):
    headers = await _headers(race_client, make_user)
    asset_id = await _active_asset(race_client, headers)
    _install_barrier(monkeypatch)
    first, second = await asyncio.gather(
        _transition(race_client, headers, asset_id, "decommissioned"),
        _transition(race_client, headers, asset_id, "decommissioned"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 422], (first.text, second.text)
    async with db_engine.connect() as conn:
        audits = (await conn.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_id = :i AND action = 'managed_asset.lifecycle_transition' AND after->>'lifecycle_status' = 'decommissioned'"),
            {"i": asset_id})).scalar_one()
        events = (await conn.execute(
            text("SELECT count(*) FROM outbox_event WHERE aggregate_id = :i AND payload->>'to' = 'decommissioned'"),
            {"i": asset_id})).scalar_one()
    assert audits == 1 and events == 1, (audits, events)


async def test_decommission_racing_maintenance_never_leaves_a_timestamp_on_a_live_asset(race_client, make_user, db_engine, monkeypatch):
    headers = await _headers(race_client, make_user)
    asset_id = await _active_asset(race_client, headers)
    _install_barrier(monkeypatch)
    await asyncio.gather(
        _transition(race_client, headers, asset_id, "decommissioned"),
        _transition(race_client, headers, asset_id, "maintenance"),
    )
    async with db_engine.connect() as conn:
        status, stamp = (await conn.execute(
            text("SELECT lifecycle_status, decommissioned_at FROM managed_asset WHERE id = :i"), {"i": asset_id})).one()
    assert (stamp is not None) == (status in ("decommissioned", "removed")), (status, stamp)
