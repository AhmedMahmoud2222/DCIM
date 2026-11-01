"""Issue #150 acceptance-evidence: migrations 0055_pdu_catalog_outlet_template and 0056_pdu_vertical_placement against a
database already holding legacy PDU inventory (ManagedAsset + PDU + PowerNode + PDUOutlet + PowerConnection + telemetry).

Mirrors the other populated-migration tests in this directory: current head is downgraded inside one transaction to the
pre-0055 shape, legacy rows are written, the two migrations are re-applied, and every legacy row is shown byte-for-byte
unchanged. The transaction is always rolled back, so nothing leaks into other tests. Tests only - no runtime change."""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

VERSIONS = Path(__file__).resolve().parents[2] / "migrations/versions"
M0055 = VERSIONS / "0055_pdu_catalog_outlet_template.py"
M0056 = VERSIONS / "0056_pdu_vertical_placement.py"


def _run(connection, path: Path, direction: str):
    spec = importlib.util.spec_from_file_location(f"mig_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _downgrade_to_pre_0055(conn):
    await conn.run_sync(_run, M0056, "downgrade")
    await conn.run_sync(_run, M0055, "downgrade")


async def _upgrade_through_0056(conn):
    await conn.run_sync(_run, M0055, "upgrade")
    await conn.run_sync(_run, M0056, "upgrade")


async def _seed_legacy_pdu(conn, tag: str) -> dict:
    """One legacy PDU with two outlets, one outlet-to-outlet power link and a telemetry reading tied to the PDU asset."""
    asset_id, node_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) "
             "VALUES (:i, 'pdu', :tag, 'active', '{}'::jsonb)"),
        {"i": asset_id, "tag": f"LEGACY-PDU-{tag}"},
    )
    await conn.execute(
        text("INSERT INTO pdu (id, name, ip_address, protocol, input_voltage, rated_current_a, outlet_count, version) "
             "VALUES (:i, 'legacy-pdu', '10.9.8.7', 'snmp', 230.00, 16.00, 2, 3)"),
        {"i": asset_id},
    )
    await conn.execute(
        text("INSERT INTO power_node (id, node_type, managed_asset_id, label) VALUES (:n, 'pdu', :a, 'legacy-pdu')"),
        {"n": node_id, "a": asset_id},
    )
    outlet_nodes = []
    for number in (1, 2):
        outlet_node = uuid.uuid4()
        outlet_nodes.append(outlet_node)
        await conn.execute(
            text("INSERT INTO power_node (id, node_type, owning_asset_id, label) VALUES (:n, 'pdu_outlet', :a, :l)"),
            {"n": outlet_node, "a": asset_id, "l": f"legacy-outlet-{number}"},
        )
        await conn.execute(
            text("INSERT INTO pdu_outlet (power_node_id, pdu_asset_id, outlet_number, label, state) "
                 "VALUES (:n, :a, :num, :l, 'on')"),
            {"n": outlet_node, "a": asset_id, "num": number, "l": f"legacy-outlet-{number}"},
        )
    connection_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO power_connection (id, source_node_id, target_node_id, connection_type, feed_label, status, "
             "version, effective_from) VALUES (:c, :s, :t, 'distribution', 'A', 'active', 2, '2026-01-01T00:00:00+00')"),
        {"c": connection_id, "s": outlet_nodes[0], "t": outlet_nodes[1]},
    )

    collector, integration, mapping = (uuid.uuid4() for _ in range(3))
    await conn.execute(
        text("INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at) "
             "VALUES (:id, :name, 'central', 'active', 'test', now())"),
        {"id": collector, "name": f"pdu-mig-{collector}"},
    )
    await conn.execute(
        text("INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, "
             "consecutive_failures, version) VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"),
        {"id": integration, "name": f"pdu-mig-{integration}"},
    )
    await conn.execute(
        text("INSERT INTO integration_metric_mapping (id, integration_id, source_identifier, canonical_metric, unit, "
             "scale, registry_version) VALUES (:id, :i, 's', 'power_kw', 'kW', 1, '1')"),
        {"id": mapping, "i": integration},
    )
    reading_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO telemetry_reading (id, collector_id, integration_id, mapping_id, managed_asset_id, "
             "external_identifier, series_key, dedup_key, metric, unit, value, raw_value, raw_unit, source_scale, "
             "registry_version, occurred_at, received_at, attributes) "
             "VALUES (:id, :c, :i, :m, :a, 'e', 'k', 'pdu-mig', 'power_kw', 'kW', 1.5, 1.5, 'kW', 1, '1', now(), now(), "
             "'{}'::jsonb)"),
        {"id": reading_id, "c": collector, "i": integration, "m": mapping, "a": asset_id},
    )
    return {"asset_id": asset_id, "node_id": node_id, "connection_id": connection_id, "reading_id": reading_id}


