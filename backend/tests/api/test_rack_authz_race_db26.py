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
from app.api.v1 import floor_plans as floor_plans_module
from app.api.v1 import managed_assets as managed_assets_module
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
    room_a3 = (
        await client.post("/api/v1/rooms", json={"floor_id": site_a["floor"], "code": f"R3-{uuid.uuid4().hex[:4]}", "name": "A3"}, headers=admin)
    ).json()["id"]
    rack = await _make_rack(client, admin, auth_headers, site_a["room"])
    group = await _group(
        client, admin, allow=["rack:read", "rack:manage", "rack:place"],
        sites=[{"site_id": site_a["site"], "rack_scope": "all", "rack_ids": []}],
    )
    _, operator = await _group_user(client, admin, [group])
    return {"admin": admin, "operator": operator, "rack": rack, "room_a": site_a["room"], "room_a2": room_a2, "room_a3": room_a3,
            "room_b": site_b["room"], "site_a": site_a, "site_b": site_b}


def _install_pause(monkeypatch, module):
    """Parks the first RESTRICTED caller of `module.ensure_rack_access` right after its check returns.

    The pause is before any lock is taken, which is the exact check-versus-lock window of DB-26. Unrestricted
    callers (the admin mover) and later calls pass straight through."""
    original = module.ensure_rack_access
    reached, release, used = asyncio.Event(), asyncio.Event(), []

    async def _wrapped(db, scope, rack_id):
        await original(db, scope, rack_id)
        if not scope.unrestricted and not used:
            used.append(True)
            reached.set()
            await asyncio.wait_for(release.wait(), BLOCK_TIMEOUT_S * 2)

    monkeypatch.setattr(module, "ensure_rack_access", _wrapped)
    return reached, release


@pytest.fixture
def pause_after_precheck(monkeypatch):
    return _install_pause(monkeypatch, racks_module)


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


