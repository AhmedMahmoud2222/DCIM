"""DB-26 (#360): rack mutations must not be authorised against a stale placement site.

A restricted Site-A operator passes the early `ensure_rack_access` check; an authorised session then moves
the rack to Site B and commits; the paused request resumes. It must be denied (404, same as any out-of-scope
rack) and must leave no rack, placement, audit or outbox change behind.

Real PostgreSQL, two genuinely independent sessions (one per request, see `per_request_client`). Determinism
comes from barriers, not sleeps:
  * `pause_after_precheck` parks the victim request exactly after its first, lock-free scope check;
  * `hold_placement_writer` keeps an uncommitted mover holding the placement locks, and `wait_until_blocked`
    polls pg_stat_activity until the victim is observably waiting on an advisory lock.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_db
from app.api.v1 import racks as racks_module
from app.application.placement_service import move_rack
from app.main import app
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

RACKS = "/api/v1/racks"
BLOCK_TIMEOUT_S = 15


# ----------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
async def per_request_client(client, db_engine):
    """One DB session per request, so two in-flight requests really are two PostgreSQL backends.

    Depends on `client` so world set-up (shared session) runs first; the previous override is restored."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    previous = app.dependency_overrides.get(get_db)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous


@pytest.fixture
async def world(client, auth_headers):
    admin = await auth_headers("Administrator")
    site_a = await _make_site(client, admin)
    site_b = await _make_site(client, admin, site_a["org"])
    room_a2 = (
        await client.post("/api/v1/rooms", json={"floor_id": site_a["floor"], "code": f"R2-{uuid.uuid4().hex[:4]}", "name": "A2"}, headers=admin)
    ).json()["id"]
    rack = await _make_rack(client, admin, auth_headers, site_a["room"])
    group = await _group(
        client, admin, allow=["rack:read", "rack:manage", "rack:place"],
        sites=[{"site_id": site_a["site"], "rack_scope": "all", "rack_ids": []}],
    )
    _, operator = await _group_user(client, admin, [group])
    return {"admin": admin, "operator": operator, "rack": rack, "room_a": site_a["room"], "room_a2": room_a2,
            "room_b": site_b["room"], "site_a": site_a, "site_b": site_b}


@pytest.fixture
def pause_after_precheck(monkeypatch):
    """Parks the first RESTRICTED caller of racks.ensure_rack_access right after its check returns.

    The pause is before any lock is taken, which is the exact check-versus-lock window of DB-26. Unrestricted
    callers (the admin mover) and later calls pass straight through."""
    original = racks_module.ensure_rack_access
    reached, release, used = asyncio.Event(), asyncio.Event(), []

    async def _wrapped(db, scope, rack_id):
        await original(db, scope, rack_id)
        if not scope.unrestricted and not used:
            used.append(True)
            reached.set()
            await asyncio.wait_for(release.wait(), BLOCK_TIMEOUT_S * 2)

    monkeypatch.setattr(racks_module, "ensure_rack_access", _wrapped)
    return reached, release


# ----------------------------------------------------------------------------------------------- helpers
async def _state(db_engine, rack_id: str) -> dict:
    async with db_engine.connect() as conn:
        placements = (await conn.execute(
            text("SELECT room_id, version, effective_to IS NULL AS current FROM rack_placement WHERE rack_id = :r ORDER BY version"),
            {"r": uuid.UUID(rack_id)})).all()
        rack = (await conn.execute(text("SELECT name, version FROM rack WHERE id = :r"), {"r": uuid.UUID(rack_id)})).one()
        audits = [r[0] for r in (await conn.execute(
            text("SELECT action FROM audit_log WHERE entity_id = :r AND entity_type = 'rack' ORDER BY timestamp"),
            {"r": uuid.UUID(rack_id)}))]
        events = [r[0] for r in (await conn.execute(
            text("SELECT event_type FROM outbox_event WHERE aggregate_id = :r"), {"r": uuid.UUID(rack_id)}))]
    current = [p for p in placements if p.current]
    return {"placements": [(str(p.room_id), p.version, p.current) for p in placements],
            "current_room": str(current[0].room_id) if current else None,
            "rack": (rack.name, rack.version), "audits": audits, "events": events}


async def _wait_until_blocked(db_engine) -> None:
    """Condition-based wait: a backend is observably blocked on an advisory lock."""
    async def _poll():
        while True:
            async with db_engine.connect() as conn:
                n = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"))).scalar_one()
            if n:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), BLOCK_TIMEOUT_S)


async def _admin_move(client, world, room: str, **extra):
    return await client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": room}, headers={**world["admin"], **extra})


async def _race(per_request_client, pause, world, victim_call):
    """Victim parks after its precheck; the admin moves the rack A -> B and commits; the victim resumes."""
    reached, release = pause
    victim = asyncio.create_task(victim_call())
    await asyncio.wait_for(reached.wait(), BLOCK_TIMEOUT_S)
    mover = await _admin_move(per_request_client, world, world["room_b"])
    assert mover.status_code == 200, mover.text  # the victim holds no lock while parked
    release.set()
    return await asyncio.wait_for(victim, BLOCK_TIMEOUT_S), mover


# ----------------------------------------------------------------------------------- scenarios 1-3, 6
async def test_retire_after_concurrent_cross_site_move_is_denied_with_no_side_effects(per_request_client, pause_after_precheck, world, db_engine):
    resp, _ = await _race(per_request_client, pause_after_precheck, world,
                          lambda: per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=world["operator"]))
    assert resp.status_code == 404, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_b"], "Site B placement must stay open"
    assert [p[2] for p in state["placements"]] == [False, True]
    assert "rack.unplace" not in state["audits"] and "RackUnplaced" not in state["events"]
    assert state["audits"].count("rack.move") == 1 and state["events"].count("RackMoved") == 1  # only the authorised mover's


