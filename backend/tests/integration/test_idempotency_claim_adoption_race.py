"""DB-03 (#357) AGY F1: a caller must never adopt a claim generation it was not issued.

The three claim statements (`_try_claim`, `_try_reclaim_stale`, `_try_replace_expired`) commit and release
their connection before `get_or_claim` reloads the row. If another request reclaims, replaces or deletes the
row inside that gap, the reload used to hand the original caller the NEWER owner's fencing token (or crash
with AssertionError on a deleted row). `_load_claim` now requires the exact generation the statement
returned, and raises IdempotencyClaimLost otherwise.

Determinism: the gap is opened by wrapping `_load_claim` so that, for the chosen call only, a second
PostgreSQL session performs the takeover *before* the original reload runs. No sleeps, no timing.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import collectors as collectors_module
from app.application import idempotency as idem
from tests.conftest import TEST_DATABASE_URL

ENDPOINT = "POST:/adoption-race"
PRINCIPAL = "user:00000000-0000-0000-0000-00000000db06"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    opened = []

    def new_session():
        session = factory()
        opened.append(session)
        return session

    try:
        yield new_session
    finally:
        for session in opened:
            await session.close()
        await engine.dispose()


@pytest.fixture(autouse=True)
def short_poll_window(monkeypatch):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 0.3)


def open_gap(monkeypatch, interloper, *, on_call: int = 1):
    """Run `interloper()` (a second session's takeover) between the claim commit and the reload of call #n."""
    real = idem._load_claim  # noqa: SLF001
    calls = {"n": 0}

    async def gapped(db, acquired):
        calls["n"] += 1
        if calls["n"] == on_call:
            await interloper()
        return await real(db, acquired)

    monkeypatch.setattr(idem, "_load_claim", gapped)
    return calls


def _key() -> str:
    return f"adopt-{uuid.uuid4()}"


async def _claim(session, key, request_hash="h"):
    return await idem.get_or_claim(
        session, principal=PRINCIPAL, key=key, endpoint=ENDPOINT, request_hash=request_hash
    )


async def _row(session, key):
    return (
        await session.execute(
            text(
                "SELECT id, status, claim_generation, request_hash, response_body FROM idempotency_key "
                "WHERE key = :k AND endpoint = :e AND principal = :p"
            ),
            {"k": key, "e": ENDPOINT, "p": PRINCIPAL},
        )
    ).one_or_none()


_EXPIRE = text("UPDATE idempotency_key SET expires_at = now() - interval '1 second' WHERE key = :k")
_STALE = text("UPDATE idempotency_key SET updated_at = now() - interval '5 minutes' WHERE key = :k")


async def _age(session, key, *, expired=False, stale=False):
    if expired:
        await session.execute(_EXPIRE, {"k": key})
    if stale:
        await session.execute(_STALE, {"k": key})
    await session.commit()


async def _business_write_and_complete(session, key, tag, claim):
    """What a legitimate owner does: its business write and claim completion in one transaction."""
    await session.execute(
        text("INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) "
             "VALUES (gen_random_uuid(), 'equipment', :t, 'planned', CAST('{}' AS jsonb))"),
        {"t": tag},
    )
    await idem.complete_claim(session, claim, response_status=201, response_body={"owner": "new", "key": key})
    await session.commit()


async def _assets(session, tag) -> int:
    return (await session.execute(text("SELECT count(*) FROM managed_asset WHERE asset_tag = :t"), {"t": tag})).scalar_one()


async def test_a_stale_reclaim_between_commit_and_reload_cannot_be_adopted(sessions, monkeypatch):
    key, tag = _key(), f"ADOPT-{uuid.uuid4().hex[:8]}"
    admin, original, interloper_session = sessions(), sessions(), sessions()
    outcomes = {}

    async def interloper():
        await _age(admin, key, stale=True)  # the original claim is now stale (owner paused >30 s)
        outcomes["b"] = await _claim(interloper_session, key)  # B reclaims: generation 2

    # The original caller wins a fresh claim (generation 1), then pauses before reloading it.
    open_gap(monkeypatch, interloper, on_call=1)
    with pytest.raises(idem.IdempotencyClaimLost):
        await _claim(original, key)

    claim_b = outcomes["b"].claim
    assert claim_b is not None and claim_b.claim_generation == 2

    # The original caller holds no claim, so it can neither complete nor delete B's claim.
    await idem.release_claim(original, idem.ClaimRef(id=claim_b.id, generation=1))  # its issued generation
    row = await _row(admin, key)
    assert (row.status, row.claim_generation) == ("processing", 2)

    # The legitimate new owner completes; exactly one business effect exists.
    await _business_write_and_complete(interloper_session, key, tag, claim_b)
    row = await _row(admin, key)
    assert (row.status, row.claim_generation, row.response_body["owner"]) == ("completed", 2, "new")
    assert await _assets(admin, tag) == 1


async def test_an_expired_replacement_between_commit_and_reload_cannot_be_adopted(sessions, monkeypatch):
    key = _key()
    admin, original, interloper_session = sessions(), sessions(), sessions()
    await admin.execute(
        text(
            "INSERT INTO idempotency_key (id, principal, key, endpoint, request_hash, status, claim_generation,"
            " response_status, response_body, expires_at, created_at, updated_at) VALUES (gen_random_uuid(), :p, :k,"
            " :e, 'old', 'completed', 3, 201, '{}', now() - interval '1 hour', now(), now())"
        ),
        {"p": PRINCIPAL, "k": key, "e": ENDPOINT},
    )
    await admin.commit()
    outcomes = {}

    async def interloper():
        await _age(admin, key, expired=True)  # the original's fresh generation (4) expires immediately...
        outcomes["b"] = await _claim(interloper_session, key, request_hash="other-body")  # ...and B replaces it (5)

    open_gap(monkeypatch, interloper, on_call=1)
    with pytest.raises(idem.IdempotencyClaimLost):
        await _claim(original, key, request_hash="original-body")

    claim_b = outcomes["b"].claim
    assert claim_b.claim_generation == 5
    row = await _row(admin, key)
    assert (row.claim_generation, row.request_hash) == (5, "other-body")  # B's hash, never the original's
    await idem.complete_claim(interloper_session, claim_b, response_status=201, response_body={"owner": "new"})
    await interloper_session.commit()
    assert (await _row(admin, key)).status == "completed"


async def test_a_claim_deleted_before_the_reload_is_a_controlled_error_not_an_assertion(sessions, monkeypatch):
    key = _key()
    admin, original = sessions(), sessions()

    async def interloper():  # the replacement owner already failed and released (deleted) the row
        await admin.execute(text("DELETE FROM idempotency_key WHERE key = :k"), {"k": key})
        await admin.commit()

    open_gap(monkeypatch, interloper, on_call=1)
    with pytest.raises(idem.IdempotencyClaimLost):  # not AssertionError / HTTP 500
        await _claim(original, key)
    assert await _row(admin, key) is None


async def test_a_row_already_completed_by_another_owner_before_the_reload_is_not_adopted(sessions, monkeypatch):
    key = _key()
    admin, original, other = sessions(), sessions(), sessions()

    async def interloper():  # same generation number, but no longer 'processing'
        await _age(admin, key, stale=True)
        won = await _claim(other, key)
        assert won.claim is not None
        await idem.complete_claim(other, won.claim, response_status=201, response_body={"owner": "other"})
        await other.commit()

    open_gap(monkeypatch, interloper, on_call=1)
    with pytest.raises(idem.IdempotencyClaimLost):
        await _claim(original, key)
    assert (await _row(admin, key)).response_body == {"owner": "other"}


async def test_without_a_takeover_the_acquired_generation_is_returned_exactly(sessions):
    key = _key()
    first = await _claim(sessions(), key)
    assert first.claim.claim_generation == 1 and first.claim.status == "processing"
    await _age(sessions(), key, expired=True)
    second = await _claim(sessions(), key)
    assert second.claim.claim_generation == 2


# --- HTTP level: user endpoint and collector ingest ------------------------------------------------------------------


async def test_user_endpoint_answers_503_request_superseded_and_writes_nothing_when_the_claim_is_taken_over(
    client, auth_headers, db_session, monkeypatch
):
    headers = await auth_headers("Engineer")
    tag = f"ADOPT-{uuid.uuid4().hex[:8]}"
    key = str(uuid.uuid4())
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def interloper():
        async with factory() as s:  # the claim is released/deleted by another request before the reload
            await s.execute(text("DELETE FROM idempotency_key WHERE key = :k"), {"k": key})
            await s.commit()

    open_gap(monkeypatch, interloper, on_call=1)
    try:
        response = await client.post(
            "/api/v1/managed-assets", json={"asset_type": "equipment", "asset_tag": tag},
            headers={**headers, "Idempotency-Key": key},
        )
    finally:
        await engine.dispose()
    assert response.status_code == 503, response.text
    assert response.json()["title"] == "Request Superseded"
    await db_session.rollback()
    assert await _assets(db_session, tag) == 0  # the superseded request performed no business write


async def test_collector_ingest_isolates_a_taken_over_claim_per_record_and_never_deletes_the_new_owners_claim(
    client, auth_headers, db_session, monkeypatch
):
    from tests.api._network_helpers import collector_with_integration, ingest_records

    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")

    def device(external, **raw):
        return {
            "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"], "external_identifier": external,
            "occurred_at": "2026-03-01T10:00:00Z", "raw_attributes": raw,
        }

    taken, sibling = device("10.30.0.1", v="taken"), device("10.30.0.2", v="sibling")
    taken_hash = idem.hash_request_body(collectors_module.IngestRecordIn(**taken).model_dump(mode="json"))
    principal = f"collector:{collector['id']}"
    ledger_key = f"{collector['id']}:{taken['dedup_key']}"
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def interloper():
        async with factory() as s:  # another request makes this key's row stale, then reclaims it (generation 2)
            await s.execute(
                text("UPDATE idempotency_key SET updated_at = now() - interval '5 minutes' WHERE key = :k"),
                {"k": ledger_key},
            )
            await s.commit()
            rival = await idem.get_or_claim(
                s, principal=principal, key=ledger_key, endpoint="collector_ingest", request_hash=taken_hash
            )
            assert rival.claim is not None and rival.claim.claim_generation == 2

    open_gap(monkeypatch, interloper, on_call=1)  # only the first record's acquisition is interrupted
    try:
        out = await ingest_records(client, collector, [taken, sibling])
    finally:
        await engine.dispose()
    by_key = {r["dedup_key"]: r for r in out["results"]}
    assert by_key[taken["dedup_key"]]["status"] == "rejected"
    assert by_key[taken["dedup_key"]]["error_code"] == "PROCESSING"  # retryable; the edge keeps the record
    assert by_key[sibling["dedup_key"]]["status"] == "accepted"  # siblings unaffected

    await db_session.rollback()
    devices = (await db_session.execute(
        text("SELECT external_identifier FROM discovered_device WHERE external_identifier IN ('10.30.0.1','10.30.0.2')")
    )).scalars().all()
    assert devices == ["10.30.0.2"]  # no ingestion for the taken-over record
    surviving = (await db_session.execute(
        text("SELECT status, claim_generation FROM idempotency_key WHERE key = :k"), {"k": ledger_key}
    )).one()
    assert tuple(surviving) == ("processing", 2)  # the new owner's claim was not released or deleted
