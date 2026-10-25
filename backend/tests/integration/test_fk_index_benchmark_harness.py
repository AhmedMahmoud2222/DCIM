"""CI-sized checks of the migration-0057 benchmark harness (scripts/fk_index_benchmark).

No timing is asserted anywhere: the harness's measurements are machine-specific and belong in a manual run. These tests prove
the harness itself is sound at a few thousand rows: the data generator is deterministic and constraint-clean, the instrumented
upgrade reports what happened, and a build interrupted at a deterministic barrier (an open writer transaction, not a sleep) is
left INVALID, leaves the database at 0056, and is recovered by a plain re-run.
"""

from __future__ import annotations

import time
import uuid

import pytest

from scripts.fk_index_benchmark import data, report, scenarios
from scripts.fk_index_benchmark.harness import (
    HEAD,
    PREVIOUS,
    AlembicRun,
    Urls,
    alembic_ok,
    connect,
    create_db,
    drop_db,
    integrity,
)
from scripts.fk_index_benchmark.scenarios import FIRST_INDEX, Config
from scripts.fk_index_benchmark.workload import Workload, percentile, summarize

SPEC = data.DatasetSpec(rows=3000, lineage_rows=300, mappings=50)
STEP_TIMEOUT = 120


@pytest.fixture(scope="module")
def urls() -> Urls:
    return Urls.from_env()


@pytest.fixture(scope="module")
def template(urls):
    """A seeded database at 0056, cloned for every test."""
    name = f"fkbench_ci_tpl_{uuid.uuid4().hex[:8]}"
    create_db(urls, name)
    try:
        alembic_ok(urls, name, "upgrade", PREVIOUS).cleanup()
        with connect(urls, name) as conn:
            data.seed(conn, SPEC)
        yield name
    finally:
        drop_db(urls, name)


@pytest.fixture
def clone(urls, template):
    name = f"fkbench_ci_{uuid.uuid4().hex[:8]}"
    create_db(urls, name, template=template)
    try:
        yield name
    finally:
        drop_db(urls, name)


def test_percentile_is_nearest_rank_and_never_interpolates():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 50) == 30.0
    assert percentile(values, 95) == 50.0
    assert percentile(values, 99) == 50.0
    assert percentile([], 50) is None
    assert summarize([])["p99_ms"] is None and summarize([7.0])["max_ms"] == 7.0


def test_seed_is_deterministic_and_matches_the_requested_shape(urls, template, clone):
    def fingerprint(db: str):
        with connect(urls, db) as conn:
            return conn.execute(
                "SELECT count(*), md5(string_agg(id::text || dedup_key || coalesce(mapping_revision_id::text, ''), ',' ORDER BY dedup_key)), "
                "count(*) FILTER (WHERE mapping_revision_id IS NOT NULL) FROM telemetry_reading"
            ).fetchone()

    rows, digest, pinned = fingerprint(clone)
    assert rows == SPEC.rows
    assert pinned == SPEC.rows - SPEC.rows // data.PINNED_SHARE  # 4 of every 5 rows carry a revision
    other = f"fkbench_ci_{uuid.uuid4().hex[:8]}"
    create_db(urls, other)
    try:
        alembic_ok(urls, other, "upgrade", PREVIOUS).cleanup()
        with connect(urls, other) as conn:
            data.seed(conn, SPEC)
        assert fingerprint(other) == (rows, digest, pinned), "the same spec must produce the same table"
    finally:
        drop_db(urls, other)
    with connect(urls, clone) as conn:
        assert data.count(conn, "daily_telemetry_aggregate_lineage") == SPEC.lineage_rows
        assert data.orphan_mapping_id(conn, SPEC) is not None


def test_paced_workload_inserts_rows_and_reports_latency_windows(urls, clone):
    work = Workload(dsn=urls.conninfo(clone), mappings=SPEC.mappings, writers=1, batch_rows=5, writes_per_second=20, readers=1)
    start = time.time()
    work.start()
    time.sleep(1.5)
    work.stop()
    end = time.time() + 1
    window = work.window(start, end)
    assert window["write"]["count"] > 0 and window["write"]["failed"] == 0
    assert window["read"]["count"] > 0 and window["read"]["failed"] == 0
    with connect(urls, clone) as conn:
        assert data.count(conn, "telemetry_reading") == SPEC.rows + window["write"]["count"] * 5