# ------------------------------------------------------------- stale observations (AGY P2: observe before the wait)
async def test_same_site_competing_moves_without_if_match_still_conflict(per_request_client, world, db_engine):
    """The first mover holds the placement locks uncommitted; the restricted second mover (no If-Match) must wait,
    then get the established 409, never silently overwrite the first move."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as holder:
        await move_rack(holder, rack_id=uuid.UUID(world["rack"]), room_id=uuid.UUID(world["room_a2"]))
        victim = asyncio.create_task(per_request_client.post(
            f"{RACKS}/{world['rack']}/move", json={"room_id": world["room_a3"]}, headers=world["operator"]))
        await _wait_until_blocked(db_engine)
        assert not victim.done()
        await holder.commit()
    resp = await asyncio.wait_for(victim, BLOCK_TIMEOUT_S)
    assert resp.status_code == 409, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_a2"], "the first move must survive"
    assert state["audits"].count("rack.move") == 0  # the holder bypassed the API; the loser wrote nothing


async def test_concurrent_first_placement_conflicts_for_the_second_placer(per_request_client, auth_headers, client, world, db_engine):
    """Unplaced rack: two placements race; the second (unrestricted admin, no If-Match) gets 409, not an overwrite."""
    from tests.api._phase2_helpers import create_rack

    loose = (await create_rack(client, world["admin"], auth_headers, name="Loose"))["id"]  # no room -> unplaced
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as holder:
        await move_rack(holder, rack_id=uuid.UUID(loose), room_id=uuid.UUID(world["room_a"]))
        victim = asyncio.create_task(per_request_client.post(
            f"{RACKS}/{loose}/move", json={"room_id": world["room_a2"]}, headers=world["admin"]))
        await _wait_until_blocked(db_engine)
        await holder.commit()
    assert (await asyncio.wait_for(victim, BLOCK_TIMEOUT_S)).status_code == 409
    assert (await _state(db_engine, loose))["current_room"] == world["room_a"]


# ------------------------------------------------- #163 per-permission union and rack_scope="selected" (scenario 3)
async def _retire_race_denied(per_request_client, pause, world, headers, db_engine):
    resp, _ = await _race(per_request_client, pause, world,
                          lambda: per_request_client.post(f"{RACKS}/{world['rack']}/retire", headers=headers))
    assert resp.status_code == 404, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_b"] and "rack.unplace" not in state["audits"]


async def test_per_permission_scopes_rack_read_on_both_sites_does_not_authorise_rack_place(
    per_request_client, client, pause_after_precheck, world, db_engine
):
    """#163: rack:read covers sites A+B, rack:place only A. After the rack moves to B the user still READS it, but
    the stale rack:place decision must not retire it."""
    reader = await _group(client, world["admin"], allow=["rack:read"], sites=[
        {"site_id": world["site_a"]["site"], "rack_scope": "all", "rack_ids": []},
        {"site_id": world["site_b"]["site"], "rack_scope": "all", "rack_ids": []}])
    placer = await _group(client, world["admin"], allow=["rack:place"], sites=[
        {"site_id": world["site_a"]["site"], "rack_scope": "all", "rack_ids": []}])
    _, headers = await _group_user(client, world["admin"], [reader, placer])
    await _retire_race_denied(per_request_client, pause_after_precheck, world, headers, db_engine)
    assert (await per_request_client.get(f"{RACKS}/{world['rack']}", headers=headers)).status_code == 200


async def test_selected_rack_scope_grant_goes_stale_when_the_rack_moves_site(
    per_request_client, client, pause_after_precheck, world, db_engine
):
    group = await _group(client, world["admin"], allow=["rack:read", "rack:manage", "rack:place"], sites=[
        {"site_id": world["site_a"]["site"], "rack_scope": "selected", "rack_ids": [world["rack"]]}])
    _, headers = await _group_user(client, world["admin"], [group])
    assert (await per_request_client.get(f"{RACKS}/{world['rack']}", headers=headers)).status_code == 200
    await _retire_race_denied(per_request_client, pause_after_precheck, world, headers, db_engine)


# ------------------------------------------------------------------------ managed-asset lifecycle (AGY P2)
def _lifecycle_scope_aware(monkeypatch):
    import app.application.access_control as access_control

    monkeypatch.setattr(access_control, "SCOPE_AWARE_PERMISSIONS",
                        access_control.SCOPE_AWARE_PERMISSIONS | {"managed_asset:read", "managed_asset:update_lifecycle"})


async def _lifecycle_operator(client, world):
    group = await _group(client, world["admin"], allow=["managed_asset:read", "managed_asset:update_lifecycle", "rack:read"], sites=[
        {"site_id": world["site_a"]["site"], "rack_scope": "all", "rack_ids": []}])
    _, headers = await _group_user(client, world["admin"], [group])
    return headers


async def test_rack_lifecycle_transition_after_concurrent_cross_site_move_is_denied(
    per_request_client, client, monkeypatch, world, db_engine
):
    _lifecycle_scope_aware(monkeypatch)
    headers = await _lifecycle_operator(client, world)
    pause = _install_pause(monkeypatch, managed_assets_module)
    resp, _ = await _race(per_request_client, pause, world,
                          lambda: per_request_client.post(f"/api/v1/managed-assets/{world['rack']}/lifecycle-transition",
                                                          json={"to_status": "installed"}, headers=headers))
    assert resp.status_code == 404, resp.text
    rack = uuid.UUID(world["rack"])
    async with db_engine.connect() as conn:
        status = (await conn.execute(text("SELECT lifecycle_status FROM managed_asset WHERE id = :r"), {"r": rack})).scalar_one()
        audits = (await conn.execute(text("SELECT count(*) FROM audit_log WHERE entity_id = :r AND action LIKE '%lifecycle%'"), {"r": rack})).scalar_one()
        events = (await conn.execute(text("SELECT count(*) FROM outbox_event WHERE aggregate_id = :r AND event_type LIKE '%Lifecycle%'"), {"r": rack})).scalar_one()
    assert (status, audits, events) == ("planned", 0, 0)


async def test_rack_lifecycle_transition_still_works_in_scope(per_request_client, client, monkeypatch, world):
    _lifecycle_scope_aware(monkeypatch)
    headers = await _lifecycle_operator(client, world)
    ok = await per_request_client.post(f"/api/v1/managed-assets/{world['rack']}/lifecycle-transition", json={"to_status": "installed"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["lifecycle_status"] == "installed"


# ------------------------------------------------------------------ floor-plan candidate accept (AGY P2 / P3)
from app.application.catalog_documents.extraction.sandbox import landlock_available  # noqa: E402

needs_parser = pytest.mark.skipif(not landlock_available(), reason="Landlock is required for the parser sandbox")


async def _accept_world(client, world, db_session):
    """A rack placed in room A with an imported, calibrated floor plan, plus an operator who holds floor_plan:manage
    through a GLOBAL role (that permission is not site-aware) but rack:place only for Site A through a group."""
    from sqlalchemy import select

    from app.domain.auth.models import Permission, Role, RoleAssignment, RolePermission
    from tests import spatial_fixtures as fx
    from tests.api._spatial_helpers import calibrate_declared, candidates, import_file, new_floor_plan

    admin = world["admin"]
    plan = await new_floor_plan(client, admin, world["room_a"])
    job = await import_file(client, admin, plan["id"], fx.rack_row_dxf(2), "r.dxf", "dxf")
    await calibrate_declared(client, admin, plan["id"], job["id"])
    cand = (await candidates(client, admin, job["id"]))[0]
    group = await _group(client, admin, allow=["rack:read", "rack:place"], sites=[
        {"site_id": world["site_a"]["site"], "rack_scope": "all", "rack_ids": []}])
    user, headers = await _group_user(client, admin, [group])
    role = Role(name=f"fp-manage-{uuid.uuid4().hex[:6]}", is_system=False)
    db_session.add(role)
    await db_session.flush()
    for code in ("floor_plan:read", "floor_plan:manage"):
        resource, action = code.split(":")
        perm = (await db_session.execute(select(Permission).where(Permission.resource == resource, Permission.action == action))).scalar_one()
        db_session.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db_session.add(RoleAssignment(id=uuid.uuid4(), user_id=uuid.UUID(user["id"]), role_id=role.id, scope_type="global"))
    await db_session.commit()
    return {"headers": headers, "job": job, "cand": cand}


@needs_parser
async def test_accept_apply_position_after_concurrent_cross_site_move_is_denied(
    per_request_client, client, db_session, monkeypatch, world, db_engine
):
    a = await _accept_world(client, world, db_session)
    version = (await client.get(f"{RACKS}/{world['rack']}", headers=world["admin"])).json()["placement"]["version"]
    pause = _install_pause(monkeypatch, floor_plans_module)
    body = {"object_type": "rack", "matched_asset_id": world["rack"], "apply_position": True, "placement_version": version}
    resp, _ = await _race(per_request_client, pause, world, lambda: per_request_client.post(
        f"/api/v1/floor-plans/import-jobs/{a['job']['id']}/candidates/{a['cand']['id']}/accept",
        json=body, headers={**a["headers"], "If-Match": str(a["cand"]["version"])}))
    assert resp.status_code == 404, resp.text
    state = await _state(db_engine, world["rack"])
    assert state["current_room"] == world["room_b"] and state["audits"].count("rack.move") == 1
    async with db_engine.connect() as conn:
        status = (await conn.execute(text("SELECT status FROM floor_plan_import_candidate WHERE id = :c"), {"c": uuid.UUID(a["cand"]["id"])})).scalar_one()
    assert status != "accepted"


@needs_parser
async def test_accept_apply_position_on_an_unplaced_rack_conflicts_with_a_concurrent_placement(
    per_request_client, client, auth_headers, db_session, world, db_engine
):
    from tests.api._phase2_helpers import create_rack

    a = await _accept_world(client, world, db_session)
    loose = (await create_rack(client, world["admin"], auth_headers, name="Loose"))["id"]
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as holder:
        await move_rack(holder, rack_id=uuid.UUID(loose), room_id=uuid.UUID(world["room_a"]))
        victim = asyncio.create_task(per_request_client.post(
            f"/api/v1/floor-plans/import-jobs/{a['job']['id']}/candidates/{a['cand']['id']}/accept",
            json={"object_type": "rack", "matched_asset_id": loose, "apply_position": True},
            headers={**world["admin"], "If-Match": str(a["cand"]["version"])}))
        await _wait_until_blocked(db_engine)
        await holder.commit()
    assert (await asyncio.wait_for(victim, BLOCK_TIMEOUT_S)).status_code == 409
