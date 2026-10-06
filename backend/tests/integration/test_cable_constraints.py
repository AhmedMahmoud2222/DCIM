"""PostgreSQL proof of cable integrity: endpoints, liveness, lifecycle, uniqueness, concurrency
and the migration's upgrade/downgrade behaviour. These are the guarantees the application
code relies on; they hold even if a bug bypasses the service layer."""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.access_control import AccessScope
from app.application.network.cable_service import create_cable
from app.core.errors import ConflictError
from tests.api._network_inventory import make_device

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0039_cables.py"
UNRESTRICTED = AccessScope(unrestricted=True)


@pytest.fixture
async def ports(client, auth_headers):
    headers = await auth_headers("Administrator")
    left = await make_device(client, headers, ["p1", "p2", "p3"], hostname="left")
    right = await make_device(client, headers, ["p1", "p2", "p3"], hostname="right")
    user_id = (await client_user(client, headers))
    return {"left": left["port_by_name"], "right": right["port_by_name"], "user_id": user_id}


async def client_user(client, headers) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])


INSERT_CABLE = """INSERT INTO cable (id, label, cable_type, status, installed_at, removed_at)
                  VALUES (:id, :label, 'copper_utp', :status, :installed, :removed)"""
INSERT_ENDPOINT = "INSERT INTO cable_endpoint (id, cable_id, is_live, end_label, equipment_port_id) VALUES (:id, :cable, :live, :end, :port)"


async def add_cable(conn, label, a, b, *, status="installed", live=True):
    cable_id = uuid.uuid4()
    await conn.execute(text(INSERT_CABLE), {
        "id": cable_id, "label": label, "status": status,
        "installed": datetime(2026, 1, 1, tzinfo=UTC) if status != "planned" else None,
        "removed": datetime(2026, 2, 1, tzinfo=UTC) if status == "removed" else None})
    for end, port in (("A", a), ("B", b)):
        await conn.execute(text(INSERT_ENDPOINT), {"id": uuid.uuid4(), "cable": cable_id, "live": live, "end": end, "port": port})
    return cable_id


async def test_every_cable_needs_exactly_one_a_and_one_b_endpoint_at_commit(db_engine, ports):
    left, right = ports["left"], ports["right"]
    for endpoints in ([], [("A", left["p1"])], [("B", left["p1"])], [("A", left["p1"]), ("A", right["p1"])]):
        async with db_engine.connect() as conn:
            cid = uuid.uuid4()
            await conn.execute(text(INSERT_CABLE), {"id": cid, "label": f"bad-{cid}", "status": "planned", "installed": None, "removed": None})
            rejected_early = False
            for end, port in endpoints:
                try:
                    await conn.execute(text(INSERT_ENDPOINT), {"id": uuid.uuid4(), "cable": cid, "live": True, "end": end, "port": port})
                except IntegrityError:
                    rejected_early = True  # a duplicate end is already refused by its unique constraint
                    break
            if rejected_early:
                await conn.rollback()
                continue
            with pytest.raises((DBAPIError, IntegrityError), match="exactly one A and one B"):
                await conn.commit()
    async with db_engine.connect() as conn:
        assert (await conn.scalar(text("SELECT count(*) FROM cable"))) == 0
    async with db_engine.begin() as conn:  # a correct pair commits
        await add_cable(conn, "good", left["p1"], right["p1"], status="planned")