def test_instrumented_upgrade_reports_invariants_and_renders(urls, template):
    cfg = Config(urls=urls, spec=SPEC, template=template, step_timeout=STEP_TIMEOUT)
    result = scenarios.s2_idle_upgrade(cfg)
    assert all(result["invariants"].values()), result["invariants"]
    assert result["upgrade_idle"]["exit_code"] == 0
    assert result["upgrade_idle"]["resources_delta"]["wal_bytes"] > 0
    full = {
        "dataset": {"rows": SPEC.rows, "lineage_rows": SPEC.lineage_rows, "mappings": SPEC.mappings},
        "environment": {
            "postgres_version": "test", "runner_platform": "test", "runner_cpu_count": 1, "runner_mem_total_bytes": None,
            "postgres_settings": {},
        },
        "workload": "none",
        "scenarios": {"s2": result},
        "limitations": list(report.LIMITATIONS),
    }
    markdown = report.render_markdown(full)
    assert "NOT YET VALIDATED" in markdown and "S2/S6" in markdown


def _wait_for_build_phase(urls: Urls, db: str, run: AlembicRun, prefix: str) -> int:
    """Block (bounded) until the first large build is observably in a phase starting with `prefix`; return its backend pid."""
    deadline = time.time() + STEP_TIMEOUT
    with connect(urls, db) as conn:
        while time.time() < deadline:
            row = conn.execute(
                "SELECT p.pid FROM pg_stat_progress_create_index p JOIN pg_class c ON c.oid = p.index_relid "
                "WHERE c.relname = %s AND p.phase LIKE %s",
                (FIRST_INDEX, prefix + "%"),
            ).fetchone()
            if row:
                return row[0]
            assert run.poll() is None, f"alembic exited early:\n{run.output()}"
            time.sleep(0.1)
    raise AssertionError(f"build never reached phase {prefix!r}\n{run.output()}")


@pytest.mark.parametrize("interrupt", ["cancel", "terminate"])
def test_interrupted_build_leaves_invalid_artifact_and_a_plain_rerun_recovers(urls, clone, interrupt):
    """Barrier: an open transaction that wrote to telemetry_reading, so CREATE INDEX CONCURRENTLY waits for writers."""
    blocker = connect(urls, clone, autocommit=False)
    blocker.execute("SET session_replication_role = replica")
    blocker.execute(
        "INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, "
        "metric, unit, value, occurred_at, received_at) SELECT md5('c')::uuid, md5('i')::uuid, md5('m1')::uuid, 'x', 's', 'blocker', 'm', 'u', 1, now(), now()"
    )
    run = AlembicRun(urls, clone, "upgrade", "head")
    try:
        pid = _wait_for_build_phase(urls, clone, run, "waiting for writers")
        with connect(urls, clone) as conn:
            conn.execute("SELECT pg_cancel_backend(%s)" if interrupt == "cancel" else "SELECT pg_terminate_backend(%s)", (pid,))
        rc = run.wait(STEP_TIMEOUT)
    finally:
        if run.poll() is None:
            run.proc.kill()
        run.cleanup()
    assert rc != 0, "an interrupted build must fail the migration"
    state = integrity(urls, clone)
    assert state["version"] == PREVIOUS, "alembic_version must not advance when the run was interrupted"
    assert FIRST_INDEX in state["invalid_named"], "an interrupted concurrent build leaves an INVALID index"
    assert state["advisory_locks_held"] == 0, "the migration advisory lock must be released with the connection"

    blocker.rollback()
    blocker.close()
    alembic_ok(urls, clone, "upgrade", "head", timeout=STEP_TIMEOUT).cleanup()
    final = integrity(urls, clone)
    assert final["version"] == HEAD and final["valid_and_ready"] == 22
    assert final["invalid_anywhere"] == 0
