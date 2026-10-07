"""Stale-owner race (NEW-1) regression coverage, against real PostgreSQL.

Before fencing, a claim owner that was merely slow (not dead) could resume after another
request reclaimed its stale 'processing' row and then complete, overwrite or delete the
replacement owner's claim. Each claim now carries a monotonically increasing
`claim_generation`; complete_claim()/release_claim() only act while the caller still holds
the generation it was issued.

Determinism: staleness is produced by back-dating `updated_at` in SQL, never by sleeping, and
the ownership interleavings are driven step by step. Each actor uses its own AsyncSession
(its own connection), exactly as one request would in production.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import idempotency as idem
from app.core.errors import register_exception_handlers
from tests.conftest import TEST_DATABASE_URL

ENDPOINT = "POST:/fencing-test"


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


async def _make_stale(session, key: str) -> None:
    await session.execute(
        text("UPDATE idempotency_key SET updated_at = now() - interval '5 minutes' WHERE key = :k"), {"k": key}
    )
    await session.commit()


async def _row(session, key: str):
    result = await session.execute(
        text(
            "SELECT status, claim_generation, response_status, response_body "
            "FROM idempotency_key WHERE key = :k AND endpoint = :e"
        ),
        {"k": key, "e": ENDPOINT},
    )
    return result.one_or_none()


async def _claim(session, key: str, request_hash: str = "h"):
    return await idem.get_or_claim(session, key=key, endpoint=ENDPOINT, request_hash=request_hash)


async def test_normal_claim_then_complete_then_replay(sessions):
    key = str(uuid.uuid4())
    owner, replayer = sessions(), sessions()

    outcome = await _claim(owner, key)
    assert outcome.claim is not None and outcome.claim.claim_generation == 1
    await idem.complete_claim(owner, outcome.claim, response_status=201, response_body={"id": "A"})
    await owner.commit()

    replay = await _claim(replayer, key)
    assert replay.claim is None and replay.cached is not None
    assert replay.cached.response_status == 201 and replay.cached.response_body == {"id": "A"}
    assert tuple(await _row(owner, key)) == ("completed", 1, 201, {"id": "A"})


async def test_different_body_with_same_key_still_conflicts(sessions):
    key = str(uuid.uuid4())
    await _claim(sessions(), key, "hash-1")
    with pytest.raises(idem.IdempotencyConflict):
        await _claim(sessions(), key, "hash-2")


async def test_fresh_claim_is_not_reclaimable_and_waiter_times_out(sessions, monkeypatch):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 0.2)
    key = str(uuid.uuid4())
    await _claim(sessions(), key)
    with pytest.raises(idem.IdempotencyStillProcessing):
        await _claim(sessions(), key)
    assert (await _row(sessions(), key)).claim_generation == 1


async def test_stale_claim_is_recovered_with_a_higher_generation(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    await _make_stale(admin, key)

    outcome_b = await _claim(b, key)
    assert outcome_b.claim is not None
    assert outcome_b.claim.id == claim_a.id
    assert outcome_b.claim.claim_generation == 2

    await idem.complete_claim(b, outcome_b.claim, response_status=201, response_body={"owner": "B"})
    await b.commit()
    assert tuple(await _row(admin, key)) == ("completed", 2, 201, {"owner": "B"})


async def test_old_owner_cannot_complete_after_reclaim(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    await _make_stale(admin, key)
    claim_b = (await _claim(b, key)).claim
    assert claim_b is not None

    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(a, claim_a, response_status=201, response_body={"owner": "A"})
    await a.rollback()

    # The replacement owner still holds a valid, completable claim.
    assert tuple(await _row(admin, key))[:2] == ("processing", 2)
    await idem.complete_claim(b, claim_b, response_status=201, response_body={"owner": "B"})
    await b.commit()
    assert tuple(await _row(admin, key)) == ("completed", 2, 201, {"owner": "B"})


async def test_old_owner_cannot_overwrite_new_owners_completed_result(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    await _make_stale(admin, key)
    claim_b = (await _claim(b, key)).claim
    await idem.complete_claim(b, claim_b, response_status=201, response_body={"owner": "B"})
    await b.commit()

    with pytest.raises(idem.IdempotencyClaimLost):
        await idem.complete_claim(a, claim_a, response_status=201, response_body={"owner": "A"})
    await a.rollback()

    assert tuple(await _row(admin, key)) == ("completed", 2, 201, {"owner": "B"})


async def test_old_owner_release_does_not_delete_new_owners_claim(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    ref_a = idem.ClaimRef.of(claim_a)
    await _make_stale(admin, key)
    claim_b = (await _claim(b, key)).claim

    await idem.release_claim(a, ref_a)  # the fenced-out owner's cleanup path

    row = await _row(admin, key)
    assert row is not None and tuple(row)[:2] == ("processing", 2)
    await idem.complete_claim(b, claim_b, response_status=201, response_body={"owner": "B"})
    await b.commit()
    assert tuple(await _row(admin, key))[0] == "completed"


async def test_old_owner_release_does_not_delete_new_owners_completed_row(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    ref_a = idem.ClaimRef.of((await _claim(a, key)).claim)
    await _make_stale(admin, key)
    claim_b = (await _claim(b, key)).claim
    await idem.complete_claim(b, claim_b, response_status=201, response_body={"owner": "B"})
    await b.commit()

    await idem.release_claim(a, ref_a)

    assert tuple(await _row(admin, key)) == ("completed", 2, 201, {"owner": "B"})


async def test_owner_release_removes_its_own_claim_so_retry_can_claim_fresh(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    ref_a = idem.ClaimRef.of((await _claim(a, key)).claim)

    await idem.release_claim(a, ref_a)
    assert await _row(admin, key) is None

    retry = await _claim(b, key)
    assert retry.claim is not None and retry.claim.claim_generation == 1


async def test_concurrent_reclaim_attempts_produce_exactly_one_owner(sessions, monkeypatch):
    monkeypatch.setattr(idem, "_POLL_MAX_WAIT_SECONDS", 0.5)
    key = str(uuid.uuid4())
    original, admin = sessions(), sessions()
    await _claim(original, key)
    await _make_stale(admin, key)

    contenders = [sessions() for _ in range(8)]
    results = await asyncio.gather(*[_claim(s, key) for s in contenders], return_exceptions=True)

    owners = [r for r in results if isinstance(r, idem.IdempotencyOutcome) and r.claim is not None]
    assert len(owners) == 1, results
    assert owners[0].claim.claim_generation == 2
    for other in results:
        if other is not owners[0]:
            assert isinstance(other, idem.IdempotencyStillProcessing), other
    assert (await _row(admin, key)).claim_generation == 2


async def test_completion_racing_a_reclaim_leaves_a_single_winner(sessions):
    """The old owner's completion holds the row lock (uncommitted) while a reclaimer's CAS is
    in flight. Whichever order PostgreSQL serializes them, only one party ends up valid."""
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    await _make_stale(admin, key)

    # A completes first but has not committed yet: it holds the row lock.
    await idem.complete_claim(a, claim_a, response_status=201, response_body={"owner": "A"})

    reclaim = asyncio.create_task(_claim(b, key))
    await asyncio.sleep(0.2)  # let B's reclaim reach the row lock; correctness does not depend on it
    assert not reclaim.done()
    await a.commit()

    outcome_b = await asyncio.wait_for(reclaim, timeout=10)
    # B must observe A's completed result and replay it, never obtain a second owner.
    assert outcome_b.claim is None and outcome_b.cached is not None
    assert outcome_b.cached.response_body == {"owner": "A"}
    assert tuple(await _row(admin, key)) == ("completed", 1, 201, {"owner": "A"})


async def test_rolled_back_completion_leaves_a_recoverable_processing_claim(sessions):
    key = str(uuid.uuid4())
    a, b, admin = sessions(), sessions(), sessions()
    claim_a = (await _claim(a, key)).claim
    await idem.complete_claim(a, claim_a, response_status=201, response_body={"owner": "A"})
    await a.rollback()  # domain write failed after the claim was completed in-transaction

    assert tuple(await _row(admin, key))[:2] == ("processing", 1)
    await _make_stale(admin, key)
    recovered = (await _claim(b, key)).claim
    assert recovered is not None and recovered.claim_generation == 2
    await idem.complete_claim(b, recovered, response_status=201, response_body={"owner": "B"})
    await b.commit()
    assert tuple(await _row(admin, key)) == ("completed", 2, 201, {"owner": "B"})


async def test_claim_lost_is_mapped_to_a_retryable_503_problem():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/lost")
    async def lost():
        raise idem.IdempotencyClaimLost()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/lost")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == "Request Superseded"
