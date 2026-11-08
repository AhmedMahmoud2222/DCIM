"""PostgreSQL proof for migration 0042 (Issue #102): a populated upgrade keeps existing power data, the new
constraints bite, downgrade refuses to discard recorded rows, and downgrade then re-upgrade works once the
new data is gone."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0042_power_protection_reports.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("power_protection_reports_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _count(conn, table):
    return await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed names in this test


async def _tables(conn):
    rows = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('protection_device','power_utilization_snapshot','power_report_job')"
        )
    )
    return {r[0] for r in rows}


async def _site(conn):
    ids = {k: uuid.uuid4() for k in ("org", "country", "city", "site")}
    await conn.execute(text("INSERT INTO organization (id, name) VALUES (:i, :n)"), {"i": ids["org"], "n": f"o-{ids['org']}"})
    await conn.execute(
        text("INSERT INTO country (id, organization_id, name, iso_code) VALUES (:i, :o, 'T', 'TL')"), {"i": ids["country"], "o": ids["org"]}
    )
    await conn.execute(text("INSERT INTO city (id, country_id, name) VALUES (:i, :c, 'C')"), {"i": ids["city"], "c": ids["country"]})
    await conn.execute(
        text("INSERT INTO site (id, city_id, code, name, timezone) VALUES (:i, :c, :code, 'S', 'UTC')"),
        {"i": ids["site"], "c": ids["city"], "code": f"S-{ids['site'].hex[:8]}"},
    )
    return ids["site"]


async def test_populated_upgrade_downgrade_and_reupgrade(db_engine):
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")
            assert await _tables(conn) == set()
            utility = uuid.uuid4()
            await conn.execute(
                text("INSERT INTO power_node (id, node_type, label) VALUES (:i, 'utility_intake', 'Utility')"), {"i": utility}
            )
            with pytest.raises(IntegrityError):  # the old constraint does not know the new type
                async with conn.begin_nested():
                    await conn.execute(
                        text("INSERT INTO power_node (id, node_type, label, owning_asset_id) VALUES (:i, 'protection_device', 'x', NULL)"),
                        {"i": uuid.uuid4()},
                    )

            await conn.run_sync(_run, "upgrade")
            assert await _tables(conn) == {"protection_device", "power_utilization_snapshot", "power_report_job"}
            assert await conn.scalar(text("SELECT label FROM power_node WHERE id = :i"), {"i": utility}) == "Utility"

            site = await _site(conn)
            asset = uuid.uuid4()
            await conn.execute(text("INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) VALUES (:i, 'power_panel', :t, 'planned', '{}'::jsonb)"), {"i": asset, "t": f"P-{asset.hex[:8]}"})
            node = uuid.uuid4()
            await conn.execute(
                text("INSERT INTO power_node (id, node_type, label, owning_asset_id) VALUES (:i, 'protection_device', 'BRK', :a)"),
                {"i": node, "a": asset},
            )
            insert_device = (
                "INSERT INTO protection_device (power_node_id, site_id, device_type, rating_a, voltage_v, poles, phase_config, "
                "state, status, version) VALUES (:n, :s, 'breaker', :r, :v, :p, :c, 'closed', 'in_service', 1)"
            )
            ok = {"n": node, "s": site, "r": 63, "v": 230, "p": 1, "c": "single"}
            await conn.execute(text(insert_device), ok)
            for bad in ({"r": 0}, {"r": 7000}, {"v": 5}, {"v": 5000}, {"p": 3}, {"c": "three"}):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(insert_device.replace("'closed'", "'closed'")), {**ok, "n": uuid.uuid4(), **bad})

            start = datetime(2026, 10, 8, 10, tzinfo=UTC)
            snap = (
                "INSERT INTO power_utilization_snapshot (id, granularity, bucket_start, bucket_end, scope_type, scope_id, site_id, "
                "metric, unit, load_kw, load_basis, sample_count, expected_samples, coverage_ratio, quality, window_start, "
                "window_end, method_version, computed_at) VALUES (:id, 'hour', :s, :e, 'site', :sid, :sid, :m, :u, :l, :lb, 3, 60, "
                "0.05, 'measured', :s, :e, '1', now())"
            )
            base = {"id": uuid.uuid4(), "s": start, "e": start + timedelta(hours=1), "sid": site, "m": "power_kw", "u": "kW", "l": 10, "lb": "measured"}
            await conn.execute(text(snap), base)
            for bad in ({"m": "temperature_c"}, {"u": "W"}, {"l": -1}, {"lb": "none"}, {"l": None}):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(snap), {**base, "id": uuid.uuid4(), "s": start + timedelta(hours=5), "e": start + timedelta(hours=6), **bad})
            with pytest.raises(IntegrityError):  # same bucket and scope twice
                async with conn.begin_nested():
                    await conn.execute(text(snap), {**base, "id": uuid.uuid4()})

            with pytest.raises(RuntimeError):
                async with conn.begin_nested():
                    await conn.run_sync(_run, "downgrade")
            assert await _count(conn, "protection_device") == 1

            await conn.execute(text("DELETE FROM power_utilization_snapshot"))
            await conn.execute(text("DELETE FROM power_node WHERE id = :i"), {"i": node})
            await conn.run_sync(_run, "downgrade")
            assert await _tables(conn) == set()
            assert await conn.scalar(text("SELECT label FROM power_node WHERE id = :i"), {"i": utility}) == "Utility"
            await conn.run_sync(_run, "upgrade")
            assert await _tables(conn) == {"protection_device", "power_utilization_snapshot", "power_report_job"}
            assert await conn.scalar(text("SELECT label FROM power_node WHERE id = :i"), {"i": utility}) == "Utility"
        finally:
            await tx.rollback()
