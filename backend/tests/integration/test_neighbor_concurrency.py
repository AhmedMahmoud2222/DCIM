"""PostgreSQL proof: concurrent ingestion of one adjacency never duplicates it, and two
operators cannot both confirm conflicting links for the same port."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.network.neighbor_service import confirm_neighbor, ingest_neighbor
from app.core.errors import ConflictError
from tests.api._network_inventory import make_device


def attributes(**remote) -> dict:
    return {
        "scan_id": "s",
        "neighbor": {
            "protocol": "lldp", "local_port": {"name": "Eth1/1", "ref": "1"},
            "remote": {"chassis_id": "core-sw", "port_id": "Eth1/24"} | remote, "capabilities": [], "raw": {},
        },
    }


async def seed_integration(db_engine) -> tuple[uuid.UUID, uuid.UUID]:
    collector_id, integration_id = uuid.uuid4(), uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at) "
            "VALUES (:id, :n, 'central', 'active', 'x', now())"), {"id": collector_id, "n": f"c-{collector_id}"})
        await conn.execute(text(
            "INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, "
            "consecutive_failures, version) VALUES (:id, :n, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"),
            {"id": integration_id, "n": f"i-{integration_id}"})
    return collector_id, integration_id


async def test_concurrent_ingest_of_one_adjacency_yields_one_row(db_engine):
    collector_id, integration_id = await seed_integration(db_engine)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    base = datetime.now(UTC) - timedelta(hours=1)

    async def one(offset: int) -> bool:
        async with factory() as session:
            _neighbor, created = await ingest_neighbor(
                session, collector_id=collector_id, integration_id=integration_id,
                occurred_at=base + timedelta(seconds=offset), raw_attributes=attributes(system_name=f"n{offset}"),
            )
            await session.commit()
            return created

    created = await asyncio.gather(*(one(i) for i in range(24)))
    assert sum(created) == 1  # exactly one inserter, everyone else updated
    async with factory() as session:
        rows = (await session.execute(text(
            "SELECT count(*), min(first_seen_at), max(last_seen_at), (array_agg(remote_system_name ORDER BY last_seen_at DESC))[1] "
            "FROM discovered_neighbor"))).one()
    assert rows[0] == 1
    assert rows[1] == base and rows[2] == base + timedelta(seconds=23)
    assert rows[3] == "n23"  # the newest observation's evidence wins regardless of arrival order


async def test_out_of_order_arrival_converges_to_the_newest_evidence(db_engine):
    collector_id, integration_id = await seed_integration(db_engine)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    base = datetime.now(UTC) - timedelta(hours=1)
    for offset in (5, 1, 9, 3, 7):
        async with factory() as session:
            await ingest_neighbor(session, collector_id=collector_id, integration_id=integration_id,
                                  occurred_at=base + timedelta(seconds=offset), raw_attributes=attributes(system_name=f"n{offset}"))
            await session.commit()
    async with factory() as session:
        row = (await session.execute(text("SELECT remote_system_name, first_seen_at, last_seen_at FROM discovered_neighbor"))).one()
    assert row == ("n9", base + timedelta(seconds=1), base + timedelta(seconds=9))


async def test_two_operators_cannot_confirm_conflicting_links_for_one_port(client, auth_headers, db_engine, db_session):
    headers = await auth_headers("Administrator")
    local = await make_device(client, headers, ["Eth1/1"], hostname="edge-sw")
    remote_a = await make_device(client, headers, ["Eth1/24"], hostname="core-a")
    remote_b = await make_device(client, headers, ["Eth1/24"], hostname="core-b")
    user_id = (await db_session.execute(text("SELECT id FROM app_user LIMIT 1"))).scalar_one()
    collector_id, integration_id = await seed_integration(db_engine)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    ids = []
    for chassis in ("core-a", "core-b"):
        async with factory() as session:
            neighbor, _ = await ingest_neighbor(
                session, collector_id=collector_id, integration_id=integration_id, occurred_at=datetime.now(UTC) - timedelta(minutes=1),
                raw_attributes=attributes(chassis_id=chassis, port_id="Eth1/24"))
            await session.commit()
            ids.append(neighbor.id)

    async def confirm(neighbor_id, remote_port_id):
        async with factory() as session:
            try:
                await confirm_neighbor(
                    session, neighbor_id=neighbor_id, local_port_id=uuid.UUID(local["port_by_name"]["Eth1/1"]),
                    remote_port_id=uuid.UUID(remote_port_id), expected_version=1, reason=None, actor_user_id=user_id,
                    request_id=None, correlation_id=None)
                await session.commit()
                return "confirmed"
            except ConflictError:
                await session.rollback()
                return "conflict"

    outcomes = await asyncio.gather(
        confirm(ids[0], remote_a["port_by_name"]["Eth1/24"]), confirm(ids[1], remote_b["port_by_name"]["Eth1/24"]))
    assert sorted(outcomes) == ["confirmed", "conflict"]
    async with factory() as session:
        confirmed = (await session.execute(text("SELECT count(*) FROM discovered_neighbor WHERE reconciliation_state='confirmed'"))).scalar_one()
    assert confirmed == 1


@pytest.mark.parametrize("state", ["confirmed", "rejected"])
async def test_migration_downgrade_refuses_to_discard_operator_decisions(db_engine, state):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    collector_id, integration_id = await seed_integration(db_engine)
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0038_discovered_neighbors.py"
    spec = importlib.util.spec_from_file_location("neighbors_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()

    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text("""
                INSERT INTO discovered_neighbor (id, integration_id, protocol, identity_key, first_seen_at, last_seen_at,
                    local_port_name, remote_chassis_ident, remote_port_ident, capabilities, raw_evidence, reconciliation_state)
                VALUES (:id, :i, 'lldp', :k, now(), now(), 'e1', 'c', 'p', '[]', '{}', :s)"""),
                {"id": uuid.uuid4(), "i": integration_id, "k": uuid.uuid4().hex, "s": state})
            with pytest.raises(RuntimeError, match="Refusing to drop discovered_neighbor"):
                await conn.run_sync(run, "downgrade")
            await conn.execute(text("UPDATE discovered_neighbor SET reconciliation_state = 'unmatched'"))
            await conn.run_sync(run, "downgrade")  # undecided evidence may be dropped
            assert await conn.scalar(text("SELECT to_regclass('discovered_neighbor')")) is None
            await conn.run_sync(run, "upgrade")
            assert await conn.scalar(text("SELECT count(*) FROM discovered_neighbor")) == 0
        finally:
            await tx.rollback()
    del collector_id
