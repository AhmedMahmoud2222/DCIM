"""True-concurrency proof for If-Match mutations (real PostgreSQL, independent sessions).

A sequential stale-version test cannot show atomicity: it never lets two requests hold the
same pre-mutation version at once. Here a separate connection holds the row (or room) lock
while both requests are sent with the same If-Match: N. The test then waits until PostgreSQL
itself reports both requests blocked on that lock (so both are provably in flight with
version N), releases it, and checks that exactly one request consumed version N.

Against the previous load / compare / mutate pattern neither request blocks, the barrier
below never fills, and these tests fail."""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import get_db
from app.main import app
from tests.api._phase2_helpers import create_equipment, create_rack, create_room
from tests.api._phase3_helpers import connection_body, create_pdu
from tests.api._phase8_helpers import create_integration

BARRIER_TIMEOUT_S = 20


@pytest_asyncio.fixture
async def race(db_engine, _admin_engine):
    """`await race(lock_sql, lock_params, calls)` -> list of responses, in `calls` order.

    `calls` are (method, url, kwargs). Each request gets its own AsyncSession/connection."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _per_request_session():
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    async def _blocked_backends() -> int:
        async with _admin_engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'active' AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()

    async def run(lock_sql: str, lock_params: dict, calls: list[tuple[str, str, dict]]):
        previous = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = _per_request_session
        try:
            async with db_engine.connect() as holder:
                await holder.execute(text(lock_sql), lock_params)  # blocks everything that needs this lock
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                    tasks = [asyncio.create_task(http.request(method, url, **kwargs)) for method, url, kwargs in calls]
                    deadline = asyncio.get_running_loop().time() + BARRIER_TIMEOUT_S
                    while await _blocked_backends() < len(calls):
                        assert asyncio.get_running_loop().time() < deadline, (
                            "requests never overlapped on the row/room lock: the mutation is not lock-protected"
                        )
                        assert not any(t.done() for t in tasks), "a request completed without waiting for the lock"
                        await asyncio.sleep(0.05)
                    await holder.rollback()  # release: both requests now race for the lock with the same If-Match
                    return await asyncio.gather(*tasks)
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = previous

    return run


async def _scalar(db_engine, sql: str, **params):
    async with db_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _history_counts(db_engine, entity_id: str, aggregate_id: str | None = None) -> tuple[int, int]:
    audit = await _scalar(db_engine, "SELECT count(*) FROM audit_log WHERE entity_id = :id", id=uuid.UUID(entity_id))
    outbox = await _scalar(
        db_engine, "SELECT count(*) FROM outbox_event WHERE aggregate_id = :id", id=uuid.UUID(aggregate_id or entity_id)
    )
    return audit, outbox


# ---------------------------------------------------------------------------------- resources

async def _integration(client, auth_headers):
    headers = await auth_headers()
    item = await create_integration(client, headers)
    return dict(
        headers=headers, id=item["id"], version=item["version"], url=f"/api/v1/integrations/{item['id']}", table="integration",
        a={"enabled": False, "poll_interval_seconds": 300}, b={"enabled": False, "poll_interval_seconds": 900},
        column="poll_interval_seconds", key="poll_interval_seconds", audit_delta=1, outbox_delta=1,
    )


async def _rack(client, auth_headers):
    headers = await auth_headers()
    item = await create_rack(client, headers, auth_headers)
    return dict(
        headers=headers, id=item["id"], version=item["version"], url=f"/api/v1/racks/{item['id']}", table="rack",
        a={"name": "rack-winner-a"}, b={"name": "rack-winner-b"}, column="name", key="name", audit_delta=1, outbox_delta=1,
    )


async def _room(client, auth_headers):
    headers = await auth_headers()
    room_id = await create_room(client, auth_headers)
    item = (await client.get(f"/api/v1/rooms/{room_id}", headers=headers)).json()
    return dict(
        headers=headers, id=room_id, version=item["version"], url=f"/api/v1/rooms/{room_id}", table="room",
        a={"name": "room-winner-a"}, b={"name": "room-winner-b"}, column="name", key="name", audit_delta=1, outbox_delta=0,
    )


async def _equipment(client, auth_headers):
    headers = await auth_headers()
    item = await create_equipment(client, headers, auth_headers)
    return dict(
        headers=headers, id=item["id"], version=item["version"], url=f"/api/v1/equipment/{item['id']}", table="equipment",
        a={"hostname": "host-a"}, b={"hostname": "host-b"}, column="hostname", key="hostname", audit_delta=1, outbox_delta=1,
    )


async def _power_connection(client, auth_headers):
    headers = await auth_headers()
    source, target = await create_pdu(client, headers), await create_pdu(client, headers)
    item = (await client.post("/api/v1/power/connections", json=connection_body(source["id"], target["id"]), headers=headers)).json()
    return dict(
        headers=headers, id=item["id"], version=item["version"], url=f"/api/v1/power/connections/{item['id']}",
        table="power_connection", a={"status": "maintenance"}, b={"status": "fault"}, column="status", key="status",
        audit_delta=1, outbox_delta=1,
    )


RESOURCES = [_integration, _rack, _room, _equipment, _power_connection]


@pytest.mark.parametrize("make", RESOURCES, ids=lambda f: f.__name__.lstrip("_"))
async def test_concurrent_patch_with_same_if_match_lets_exactly_one_consume_the_version(
    make, client, auth_headers, db_engine, race
):
    r = await make(client, auth_headers)
    rid = uuid.UUID(r["id"])
    audit_before, outbox_before = await _history_counts(db_engine, r["id"])
    headers = {**r["headers"], "If-Match": str(r["version"])}

    responses = await race(
        f"SELECT id FROM {r['table']} WHERE id = :id FOR UPDATE", {"id": rid},
        [("PATCH", r["url"], {"json": r["a"], "headers": headers}), ("PATCH", r["url"], {"json": r["b"], "headers": headers})],
    )

    statuses = sorted(resp.status_code for resp in responses)
    assert statuses == [200, 409], [(resp.status_code, resp.text) for resp in responses]
    winner = next(body for resp, body in zip(responses, (r["a"], r["b"]), strict=True) if resp.status_code == 200)
    # Version N consumed once; the value is the winner's, not a silent overwrite by the loser.
    assert await _scalar(db_engine, f"SELECT version FROM {r['table']} WHERE id = :id", id=rid) == r["version"] + 1
    assert await _scalar(db_engine, f"SELECT {r['column']} FROM {r['table']} WHERE id = :id", id=rid) == winner[r["key"]]
    # History records only the committed mutation.
    audit_after, outbox_after = await _history_counts(db_engine, r["id"])
    assert audit_after - audit_before == r["audit_delta"]
    assert outbox_after - outbox_before == r["outbox_delta"]
    # The next request must use the incremented version, and a retry of the loser now succeeds.
    loser = next(body for resp, body in zip(responses, (r["a"], r["b"]), strict=True) if resp.status_code == 409)
    retry = await client.patch(r["url"], json=loser, headers={**r["headers"], "If-Match": str(r["version"] + 1)})
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == r["version"] + 2


# ---------------------------------------------------------------------------------- FloorPlan

async def _floor_plan_room(client, auth_headers, plans: int, *, active_first: bool = False):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    created = []
    for _ in range(plans):
        resp = await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)
        assert resp.status_code == 201, resp.text
        created.append(resp.json())
    if active_first:
        resp = await client.post(
            f"/api/v1/floor-plans/{created[0]['id']}/activate", headers={**headers, "If-Match": str(created[0]["version"])}
        )
        assert resp.status_code == 200, resp.text
        created[0] = resp.json()
    return headers, room_id, created


async def _active_plans(db_engine, room_id: str) -> list[str]:
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM floor_plan WHERE room_id = :r AND status = 'active'"), {"r": uuid.UUID(room_id)}
        )
        return [str(row[0]) for row in rows]


_ROOM_LOCK = "SELECT id FROM room WHERE id = :id FOR NO KEY UPDATE"


async def test_concurrent_activation_of_the_same_floor_plan_lets_one_consume_the_version(
    client, auth_headers, db_engine, race
):
    headers, room_id, (plan,) = await _floor_plan_room(client, auth_headers, 1)
    audit_before, outbox_before = await _history_counts(db_engine, plan["id"])
    call = ("POST", f"/api/v1/floor-plans/{plan['id']}/activate", {"headers": {**headers, "If-Match": str(plan["version"])}})

    responses = await race(_ROOM_LOCK, {"id": uuid.UUID(room_id)}, [call, call])

    assert sorted(r.status_code for r in responses) == [200, 409], [(r.status_code, r.text) for r in responses]
    assert await _active_plans(db_engine, room_id) == [plan["id"]]
    assert await _scalar(db_engine, "SELECT version FROM floor_plan WHERE id = :id", id=uuid.UUID(plan["id"])) == plan["version"] + 1
    audit_after, outbox_after = await _history_counts(db_engine, plan["id"])
    assert (audit_after - audit_before, outbox_after - outbox_before) == (1, 1)


@pytest.mark.parametrize("previously_active", [False, True], ids=["no_prior_active", "prior_active_plan"])
async def test_concurrent_activation_of_two_plans_in_one_room_leaves_exactly_one_active(
    previously_active, client, auth_headers, db_engine, race
):
    headers, room_id, plans = await _floor_plan_room(client, auth_headers, 3 if previously_active else 2, active_first=previously_active)
    contenders = plans[-2:]
    prior = plans[0] if previously_active else None
    calls = [
        ("POST", f"/api/v1/floor-plans/{p['id']}/activate", {"headers": {**headers, "If-Match": str(p["version"])}})
        for p in contenders
    ]

    history_before = {p["id"]: await _history_counts(db_engine, p["id"]) for p in contenders}

    responses = await race(_ROOM_LOCK, {"id": uuid.UUID(room_id)}, calls)

    # Neither an uncontrolled IntegrityError (500) nor two winners: one 200, one controlled 409.
    assert sorted(r.status_code for r in responses) == [200, 409], [(r.status_code, r.text) for r in responses]
    winner = next(p for p, r in zip(contenders, responses, strict=True) if r.status_code == 200)
    loser = next(p for p, r in zip(contenders, responses, strict=True) if r.status_code == 409)
    assert await _active_plans(db_engine, room_id) == [winner["id"]]
    assert await _scalar(db_engine, "SELECT status FROM floor_plan WHERE id = :id", id=uuid.UUID(loser["id"])) == "draft"
    assert await _scalar(db_engine, "SELECT version FROM floor_plan WHERE id = :id", id=uuid.UUID(loser["id"])) == loser["version"]
    if prior is not None:
        assert await _scalar(db_engine, "SELECT status FROM floor_plan WHERE id = :id", id=uuid.UUID(prior["id"])) == "superseded"
        assert await _scalar(db_engine, "SELECT version FROM floor_plan WHERE id = :id", id=uuid.UUID(prior["id"])) == prior["version"] + 1
    # Only the committed activation is in the history.
    winner_audit, winner_outbox = await _history_counts(db_engine, winner["id"])
    assert (winner_audit - history_before[winner["id"]][0], winner_outbox - history_before[winner["id"]][1]) == (1, 1)
    assert await _history_counts(db_engine, loser["id"]) == history_before[loser["id"]]
