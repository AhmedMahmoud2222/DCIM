"""PR #59 review, part 2: permission/scope composition,
revocation timing, genuinely concurrent administrator changes, and password handling."""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import get_db
from app.main import app
from tests.api.test_user_groups import PW, _group, _group_user, _login, _make_rack, _make_site

DATA_PERMS = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]
ADMIN_PERMS = ["user:read", "user:manage", "group:read", "group:manage"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _admin_user_id(client, headers) -> str:
    return (await client.get("/api/v1/auth/me", headers=headers)).json()["id"]


# ------------------------------------------------------------------ composition
async def test_union_of_sites_and_racks_across_groups_and_all_beats_selected(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    ra1, ra2 = await _make_rack(client, admin, auth_headers, a["room"]), await _make_rack(client, admin, auth_headers, a["room"])
    rb = await _make_rack(client, admin, auth_headers, b["room"])
    g1 = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [ra1]}])
    # Since SEC-ARCH-02 (#163/#170) a group's site/rack grant is bound to the permissions that
    # same group grants: scopes union only across groups granting the same permission.
    g2 = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": b["site"], "rack_scope": "selected", "rack_ids": [rb]}])
    _, h = await _group_user(client, admin, [g1, g2])
    seen = {r["id"] for r in (await client.get("/api/v1/racks", headers=h)).json()["items"]}
    assert seen == {ra1, rb}
    g3 = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, h2 = await _group_user(client, admin, [g1, g3])
    assert {r["id"] for r in (await client.get("/api/v1/racks", headers=h2)).json()["items"]} == {ra1, ra2}
    # Negative boundary: a group holding a site grant but no permission widens nothing.
    g_nop_b = await _group(client, admin, sites=[{"site_id": b["site"], "rack_scope": "selected", "rack_ids": [rb]}])
    g_nop_a = await _group(client, admin, sites=[{"site_id": a["site"], "rack_scope": "all"}])
    _, h3 = await _group_user(client, admin, [g1, g_nop_b, g_nop_a])
    assert {r["id"] for r in (await client.get("/api/v1/racks", headers=h3)).json()["items"]} == {ra1}


async def test_group_deny_beats_allow_from_another_group_and_from_a_role(client, admin):
    site = await _make_site(client, admin)
    allow = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": site["site"], "rack_scope": "all"}])
    deny = await _group(client, admin, deny=["rack:read"])
    _, h = await _group_user(client, admin, [allow, deny])
    assert (await client.get("/api/v1/racks", headers=h)).status_code == 403
    email = f"viewer-{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post("/api/v1/users", json={"email": email, "full_name": "V", "password": PW, "role_name": "Viewer", "group_ids": [deny]}, headers=admin)
    assert r.status_code == 201, r.text
    hv = await _login(client, email)
    assert (await client.get("/api/v1/racks", headers=hv)).status_code == 403
    # deny is per permission, and the role user stays unrestricted: the status alone would also be 200 for an empty restricted scope
    sites = await client.get("/api/v1/sites", headers=hv)
    assert sites.status_code == 200
    assert site["site"] in {item["id"] for item in sites.json()["items"]}


async def test_user_with_permissions_but_no_sites_sees_nothing(client, admin, auth_headers):
    site = await _make_site(client, admin)
    rack = await _make_rack(client, admin, auth_headers, site["room"])
    _, h = await _group_user(client, admin, [await _group(client, admin, allow=DATA_PERMS)])
    assert (await client.get("/api/v1/racks", headers=h)).json()["total"] == 0
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 404
    assert (await client.get("/api/v1/sites", headers=h)).json()["total"] == 0


async def test_revocations_take_effect_on_the_next_request(client, admin, auth_headers):
    site = await _make_site(client, admin)
    rack = await _make_rack(client, admin, auth_headers, site["room"])
    gid = await _group(client, admin, allow=DATA_PERMS, sites=[{"site_id": site["site"], "rack_scope": "selected", "rack_ids": [rack]}])
    user, h = await _group_user(client, admin, [gid])
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 200
    await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": site["site"], "rack_scope": "selected", "rack_ids": []}]}, headers=admin)
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 404  # rack removed
    await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": site["site"], "rack_scope": "all"}]}, headers=admin)
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 200
    await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": []}, headers=admin)
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 404  # site removed
    await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": site["site"], "rack_scope": "all"}]}, headers=admin)
    await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": []}, headers=admin)
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 403  # membership removed
    await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": [user["id"]]}, headers=admin)
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 200
    assert (await client.delete(f"/api/v1/groups/{gid}", headers=admin)).status_code == 204  # group deleted
    assert (await client.get(f"/api/v1/racks/{rack}", headers=h)).status_code == 403


async def test_password_change_and_deactivation_end_sessions(client, admin):
    gid = await _group(client, admin)
    user, h = await _group_user(client, admin, [gid])
    login = await client.post("/api/v1/auth/login", json={"email": user["email"], "password": PW})
    assert (await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}, headers=admin)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})).status_code == 401