async def test_one_port_cannot_terminate_both_ends_or_two_live_cables(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.connect() as conn:
        await conn.execute(text(INSERT_CABLE), {"id": (cid := uuid.uuid4()), "label": "loop", "status": "planned", "installed": None, "removed": None})
        await conn.execute(text(INSERT_ENDPOINT), {"id": uuid.uuid4(), "cable": cid, "live": True, "end": "A", "port": left["p1"]})
        with pytest.raises(IntegrityError):
            await conn.execute(text(INSERT_ENDPOINT), {"id": uuid.uuid4(), "cable": cid, "live": True, "end": "B", "port": left["p1"]})
        await conn.rollback()
    async with db_engine.begin() as conn:
        await add_cable(conn, "first", left["p1"], right["p1"])
    async with db_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await add_cable(conn, "second", left["p1"], right["p2"])  # port already carries a live cable
        await conn.rollback()


async def test_removal_cascades_liveness_and_frees_the_port(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.begin() as conn:
        cid = await add_cable(conn, "old", left["p1"], right["p1"])
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE cable SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": cid})
        assert (await conn.execute(text("SELECT count(*) FROM cable_endpoint WHERE cable_id = :c AND is_live"), {"c": cid})).scalar_one() == 0
    async with db_engine.begin() as conn:  # same ports and same label are reusable
        await add_cable(conn, "OLD", left["p1"], right["p1"])
    async with db_engine.connect() as conn:
        with pytest.raises(IntegrityError):  # but two live cables cannot share a (case-insensitive) label
            await add_cable(conn, "old", left["p2"], right["p2"])
        await conn.rollback()


async def test_endpoint_liveness_cannot_disagree_with_its_cable(db_engine, ports):
    left = ports["left"]
    async with db_engine.connect() as conn:
        await conn.execute(text(INSERT_CABLE), {"id": (cid := uuid.uuid4()), "label": "live", "status": "planned", "installed": None, "removed": None})
        with pytest.raises(IntegrityError):
            await conn.execute(text(INSERT_ENDPOINT), {"id": uuid.uuid4(), "cable": cid, "live": False, "end": "A", "port": left["p1"]})
        await conn.rollback()


async def test_lifecycle_guard_and_immutability(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.begin() as conn:
        planned = await add_cable(conn, "plan", left["p1"], right["p1"], status="planned")
        installed = await add_cable(conn, "inst", left["p2"], right["p2"])
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE cable SET status = 'installed', installed_at = now() WHERE id = :id"), {"id": planned})
    for sql, why in (
        ("UPDATE cable SET status = 'planned', installed_at = NULL WHERE id = :id", "installed -> planned"),
        ("UPDATE cable SET status = 'installed', installed_at = now(), removed_at = now() WHERE id = :id", "timestamps vs status"),
        ("UPDATE cable SET label = '' WHERE id = :id", "blank label"),
        ("UPDATE cable SET length_m = 0 WHERE id = :id", "non-positive length"),
        ("UPDATE cable SET cable_type = 'laser' WHERE id = :id", "unknown type"),
        ("UPDATE cable SET route_metadata = '[]' WHERE id = :id", "route must be an object"),
        ("UPDATE cable SET source = 'discovery_confirmed', source_neighbor_id = gen_random_uuid() WHERE id = :id", "dangling provenance"),
    ):
        async with db_engine.connect() as conn:
            with pytest.raises((IntegrityError, DBAPIError)):
                await conn.execute(text(sql), {"id": installed})
            await conn.rollback()
        del why
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE cable SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": installed})
    for sql in (
        "UPDATE cable SET status = 'installed', removed_at = NULL WHERE id = :id",
        "UPDATE cable SET label = 'rewritten' WHERE id = :id",
        "UPDATE cable SET notes = 'edited history' WHERE id = :id",
    ):
        async with db_engine.connect() as conn:
            with pytest.raises((IntegrityError, DBAPIError)):
                await conn.execute(text(sql), {"id": installed})
            await conn.rollback()


async def test_endpoints_cannot_be_re_terminated(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.begin() as conn:
        await add_cable(conn, "fixed", left["p1"], right["p1"])
    for sql in (
        "UPDATE cable_endpoint SET equipment_port_id = :other WHERE end_label = 'A'",
        "UPDATE cable_endpoint SET end_label = 'B' WHERE end_label = 'A'",
    ):
        async with db_engine.connect() as conn:
            with pytest.raises((IntegrityError, DBAPIError)):
                await conn.execute(text(sql), {"other": left["p3"]})
            await conn.rollback()


async def test_equipment_with_cable_history_cannot_lose_its_ports(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.begin() as conn:
        await add_cable(conn, "keep", left["p1"], right["p1"], status="removed", live=False)
    async with db_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(text("DELETE FROM equipment_port WHERE id = :id"), {"id": left["p1"]})
        await conn.rollback()


async def test_concurrent_cabling_of_one_port_has_exactly_one_winner(db_engine, ports):
    left, right = ports["left"], ports["right"]
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def attempt(label, remote):
        async with factory() as session:
            try:
                await create_cable(
                    session, scope=UNRESTRICTED, label=label, cable_type="copper_utp", connector_a=None, connector_b=None,
                    length_m=None, route_metadata={}, notes=None, port_a_id=uuid.UUID(left["p1"]), port_b_id=uuid.UUID(remote),
                    status="installed", installed_at=None, source="manual", source_neighbor_id=None,
                    actor_user_id=ports["user_id"], request_id=None, correlation_id=None)
                await session.commit()
                return "created"
            except ConflictError:
                await session.rollback()
                return "conflict"

    results = await asyncio.gather(*(attempt(f"race-{i}", right[p]) for i, p in enumerate(("p1", "p2", "p3", "p1", "p2", "p3"))))
    assert sorted(results).count("created") == 1 and results.count("conflict") == 5
    async with db_engine.connect() as conn:
        assert (await conn.scalar(text("SELECT count(*) FROM cable WHERE is_live"))) == 1
        assert (await conn.scalar(text("SELECT count(*) FROM port_connection"))) == 1
        assert (await conn.scalar(text("SELECT count(*) FROM cable_endpoint"))) == 2


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("cables_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_migration_downgrade_refuses_to_discard_cables_and_round_trips_when_empty(db_engine, ports, db_session):
    await db_session.rollback()  # release the fixture session's table locks; DROP TABLE needs exclusive access
    left, right = ports["left"], ports["right"]
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await add_cable(conn, "history", left["p1"], right["p1"], status="removed", live=False)
            with pytest.raises(RuntimeError, match="Refusing to drop cables"):
                await conn.run_sync(_run, "downgrade")
        finally:
            await tx.rollback()
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")
            assert await conn.scalar(text("SELECT to_regclass('cable')")) is None
            assert await conn.scalar(text("SELECT count(*) FROM permission WHERE resource = 'cable'")) == 0
            assert await conn.scalar(text("SELECT to_regproc('cable_require_two_endpoints')")) is None
            await conn.run_sync(_run, "upgrade")
            assert await conn.scalar(text("SELECT count(*) FROM permission WHERE resource = 'cable'")) == 2
            managers = {r[0] for r in await conn.execute(text(
                "SELECT r.name FROM role r JOIN role_permission rp ON rp.role_id = r.id JOIN permission p ON p.id = rp.permission_id "
                "WHERE p.resource = 'cable' AND p.action = 'manage'"))}
            assert managers == {"Administrator", "DCIM Manager", "Engineer"}
            await add_cable(conn, "after-roundtrip", left["p2"], right["p2"], status="planned")
        finally:
            await tx.rollback()


async def test_history_cannot_be_deleted_and_endpoints_cannot_be_replaced(db_engine, ports):
    left, right = ports["left"], ports["right"]
    async with db_engine.begin() as conn:
        installed = await add_cable(conn, "immutable", left["p1"], right["p1"])
    async with db_engine.connect() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(text("DELETE FROM cable_endpoint WHERE cable_id = :id"), {"id": installed})
        await conn.rollback()
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(text("DELETE FROM cable WHERE id = :id"), {"id": installed})
        await conn.rollback()
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE cable SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": installed})
    async with db_engine.connect() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(text("DELETE FROM cable WHERE id = :id"), {"id": installed})
        await conn.rollback()
