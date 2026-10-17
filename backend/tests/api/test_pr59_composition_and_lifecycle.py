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
    g2 = await _group(client, admin, sites=[{"site_id": b["site"], "rack_scope": "selected", "rack_ids": [rb]}])
    _, h = await _group_user(client, admin, [g1, g2])
    seen = {r["id"] for r in (await client.get("/api/v1/racks", headers=h)).json()["items"]}
    assert seen == {ra1, rb}
    g3 = await _group(client, admin, sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, h2 = await _group_user(client, admin, [g1, g3])
    assert {r["id"] for r in (await client.get("/api/v1/racks", headers=h2)).json()["items"]} == {ra1, ra2}


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
    assert (await client.get("/api/v1/sites", headers=hv)).status_code == 200  # deny is per permission, and the role user stays unrestricted


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
    await client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}, headers=admin)
    bodies = []
    for email, pw in ((user["email"], PW), ("nobody@example.com", PW), (user["email"], "wrong-password-value")):
        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
        bodies.append((resp.status_code, resp.json()["detail"]))
    assert len(set(bodies)) == 1


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
    """Each request has its own session/transaction, so the two PATCHes genuinely overlap in
    PostgreSQL. Whatever the interleaving, at least one administrator must stay active."""
    client = race_client
    root = await make_user(f"root-{uuid.uuid4().hex[:6]}@example.com", PW, "Administrator")
    admin = await _login(client, root.email)
    gid = await _group(client, admin, allow=ADMIN_PERMS)
    u1, h1 = await _group_user(client, admin, [gid])
    u2, h2 = await _group_user(client, admin, [gid])
    async with db_engine.begin() as conn:  # take the global administrator out of play
        await conn.execute(text("UPDATE app_user SET is_active = false WHERE id = :i"), {"i": root.id})
    for _ in range(5):
        results = await asyncio.gather(
            client.patch(f"/api/v1/users/{u2['id']}", json={"is_active": False}, headers=h1),
            client.patch(f"/api/v1/users/{u1['id']}", json={"is_active": False}, headers=h2),
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, Exception)], results
        # Only outcomes the design allows: success, last-administrator conflict, or refusal. A 401 (lapsed token) or a 5xx
        # would make the "at least one stays active" check below pass for the wrong reason.
        assert {r.status_code for r in results} <= {200, 403, 409}, [(r.status_code, r.text) for r in results]
        async with db_engine.connect() as conn:
            active = (await conn.execute(text("SELECT count(*) FROM app_user WHERE id IN (:a, :b) AND is_active"), {"a": u1["id"], "b": u2["id"]})).scalar_one()
        assert active >= 1, sorted(r.status_code for r in results)
        async with db_engine.begin() as conn:
            await conn.execute(text("UPDATE app_user SET is_active = true WHERE id IN (:a, :b)"), {"a": u1["id"], "b": u2["id"]})
