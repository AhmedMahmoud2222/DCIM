"""Issue #337 regression coverage, against real PostgreSQL: idempotency records are scoped to the
authenticated principal, so two authenticated users sharing an Idempotency-Key and endpoint can
neither replay each other's responses nor block each other, while one principal's retries still
deduplicate and the claim/fencing machinery keeps working per principal.

HTTP tests use two real logged-in users and a fresh AsyncSession per request (as production's
get_db does); service tests drive get_or_claim directly with one session per actor."""

import asyncio
import secrets
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.application import idempotency as idem
from app.main import app
from tests.conftest import TEST_DATABASE_URL

PASSWORD = secrets.token_urlsafe(24)  # random per test run, never a fixed credential
ENDPOINT = "POST:/principal-scope-test"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=10)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def per_request_client(session_factory):
    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _login(email: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _post_asset(headers: dict, key: str, tag: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(
            "/api/v1/managed-assets",
            json={"asset_type": "rack", "asset_tag": tag},
            headers={**headers, "Idempotency-Key": key},
        )


@pytest_asyncio.fixture
async def two_principals(make_user, per_request_client):
    suffix = uuid.uuid4().hex[:8]
    a = await make_user(f"idem-a-{suffix}@example.com", PASSWORD, "Administrator")
    b = await make_user(f"idem-b-{suffix}@example.com", PASSWORD, "Administrator")
    return (a, await _login(a.email)), (b, await _login(b.email))


async def _principals_for(db_session, key: str) -> list[str]:
    rows = await db_session.execute(
        text("SELECT principal FROM idempotency_key WHERE key = :k ORDER BY principal"), {"k": key}
    )
    return [r[0] for r in rows]


async def test_same_key_and_body_two_principals_do_not_replay_each_other(two_principals, db_session):
    (user_a, headers_a), (user_b, headers_b) = two_principals
    key, tag = str(uuid.uuid4()), f"P337-{uuid.uuid4().hex[:8]}"

    first = await _post_asset(headers_a, key, tag)
    assert first.status_code == 201, first.text
    # B sends the identical key AND body. Before the fix this replayed A's response (201 with A's id);
    # now it is B's own request, which hits the unrelated unique asset_tag instead of A's cached reply.
    second = await _post_asset(headers_b, key, tag)
    assert second.status_code == 409, second.text
    assert second.json().get("id") != first.json()["id"]

    assert sorted(await _principals_for(db_session, key)) == [f"user:{user_a.id}"], (
        "B's failed claim must be released and must never have touched A's record"
    )


async def test_same_key_distinct_bodies_two_principals_do_not_block_each_other(two_principals, db_session):
    (user_a, headers_a), (user_b, headers_b) = two_principals
    key = str(uuid.uuid4())

    ra = await _post_asset(headers_a, key, f"P337A-{uuid.uuid4().hex[:8]}")
    rb = await _post_asset(headers_b, key, f"P337B-{uuid.uuid4().hex[:8]}")
    # Before the fix B got 422 "Idempotency Key Reused" purely because A used the key (DoS by key collision).
    assert (ra.status_code, rb.status_code) == (201, 201), (ra.text, rb.text)
    assert ra.json()["id"] != rb.json()["id"]
    assert await _principals_for(db_session, key) == sorted([f"user:{user_a.id}", f"user:{user_b.id}"])


async def test_same_principal_retry_still_deduplicates(two_principals):
    (_, headers_a), (_, headers_b) = two_principals
    key, tag = str(uuid.uuid4()), f"P337R-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(*[_post_asset(headers_a, key, tag) for _ in range(5)])
    assert [r.status_code for r in results] == [201] * 5
    assert len({r.json()["id"] for r in results}) == 1
    # And a different body for the same principal still conflicts.
    clash = await _post_asset(headers_a, key, f"{tag}-other")
    assert clash.status_code == 422


async def test_concurrent_identical_requests_from_two_principals_each_create_one_resource(two_principals):
    (_, headers_a), (_, headers_b) = two_principals
    key = str(uuid.uuid4())
    tag_a, tag_b = f"P337CA-{uuid.uuid4().hex[:8]}", f"P337CB-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        *[_post_asset(headers_a, key, tag_a) for _ in range(6)], *[_post_asset(headers_b, key, tag_b) for _ in range(6)]
    )
    assert [r.status_code for r in results] == [201] * 12
    assert len({r.json()["id"] for r in results[:6]}) == 1
    assert len({r.json()["id"] for r in results[6:]}) == 1
    assert results[0].json()["id"] != results[6].json()["id"]


# ---- service level: claims, stale reclaim and fencing are per principal ----------------------------


async def _claim(factory, principal: str, key: str, request_hash: str = "h"):
    session = factory()
    outcome = await idem.get_or_claim(session, principal=principal, key=key, endpoint=ENDPOINT, request_hash=request_hash)
    return session, outcome


async def test_processing_claim_of_one_principal_does_not_block_another(session_factory):
    key = str(uuid.uuid4())
    sa_, oa = await _claim(session_factory, "user:A", key, "hash-a")
    # A is still 'processing'. B must obtain its own claim immediately instead of polling/503/conflict.
    sb, ob = await asyncio.wait_for(_claim(session_factory, "user:B", key, "hash-b"), timeout=2)
    assert oa.claim is not None and ob.claim is not None and oa.claim.id != ob.claim.id
    await idem.complete_claim(sb, ob.claim, response_status=201, response_body={"owner": "B"})
    await sb.commit()
    await idem.complete_claim(sa_, oa.claim, response_status=201, response_body={"owner": "A"})
    await sa_.commit()

    for principal, owner in (("user:A", "A"), ("user:B", "B")):
        s, replay = await _claim(session_factory, principal, key, f"hash-{owner.lower()}")
        assert replay.cached is not None and replay.cached.response_body == {"owner": owner}
        await s.close()
    await sa_.close()
    await sb.close()


async def test_stale_reclaim_and_fencing_are_scoped_to_principal(session_factory):
    key = str(uuid.uuid4())
    sa_, oa = await _claim(session_factory, "user:A", key)
    sb, ob = await _claim(session_factory, "user:B", key)
    async with session_factory() as s:
        await s.execute(
            text("UPDATE idempotency_key SET updated_at = now() - interval '5 minutes' WHERE key = :k AND principal = 'user:A'"),
            {"k": key},
        )
        await s.commit()

    # A's retry reclaims A's stale row (generation bump) and fences the original A owner out ...
    sa2, oa2 = await _claim(session_factory, "user:A", key)
    assert oa2.claim is not None and oa2.claim.claim_generation == 2
    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(sa_, oa.claim, response_status=201, response_body={"stale": True})
    await sa_.rollback()
    # ... without disturbing B's record, which B can still complete at generation 1.
    assert ob.claim.claim_generation == 1
    await idem.complete_claim(sb, ob.claim, response_status=201, response_body={"owner": "B"})
    await sb.commit()
    for s in (sa_, sb, sa2):
        await s.close()


async def test_release_by_one_principal_never_deletes_anothers_claim(session_factory):
    key = str(uuid.uuid4())
    sa_, oa = await _claim(session_factory, "user:A", key)
    sb, ob = await _claim(session_factory, "user:B", key)
    await idem.release_claim(sa_, idem.ClaimRef.of(oa.claim))
    async with session_factory() as s:
        rows = (await s.execute(text("SELECT principal FROM idempotency_key WHERE key = :k"), {"k": key})).all()
    assert [r[0] for r in rows] == ["user:B"]
    await sa_.close()
    await sb.close()


async def test_claim_requires_a_principal(session_factory):
    async with session_factory() as s:
        for bad in ("", "   "):
            with pytest.raises(ValueError):
                await idem.get_or_claim(s, principal=bad, key="k", endpoint=ENDPOINT, request_hash="h")


def test_principal_namespaces_cannot_collide():
    u = uuid.uuid4()
    assert idem.user_principal(u) != idem.collector_principal(u)
