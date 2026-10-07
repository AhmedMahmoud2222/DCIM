"""PostgreSQL proof of pass-through integrity (works even if a bug bypasses the service layer), true
concurrency for pass-through writers, and the 0040 migration against populated data."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.api import _pass_through_helpers as h
from tests.api._cable_helpers import CABLES, create_cable
from tests.api._network_inventory import make_device
from tests.integration.test_atomic_optimistic_concurrency import _scalar, race  # noqa: F401

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations/versions"


async def _equipment_of(db_engine, port_id) -> uuid.UUID:
    return await _scalar(db_engine, "SELECT equipment_id FROM equipment_port WHERE id = :p", p=port_id)


INSERT_PT = "INSERT INTO port_pass_through (id, equipment_id) VALUES (:id, :e)"
INSERT_MEMBER = "INSERT INTO port_pass_through_member (id, pass_through_id, equipment_id, equipment_port_id) VALUES (gen_random_uuid(), :pt, :e, :p)"


@pytest.fixture
async def panels(client, auth_headers):
    headers = await auth_headers("Administrator")
    one = await h.patch_panel(client, headers, "c-one", pairs=2)
    two = await h.patch_panel(client, headers, "c-two")
    return {"headers": headers, "one": one["port_by_name"], "one_id": one["id"], "two": two["port_by_name"], "two_id": two["id"]}


async def _insert_pair(conn, equipment_id, a, b):
    pt = uuid.uuid4()
    await conn.execute(text(INSERT_PT), {"id": pt, "e": equipment_id})
    for port in (a, b):
        await conn.execute(text(INSERT_MEMBER), {"pt": pt, "e": equipment_id, "p": port})
    return pt


async def test_a_valid_pair_commits_and_each_rule_is_enforced_by_the_database(db_engine, panels, db_session):
    await db_session.rollback()
    e = uuid.UUID(panels["one_id"])
    async with db_engine.begin() as conn:
        await _insert_pair(conn, e, panels["one"]["Front01"], panels["one"]["Rear01"])

    async def fails(statements, *, commit_fails=False):
        async with db_engine.connect() as conn:
            tx = await conn.begin()
            try:
                with pytest.raises((IntegrityError, DBAPIError)):
                    for sql, params in statements:
                        await conn.execute(text(sql), params)
                    if commit_fails:
                        await tx.commit()
            finally:
                if tx.is_active:
                    await tx.rollback()

    # a port is in at most one pass-through
    pt2 = uuid.uuid4()
    await fails([(INSERT_PT, {"id": pt2, "e": e}), (INSERT_MEMBER, {"pt": pt2, "e": e, "p": panels["one"]["Front01"]})])
    # members must belong to the pass-through's equipment (composite foreign key)
    other = uuid.UUID(panels["two_id"])
    pt3 = uuid.uuid4()
    await fails([(INSERT_PT, {"id": pt3, "e": other}), (INSERT_MEMBER, {"pt": pt3, "e": other, "p": panels["one"]["Front02"]})])
    # exactly two members at commit: one member alone is refused...
    pt4 = uuid.uuid4()
    await fails(
        [(INSERT_PT, {"id": pt4, "e": e}), (INSERT_MEMBER, {"pt": pt4, "e": e, "p": panels["one"]["Front02"]}), ("SET CONSTRAINTS ALL IMMEDIATE", {})],
    )
    # members cannot be re-pointed
    await fails([("UPDATE port_pass_through_member SET equipment_port_id = :p WHERE equipment_port_id = :q",
                  {"p": panels["one"]["Front02"], "q": panels["one"]["Front01"]})])
    # a member cannot be dropped while its pass-through stays
    await fails([("DELETE FROM port_pass_through_member WHERE equipment_port_id = :q", {"q": panels["one"]["Front01"]}),
                 ("SET CONSTRAINTS ALL IMMEDIATE", {})])
    assert await _scalar(db_engine, "SELECT count(*) FROM port_pass_through_member") == 2


async def test_deleting_the_pass_through_or_its_equipment_cascades_cleanly(db_engine, panels, db_session):
    await db_session.rollback()
    e = uuid.UUID(panels["one_id"])
    async with db_engine.begin() as conn:
        pt = await _insert_pair(conn, e, panels["one"]["Front01"], panels["one"]["Rear01"])
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM port_pass_through WHERE id = :id"), {"id": pt})
    assert await _scalar(db_engine, "SELECT count(*) FROM port_pass_through_member") == 0
    async with db_engine.begin() as conn:
        await _insert_pair(conn, e, panels["one"]["Front02"], panels["one"]["Rear02"])
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM equipment WHERE id = :e"), {"e": e})
    assert await _scalar(db_engine, "SELECT count(*) FROM port_pass_through") == 0


async def test_two_writers_claiming_one_port_cannot_both_win(client, db_engine, race, panels):  # noqa: F811
    """The topology lock serialises them, and the unique member constraint is the backstop: exactly one commits."""
    ports = panels["one"]
    calls = [
        ("POST", h.PASS_THROUGHS, {"json": {"port_a_id": ports["Front01"], "port_b_id": ports["Rear01"]}, "headers": panels["headers"]}),
        ("POST", h.PASS_THROUGHS, {"json": {"port_a_id": ports["Rear01"], "port_b_id": ports["Front02"]}, "headers": panels["headers"]}),
    ]
    responses = await race("SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls)
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    assert await _scalar(db_engine, "SELECT count(*) FROM port_pass_through") == 1
    assert await _scalar(db_engine, "SELECT count(*) FROM port_pass_through_member") == 2  # never a half-committed pair


async def test_deleting_a_pass_through_while_a_cable_is_recorded_leaves_one_consistent_path(client, db_engine, race, panels):  # noqa: F811
    """A delete and a cable create race for the same panel port: whichever order wins, the stored topology is consistent."""
    headers = panels["headers"]
    sw = await make_device(client, headers, ["Eth1/1"], hostname="race-sw")
    created = await client.post(h.PASS_THROUGHS, json={"port_a_id": panels["one"]["Front01"], "port_b_id": panels["one"]["Rear01"]}, headers=headers)
    assert created.status_code == 201
    calls = [
        ("DELETE", f"{h.PASS_THROUGHS}/{created.json()['id']}", {"headers": headers | {"If-Match": "1"}}),
        ("POST", CABLES, {"json": {"label": "RACE-PT", "cable_type": "copper_utp", "endpoint_a_port_id": sw["port_by_name"]["Eth1/1"],
                                   "endpoint_b_port_id": panels["one"]["Front01"], "status": "installed"}, "headers": headers}),
    ]
    responses = await race("SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls)
    assert sorted(r.status_code for r in responses) == [201, 204], [r.text for r in responses]
    trace = (await client.get(h.trace_url(sw["port_by_name"]["Eth1/1"]), headers=headers)).json()
    members = await _scalar(db_engine, "SELECT count(*) FROM port_pass_through_member")
    assert members == 0 and trace["terminated"] == "end_of_path" and trace["hop_count"] == 1


async def test_migration_0040_round_trip_preserves_cables_and_their_traces(client, auth_headers, db_engine, db_session):
    """Existing one-hop cables stay valid across 0040 -> 0039 -> 0040, with their ports and connections untouched."""
    headers = await auth_headers("Administrator")
    left = await make_device(client, headers, ["p1"], hostname="mig-l")
    right = await make_device(client, headers, ["p1"], hostname="mig-r")
    cable = await create_cable(client, headers, left["port_by_name"]["p1"], right["port_by_name"]["p1"], label="MIG-1", status="installed")
    await db_session.rollback()
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            before = (await conn.execute(text("SELECT c.id, c.label, c.status, c.port_connection_id FROM cable c WHERE c.id = :c"), {"c": cable["id"]})).one()
            ports_before = (await conn.execute(text("SELECT count(*) FROM equipment_port"))).scalar_one()
            await conn.run_sync(_run_file, MIGRATIONS / "0040_port_pass_through.py", "downgrade")
            assert await conn.scalar(text("SELECT to_regclass('port_pass_through')")) is None
            await conn.run_sync(_run_file, MIGRATIONS / "0040_port_pass_through.py", "upgrade")
            assert (await conn.execute(text("SELECT c.id, c.label, c.status, c.port_connection_id FROM cable c WHERE c.id = :c"), {"c": cable["id"]})).one() == before
            assert (await conn.execute(text("SELECT count(*) FROM equipment_port"))).scalar_one() == ports_before
            assert await conn.scalar(text("SELECT to_regclass('port_pass_through_member')")) is not None
        finally:
            await tx.rollback()
    trace = (await client.get(h.trace_url(left["port_by_name"]["p1"]), headers=headers)).json()
    assert trace["terminated"] == "end_of_path" and trace["hop_count"] == 1  # the one-hop cable still traces


async def test_downgrade_refuses_to_discard_recorded_pass_throughs(db_engine, panels, db_session):
    await db_session.rollback()
    e = uuid.UUID(panels["one_id"])
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await _insert_pair(conn, e, panels["one"]["Front01"], panels["one"]["Rear01"])
            with pytest.raises(Exception, match="Refusing to drop pass-through"):
                await conn.run_sync(_run_file, MIGRATIONS / "0040_port_pass_through.py", "downgrade")
        finally:
            await tx.rollback()


def _run_file(connection, path, direction):
    spec = importlib.util.spec_from_file_location(f"mig_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()
