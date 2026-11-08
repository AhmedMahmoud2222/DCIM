"""0052 upgrade/downgrade preserves populated unbound rows and binding evidence."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.db.base import Base

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0052_monitoring_template_binding.py"


def run(connection, direction):
    spec = importlib.util.spec_from_file_location("monitoring_template_binding", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
    with Operations.context(context):
        getattr(module, direction)()


async def test_empty_migration_roundtrip(db_engine):
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(run, "downgrade")
            await connection.run_sync(run, "upgrade")
            columns = set((await connection.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='monitoring_metric_template'"
            ))).scalars())
            assert {"registry_metric", "registry_version", "threshold_unit"} <= columns
        finally:
            await transaction.rollback()


async def test_populated_binding_refuses_downgrade(client, auth_headers, db_engine):
    from tests.api.test_monitoring_template_binding import body, make_revision

    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics", json=body(),
                                 headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(RuntimeError, match="Cannot downgrade 0052"):
                await connection.run_sync(run, "downgrade")
            assert await connection.scalar(text("SELECT registry_version FROM monitoring_metric_template")) == "2"
        finally:
            await transaction.rollback()


async def test_populated_unbound_roundtrip_preserves_authored_template(client, auth_headers, db_engine):
    from tests.api.test_monitoring_template_binding import body, make_revision

    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics", json=body(
        registry_metric=None, registry_version=None, threshold_unit=None, unit="vendor-unit"),
        headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(run, "downgrade")
            await connection.run_sync(run, "upgrade")
            row = (await connection.execute(text(
                "SELECT unit, registry_metric, registry_version, threshold_unit FROM monitoring_metric_template"
            ))).one()
            assert row == ("vendor-unit", None, None, None)
        finally:
            await transaction.rollback()


async def test_downgrade_waits_for_binding_writer_then_refuses_evidence_loss(client, auth_headers, db_engine):
    from tests.api.test_monitoring_template_binding import body, make_revision

    headers = await auth_headers("Administrator")
    revision = await make_revision(client, headers)
    response = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/monitoring-metrics", json=body(
        registry_metric=None, registry_version=None, threshold_unit=None),
        headers={**headers, "If-Match": str(revision["version"])})
    assert response.status_code == 201, response.text
    metric_id = response.json()["id"]
    async with db_engine.connect() as writer, db_engine.connect() as downgrader:
        writer_transaction = await writer.begin()
        downgrade_transaction = await downgrader.begin()
        pending = None
        try:
            await writer.execute(text(
                "UPDATE monitoring_metric_template SET registry_metric='temperature_c', "
                "registry_version='2', threshold_unit='degF' WHERE id=:id"
            ), {"id": metric_id})
            downgrade_pid = await downgrader.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(downgrader.run_sync(run, "downgrade"))
            # The writer owns RowExclusiveLock. Wait for the competing DDL
            # request rather than inferring a race from a fixed sleep.
            for _ in range(100):
                waiting = await writer.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid "
                    "AND relation='monitoring_metric_template'::regclass "
                    "AND mode='AccessExclusiveLock' AND NOT granted)"
                ), {"pid": downgrade_pid})
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting, "Downgrade did not request the evidence-protecting table lock"
            assert not pending.done()
            await writer_transaction.commit()
            with pytest.raises(RuntimeError, match="Cannot downgrade 0052"):
                await asyncio.wait_for(pending, timeout=5)
            await downgrade_transaction.rollback()
            assert await writer.scalar(text(
                "SELECT registry_version FROM monitoring_metric_template WHERE id=:id"
            ), {"id": metric_id}) == "2"
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if writer_transaction.is_active:
                await writer_transaction.rollback()
            if downgrade_transaction.is_active:
                await downgrade_transaction.rollback()