async def _snapshot(conn, ids: dict) -> dict:
    """Every legacy row, as full tuples, so any drift in any column is detected."""
    queries = {
        "asset": ("SELECT * FROM managed_asset WHERE id = :a", {"a": ids["asset_id"]}),
        "pdu": ("SELECT * FROM pdu WHERE id = :a", {"a": ids["asset_id"]}),
        "nodes": ("SELECT * FROM power_node WHERE managed_asset_id = :a OR owning_asset_id = :a ORDER BY label", {"a": ids["asset_id"]}),
        "outlets": ("SELECT * FROM pdu_outlet WHERE pdu_asset_id = :a ORDER BY outlet_number", {"a": ids["asset_id"]}),
        "connection": ("SELECT * FROM power_connection WHERE id = :c", {"c": ids["connection_id"]}),
        "reading": ("SELECT * FROM telemetry_reading WHERE id = :r", {"r": ids["reading_id"]}),
    }
    return {
        name: [tuple(row) for row in (await conn.execute(text(sql), params)).all()]
        for name, (sql, params) in queries.items()
    }


async def test_populated_legacy_pdu_inventory_survives_0055_and_0056_unchanged(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await _downgrade_to_pre_0055(conn)
            assert await conn.scalar(text("SELECT to_regclass('pdu_outlet_template')")) is None
            legacy = await _seed_legacy_pdu(conn, uuid.uuid4().hex[:8])
            before = await _snapshot(conn, legacy)
            assert len(before["outlets"]) == 2 and len(before["nodes"]) == 3
            assert len(before["connection"]) == 1 and len(before["reading"]) == 1

            await _upgrade_through_0056(conn)

            assert await _snapshot(conn, legacy) == before, "legacy PDU inventory drifted across 0055/0056"
            assert await conn.scalar(text("SELECT to_regclass('pdu_outlet_template')")) is not None
            assert await conn.scalar(text("SELECT count(*) FROM pdu_outlet_template")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_equipment_placement_pdu_vertical_0u'")) == 1
            # No catalog back-reference was added to the runtime power domain (snapshot semantics, spec §4.1).
            for table in ("pdu", "pdu_outlet", "power_node", "managed_asset"):
                cols = {r[0] for r in (await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})).all()}
                assert not any("catalog" in c for c in cols), f"{table} unexpectedly gained a catalog column: {cols}"
        finally:
            await transaction.rollback()


async def test_pre_existing_rows_remain_writable_and_ordinary_pdu_assets_need_no_catalog_revision(db_engine):
    """The new lifecycle trigger fires only on catalog_model_revision; a legacy-style PDU can still be created and its
    outlets edited after the upgrade, with no catalog revision involved."""
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await _downgrade_to_pre_0055(conn)
            legacy = await _seed_legacy_pdu(conn, uuid.uuid4().hex[:8])
            await _upgrade_through_0056(conn)

            await conn.execute(
                text("UPDATE pdu_outlet SET state = 'off' WHERE pdu_asset_id = :a AND outlet_number = 1"),
                {"a": legacy["asset_id"]},
            )
            assert await conn.scalar(
                text("SELECT state FROM pdu_outlet WHERE pdu_asset_id = :a AND outlet_number = 1"), {"a": legacy["asset_id"]}
            ) == "off"
            fresh = await _seed_legacy_pdu(conn, uuid.uuid4().hex[:8])
            assert await conn.scalar(text("SELECT count(*) FROM pdu_outlet WHERE pdu_asset_id = :a"), {"a": fresh["asset_id"]}) == 2
        finally:
            await transaction.rollback()


async def test_downgrade_then_reupgrade_of_populated_database_is_clean(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await _downgrade_to_pre_0055(conn)
            legacy = await _seed_legacy_pdu(conn, uuid.uuid4().hex[:8])
            before = await _snapshot(conn, legacy)
            await _upgrade_through_0056(conn)
            await conn.run_sync(_run, M0056, "downgrade")
            await conn.run_sync(_run, M0055, "downgrade")
            assert await _snapshot(conn, legacy) == before
            await _upgrade_through_0056(conn)
            assert await _snapshot(conn, legacy) == before
        finally:
            await transaction.rollback()