# ------------------------------------------------------------------ password handling
async def test_password_policy_is_enforced_on_create_and_reset_and_never_echoed(client, admin, db_session):
    short = "elevenchars"
    assert len(short) == 11
    r = await client.post("/api/v1/users", json={"email": "s1@example.com", "full_name": "S", "password": short}, headers=admin)
    assert r.status_code == 422 and short not in r.text
    gid = await _group(client, admin)
    user, _ = await _group_user(client, admin, [gid])
    r = await client.patch(f"/api/v1/users/{user['id']}", json={"password": short}, headers=admin)
    assert r.status_code == 422 and short not in r.text
    secret = "Sup3r-Secret-Value-123"
    r = await client.patch(f"/api/v1/users/{user['id']}", json={"password": secret}, headers=admin)
    assert r.status_code == 200 and secret not in r.text
    rows = (await db_session.execute(text("SELECT before, after FROM audit_log WHERE entity_type = 'user'"))).all()
    assert secret not in repr(rows) and PW not in repr(rows)
    assert (await client.post("/api/v1/auth/login", json={"email": user["email"], "password": secret})).status_code == 200
    r = await client.post("/api/v1/users", json={"email": user["email"].upper(), "full_name": "D", "password": PW}, headers=admin)
    assert r.status_code == 409  # email normalisation matches the login path


async def test_login_error_is_identical_for_unknown_wrong_and_deactivated(client, admin):
    user, _ = await _group_user(client, admin, [await _group(client, admin)])
    bodies = []

    async def attempt(email, pw):
        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert resp.status_code in (401, 403), resp.text
        bodies.append((resp.status_code, resp.json()["detail"]))

    # Wrong password while the account is still active: this is the password-mismatch branch (inactive users are
    # filtered out before the password is checked, so it cannot be reached after deactivation).
    await attempt(user["email"], "wrong-password-value")
    await attempt("nobody@example.com", PW)
    await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}, headers=admin)
    await attempt(user["email"], PW)
    await attempt(user["email"], "wrong-password-value")
    assert len(bodies) == 4 and len(set(bodies)) == 1, bodies


# ------------------------------------------------------------------ concurrency (real overlapping transactions)
@pytest_asyncio.fixture
async def race_client(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_two_administrators_cannot_deactivate_each_other_concurrently(race_client, make_user, db_engine):
    """Two qualifying principals (global Administrators, unrestricted for user:manage and group:manage), each request
    on its own session so the PATCHes genuinely overlap in PostgreSQL. Global Administrators are peers, so strict
    outranking refuses both with exactly 403 and both stay active. The last-administrator invariant is unreachable
    through the API (any actor that strictly outranks an administrator is itself one); it is exercised directly,
    with real overlapping transactions, in the test below."""
    client = race_client
    emails = [f"adm-{uuid.uuid4().hex[:6]}@example.com" for _ in range(2)]
    users = [await make_user(email, PW, "Administrator") for email in emails]
    h1, h2 = [await _login(client, email) for email in emails]
    for _ in range(5):
        results = await asyncio.gather(
            client.patch(f"/api/v1/users/{users[1].id}", json={"is_active": False}, headers=h1),
            client.patch(f"/api/v1/users/{users[0].id}", json={"is_active": False}, headers=h2),
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, Exception)], results
        assert [r.status_code for r in results] == [403, 403], [(r.status_code, r.text) for r in results]
        async with db_engine.connect() as conn:
            active = (await conn.execute(text("SELECT count(*) FROM app_user WHERE id IN (:a, :b) AND is_active"), {"a": users[0].id, "b": users[1].id})).scalar_one()
        assert active == 2


async def test_last_administrator_lock_serialises_overlapping_deactivations(db_engine, make_user):
    """Two active global Administrators; two transactions each deactivate a different one and then run the
    last-administrator check. A barrier forces both to have flushed their own change (neither committed) before
    either checks, so without the advisory lock both see the other still active, both pass and no administrator
    remains. With the lock exactly one succeeds and the other gets the conflict."""
    from app.application.user_admin_service import assert_administrator_remains
    from app.core.errors import ConflictError

    users = [await make_user(f"adm-{uuid.uuid4().hex[:6]}@example.com", PW, "Administrator") for _ in range(2)]
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    barrier = asyncio.Barrier(2)

    async def deactivate(user_id) -> str:
        async with factory() as session:
            await session.execute(text("UPDATE app_user SET is_active = false WHERE id = :i"), {"i": user_id})
            await session.flush()
            await asyncio.wait_for(barrier.wait(), timeout=10)
            try:
                await assert_administrator_remains(session)
            except ConflictError:
                await session.rollback()
                return "conflict"
            await session.commit()
            return "ok"

    outcomes = await asyncio.gather(*(deactivate(u.id) for u in users))
    assert sorted(outcomes) == ["conflict", "ok"], outcomes
    async with db_engine.connect() as conn:
        active = (await conn.execute(text("SELECT count(*) FROM app_user WHERE id IN (:a, :b) AND is_active"), {"a": users[0].id, "b": users[1].id})).scalar_one()
    assert active == 1
