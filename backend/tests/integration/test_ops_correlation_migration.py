"""PostgreSQL proof for migration 0043 (Issue #103): populated upgrade keeps existing alarm, collector and
heartbeat data untouched, the new constraints bite, downgrade refuses to discard recorded rows, and a clean
downgrade then re-upgrade works."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0043_ops_correlation_itsm.py"
NEW_TABLES = {
    "collector_state", "collector_transition", "correlation_incident", "correlation_member", "notification_channel",
    "notification_policy", "notification_delivery", "itsm_connection", "itsm_ticket",
}
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("ops_correlation_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _tables(conn):
    rows = await conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:t)"), {"t": list(NEW_TABLES)})
    return {r[0] for r in rows}


async def _seed_existing(conn):
    ids = {k: uuid.uuid4() for k in ("collector", "integration", "rule", "alarm", "hb")}
    await conn.execute(
        text("INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at) VALUES (:i, 'c-mig', 'central', 'active', 'x', :t)"),
        {"i": ids["collector"], "t": NOW},
    )
    await conn.execute(
        text("INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, consecutive_failures, version) VALUES (:i, 'i-mig', 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"),
        {"i": ids["integration"]},
    )
    await conn.execute(
        text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, enabled, name) VALUES (:r, :i, 'availability', 'availability_unavailable', true, 'r')"),
        {"r": ids["rule"], "i": ids["integration"]},
    )
    await conn.execute(
        text("INSERT INTO alarm (id, rule_id, integration_id, subject_key, status, opened_at, last_value, details) VALUES (:a, :r, :i, 's', 'ACTIVE', :t, 0, '{}'::jsonb)"),
        {"a": ids["alarm"], "r": ids["rule"], "i": ids["integration"], "t": NOW},
    )
    await conn.execute(
        text("INSERT INTO collector_heartbeat (id, collector_id, ts, status) VALUES (:h, :c, :t, 'ok')"),
        {"h": ids["hb"], "c": ids["collector"], "t": NOW},
    )
    return ids


async def _fingerprint(conn):
    return (
        await conn.execute(
            text(
                "SELECT md5(coalesce((SELECT string_agg(a::text, '|' ORDER BY a.id::text) FROM alarm a), '') || "
                "coalesce((SELECT string_agg(h::text, '|' ORDER BY h.id::text) FROM collector_heartbeat h), '') || "
                "coalesce((SELECT string_agg(c::text, '|' ORDER BY c.id::text) FROM collector c), ''))"
            )
        )
    ).scalar_one()


async def test_populated_upgrade_downgrade_and_reupgrade(db_engine):
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")
            assert await _tables(conn) == set()
            ids = await _seed_existing(conn)
            before = await _fingerprint(conn)

            await conn.run_sync(_run, "upgrade")
            assert await _tables(conn) == NEW_TABLES
            assert await _fingerprint(conn) == before  # existing source facts untouched

            inc = uuid.uuid4()
            incident = (
                "INSERT INTO correlation_incident (id, dedup_key, rule, cause_type, cause_ref, cause_label, confidence, rationale, evidence, status, "
                "opened_at, last_member_at, correlation_id, method_version, version) VALUES (:i, :k, :rule, 'alarm', 'x', 'x', :conf, 'r', '[]'::jsonb, "
                ":st, :t, :t, 'c', '1', 1)"
            )
            ok = {"i": inc, "k": "k1", "rule": "same_device", "conf": "low", "st": "open", "t": NOW}
            await conn.execute(text(incident), ok)
            for bad in ({"rule": "magic"}, {"conf": "certain"}, {"st": "resolved"}):  # resolved needs resolved_at
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(incident), {**ok, "i": uuid.uuid4(), "k": f"k-{uuid.uuid4().hex}", **bad})
            with pytest.raises(IntegrityError):  # dedup key is unique
                async with conn.begin_nested():
                    await conn.execute(text(incident), {**ok, "i": uuid.uuid4()})

            member = "INSERT INTO correlation_member (id, incident_id, member_type, alarm_id, role, source_time, added_at) VALUES (:i, :inc, 'alarm', :a, 'cause', :t, :t)"
            await conn.execute(text(member), {"i": uuid.uuid4(), "inc": inc, "a": ids["alarm"], "t": NOW})
            other = uuid.uuid4()
            await conn.execute(text(incident), {**ok, "i": other, "k": "k2"})
            with pytest.raises(IntegrityError):  # an alarm belongs to one incident only
                async with conn.begin_nested():
                    await conn.execute(text(member), {"i": uuid.uuid4(), "inc": other, "a": ids["alarm"], "t": NOW})
            with pytest.raises(IntegrityError):  # exactly one source
                async with conn.begin_nested():
                    await conn.execute(
                        text("INSERT INTO correlation_member (id, incident_id, member_type, role, source_time, added_at) VALUES (:i, :inc, 'alarm', 'cause', :t, :t)"),
                        {"i": uuid.uuid4(), "inc": other, "t": NOW},
                    )

            await conn.execute(
                text("INSERT INTO collector_state (collector_id, state, generation, since, updated_at) VALUES (:c, 'online', 1, :t, :t)"),
                {"c": ids["collector"], "t": NOW},
            )
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE collector_state SET generation = 0 WHERE collector_id = :c"), {"c": ids["collector"]})
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE collector_state SET state = 'degraded' WHERE collector_id = :c"), {"c": ids["collector"]})

            with pytest.raises(RuntimeError):
                async with conn.begin_nested():
                    await conn.run_sync(_run, "downgrade")
            assert await _tables(conn) == NEW_TABLES

            await conn.execute(text("DELETE FROM correlation_incident"))
            await conn.execute(text("DELETE FROM collector_state"))
            await conn.run_sync(_run, "downgrade")
            assert await _tables(conn) == set()
            assert await _fingerprint(conn) == before
            await conn.run_sync(_run, "upgrade")
            assert await _tables(conn) == NEW_TABLES
            assert await _fingerprint(conn) == before
            assert timedelta(0) < timedelta(seconds=1)
        finally:
            await tx.rollback()