async def test_update_after_concurrent_cross_site_move_is_denied_with_no_side_effects(per_request_client, pause_after_precheck, world, db_engine):
    before = await _state(db_engine, world["rack"])
    resp, _ = await _race(per_request_client, pause_after_precheck, world,
                          lambda: per_request_client.patch(f"{RACKS}/{world['rack']}", json={"name": "hijacked"},
                                                           headers={**world["operator"], "If-Match": str(before["rack"][1])}))
    assert resp.status_code == 404, resp.text
    assert "version" not in resp.text.lower()  # a denied caller learns nothing about the rack
    state = await _state(db_engine, world["rack"])
    assert state["rack"] == before["rack"]
    assert "rack.update" not in state["audits"] and "RackUpdated" not in state["events"]


async def test_move_with_stale_visibility_cannot_relocate_a_rack_that_left_the_operators_site(per_request_client, pause_after_precheck, world, db_engine):
    resp, _ = await _race(per_request_client, pause_after_precheck, world,
                          lambda: per_request_client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_a2"]}, headers=world["operator"]))
    assert resp.status_code == 404, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_b"]
    assert [p[2] for p in state["placements"]] == [False, True]  # no third placement row
    assert state["audits"].count("rack.move") == 1 and state["events"].count("RackMoved") == 1


# ------------------------------------------------------------------------------------------ scenario 4
async def test_authorised_same_site_workflows_still_work(per_request_client, world, db_engine):
    h = world["operator"]
    rack = (await per_request_client.get(f"{RACKS}/{world['rack']}", headers=h)).json()
    upd = await per_request_client.patch(f"{RACKS}/{world['rack']}", json={"name": "renamed"}, headers={**h, "If-Match": str(rack["version"])})
    assert upd.status_code == 200 and upd.json()["name"] == "renamed" and upd.json()["version"] == rack["version"] + 1
    mv = await per_request_client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_a2"]}, headers=h)
    assert mv.status_code == 200 and mv.json()["placement"]["room_id"] == world["room_a2"]
    assert (await per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=h)).status_code == 200
    again = await per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=h)  # retry stays idempotent
    assert again.status_code == 200 and again.json()["placement"] is None
    state = await _state(db_engine, world["rack"])
    assert state["audits"].count("rack.unplace") == 1 and state["events"].count("RackUnplaced") == 1


async def test_cross_site_move_needs_scope_over_both_sites(per_request_client, world):
    denied = await per_request_client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_b"]}, headers=world["operator"])
    assert denied.status_code == 404  # destination room outside the operator's scope
    ok = await _admin_move(per_request_client, world, world["room_b"])  # unrestricted admin: legitimate cross-site move
    assert ok.status_code == 200 and ok.json()["placement"]["room_id"] == world["room_b"]
    back = await per_request_client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_a"]}, headers=world["operator"])
    assert back.status_code == 404  # source (now Site B) outside scope, even though the destination is in scope


# ------------------------------------------------------------------------------------------ scenario 5
async def test_reverse_ordering_victim_blocks_on_the_mover_then_fails_closed(per_request_client, world, db_engine):
    """The mover is first: it holds the placement locks uncommitted while the victim's early check still sees Site A."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as holder:
        await move_rack(holder, rack_id=uuid.UUID(world["rack"]), room_id=uuid.UUID(world["room_b"]))  # takes AUTHORITY(S) + rack lock
        victim = asyncio.create_task(per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=world["operator"]))
        await _wait_until_blocked(db_engine)
        assert not victim.done(), "victim must wait for the in-flight move, not decide from stale state"
        await holder.commit()
    resp = await asyncio.wait_for(victim, BLOCK_TIMEOUT_S)
    assert resp.status_code == 404, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_b"] and "rack.unplace" not in state["audits"]


async def test_same_site_overlap_serialises_without_deadlock_and_stale_tokens_conflict(per_request_client, world, db_engine):
    """Two operators race a rename and a retire on the same rack: both finish (no deadlock), stale If-Match is 409."""
    h = world["operator"]
    version = (await per_request_client.get(f"{RACKS}/{world['rack']}", headers=h)).json()["version"]
    results = await asyncio.wait_for(asyncio.gather(
        per_request_client.patch(f"{RACKS}/{world['rack']}", json={"name": "one"}, headers={**h, "If-Match": str(version)}),
        per_request_client.patch(f"{RACKS}/{world['rack']}", json={"name": "two"}, headers={**h, "If-Match": str(version)}),
        per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=h),
    ), BLOCK_TIMEOUT_S)
    assert sorted(r.status_code for r in results[:2]) == [200, 409]
    assert results[2].status_code == 200


async def test_placement_aba_stale_if_match_cannot_move_a_replaced_rack(per_request_client, world):
    h = world["operator"]
    first = (await per_request_client.get(f"{RACKS}/{world['rack']}", headers=h)).json()["placement"]["version"]
    assert (await per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=h)).status_code == 200
    assert (await _admin_move(per_request_client, world, world["room_a"])).status_code == 200  # same room again: ABA
    stale = await per_request_client.post(f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_a2"]},
                                          headers={**h, "If-Match": str(first)})
    assert stale.status_code == 409, stale.text
