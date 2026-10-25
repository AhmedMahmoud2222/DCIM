"""CI-sized checks of the migration-0057 benchmark harness (scripts/fk_index_benchmark).

No timing is asserted anywhere: the harness's measurements are machine-specific and belong in a manual run. These tests prove
the harness itself is sound at a few thousand rows:

* the safety controls REFUSE prohibited targets and never touch a database the run does not own (negative tests),
* the data generator is deterministic, honours its profile, and in `fk` mode loads real parents with FKs enforced,
* ingest works in raw, fk and application-service modes,
* the 22-index acceptance check catches missing, wrong, substituted, invalid and not-ready indexes,
* a build interrupted at a deterministic barrier (an open writer transaction, not a sleep) is left INVALID, leaves the database
  at 0056, and is recovered by a plain re-run.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy.engine.url import make_url

from scripts.fk_index_benchmark import __main__ as bench_main
from scripts.fk_index_benchmark import data, report, scenarios
from scripts.fk_index_benchmark.harness import (
    FK_INDEXES,
    HEAD,
    PREVIOUS,
    AlembicRun,
    Urls,
    alembic_ok,
    connect,
    create_db,
    drop_db,
    integrity,
    signal_backend,
    verify_indexes,
)
from scripts.fk_index_benchmark.safety import MARKER_PREFIX, OWNER_TAG, Authorization, UnsafeTarget, verify_target
from scripts.fk_index_benchmark.scenarios import FIRST_INDEX, Config
from scripts.fk_index_benchmark.workload import Workload, percentile, summarize

SPEC = data.DatasetSpec(rows=3000, lineage_rows=300, mappings=50)
FK_SPEC = data.DatasetSpec(rows=3000, lineage_rows=300, mappings=50, mode="fk")
STEP_TIMEOUT = 120
RUN = f"citest-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def urls() -> Urls:
    return Urls.from_env()


def _template(urls: Urls, spec: data.DatasetSpec):
    name = f"fkbench_ci_tpl_{uuid.uuid4().hex[:8]}"
    create_db(urls, name, owner=RUN)
    try:
        alembic_ok(urls, name, "upgrade", PREVIOUS).cleanup()
        with connect(urls, name, app_role=spec.mode == "fk") as conn:
            data.seed(conn, spec)
        yield name
    finally:
        drop_db(urls, name, owner=RUN)


@pytest.fixture(scope="module")
def template(urls):
    yield from _template(urls, SPEC)


@pytest.fixture(scope="module")
def template_fk(urls):
    yield from _template(urls, FK_SPEC)


def _clone(urls: Urls, template_name: str):
    name = f"fkbench_ci_{uuid.uuid4().hex[:8]}"
    create_db(urls, name, owner=RUN, template=template_name)
    try:
        yield name
    finally:
        drop_db(urls, name, owner=RUN)


@pytest.fixture
def clone(urls, template):
    yield from _clone(urls, template)


@pytest.fixture
def clone_fk(urls, template_fk):
    yield from _clone(urls, template_fk)


def _databases(urls: Urls) -> set[str]:
    with connect(urls, "postgres") as conn:
        return {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}


def _cluster_facts(urls: Urls) -> tuple[str, str]:
    with connect(urls, "postgres") as conn:
        return (
            conn.execute("SHOW cluster_name").fetchone()[0],
            str(conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]),
        )


# -- safety: the benchmark must refuse prohibited targets ---------------------------------------------------------------------


def test_verify_target_requires_authorization_and_the_disposable_marker(urls):
    cluster_name, system_id = _cluster_facts(urls)
    base = (urls.conninfo("postgres"), urls.admin, urls.app)
    with pytest.raises(UnsafeTarget, match="not authorized"):
        verify_target(*base, Authorization(system_id, allow_create_databases=False, allow_superuser=True))
    if not cluster_name.startswith(MARKER_PREFIX):
        # The CI cluster was never started with the operator's cluster_name marker, so it is NOT accepted as a scratch cluster.
        with pytest.raises(UnsafeTarget, match="cluster_name"):
            verify_target(*base, Authorization(system_id, allow_create_databases=True, allow_superuser=True))
    # With the marker requirement relaxed for this test only, the remaining controls still bite:
    relaxed = {"marker_prefix": cluster_name}
    with pytest.raises(UnsafeTarget, match="system identifier"):
        verify_target(*base, Authorization("0000000000000000000", allow_create_databases=True, allow_superuser=True), **relaxed)
    with pytest.raises(UnsafeTarget, match="system identifier"):
        verify_target(*base, Authorization(None, allow_create_databases=True, allow_superuser=True), **relaxed)


def test_verify_target_refuses_superuser_and_unrecognised_databases(urls):
    cluster_name, system_id = _cluster_facts(urls)
    base = (urls.conninfo("postgres"), urls.admin, urls.app)
    relaxed = {"marker_prefix": cluster_name}
    with connect(urls, "postgres") as conn:
        is_super = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if is_super:
        with pytest.raises(UnsafeTarget, match="superuser"):
            verify_target(*base, Authorization(system_id, allow_create_databases=True, allow_superuser=False), **relaxed)
    allow = Authorization(system_id, allow_create_databases=True, allow_superuser=True)
    unknown = f"prod_like_{uuid.uuid4().hex[:6]}"
    with connect(urls, "postgres") as conn:
        conn.execute(f'CREATE DATABASE "{unknown}"')
    try:
        with pytest.raises(UnsafeTarget, match="does not recognise"):
            verify_target(*base, allow, **relaxed)
    finally:
        with connect(urls, "postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{unknown}"')
    other = _databases(urls) - {"postgres", "template0", "template1"}
    if all(d.startswith("fkbench_") or d in (urls.admin.rsplit("/", 1)[-1], urls.app.rsplit("/", 1)[-1]) for d in other):
        facts = verify_target(*base, allow, **relaxed)  # positive control: a recognised cluster passes every other check
        assert facts["system_identifier"] == system_id


def test_cli_refuses_without_authorization_and_creates_nothing(urls, capsys):
    before = _databases(urls)
    assert bench_main.main(["--rows", "1000"]) == 2
    assert bench_main.main(["--rows", "1000", "--allow-create-databases", "--authorize-cluster", "123"]) == 2
    assert _databases(urls) == before, "a refused run must not create (or drop) any database"
    assert "refusing to run" in capsys.readouterr().err


def test_drop_db_only_drops_databases_this_run_created(urls):
    mine = f"fkbench_ci_own_{uuid.uuid4().hex[:6]}"
    create_db(urls, mine, owner=RUN)
    untagged = f"fkbench_ci_untagged_{uuid.uuid4().hex[:6]}"
    foreign_name = f"unrelated_{uuid.uuid4().hex[:6]}"
    with connect(urls, "postgres") as conn:
        conn.execute(f'CREATE DATABASE "{untagged}"')
        conn.execute(f'CREATE DATABASE "{foreign_name}"')
        conn.execute(f"COMMENT ON DATABASE \"{foreign_name}\" IS '{OWNER_TAG}{RUN}'")  # right tag, wrong name prefix
    try:
        with pytest.raises(UnsafeTarget, match="owner tag"):
            drop_db(urls, mine, owner="some-other-run")
        with pytest.raises(UnsafeTarget, match="owner tag"):
            drop_db(urls, untagged, owner=RUN)
        with pytest.raises(UnsafeTarget, match="not a fkbench_"):
            drop_db(urls, foreign_name, owner=RUN)
        with pytest.raises(UnsafeTarget, match="not a fkbench_"):
            drop_db(urls, "postgres", owner=RUN)
        assert {mine, untagged, foreign_name, "postgres"} <= _databases(urls), "refused drops must leave every database in place"
        drop_db(urls, mine, owner=RUN)
        assert mine not in _databases(urls)
    finally:
        with connect(urls, "postgres") as conn:
            for name in (mine, untagged, foreign_name):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_create_db_refuses_names_outside_the_fkbench_namespace(urls):
    with pytest.raises(UnsafeTarget, match="not a fkbench_"):
        create_db(urls, f"production_{uuid.uuid4().hex[:6]}", owner=RUN)


@contextlib.contextmanager
def _signal_fixture(urls: Urls):
    """Databases A, B (owned by this run), C (fkbench_ but tagged for another run), D (fkbench_ but untagged), plus a bystander
    session on each and on `postgres`. Yields (names, bystanders); every database is dropped afterwards."""
    suffix = uuid.uuid4().hex[:6]
    a, b = f"fkbench_ci_sig_a_{suffix}", f"fkbench_ci_sig_b_{suffix}"
    c, d = f"fkbench_ci_sig_c_{suffix}", f"fkbench_ci_sig_d_{suffix}"
    create_db(urls, a, owner=RUN)
    create_db(urls, b, owner=RUN)
    with connect(urls, "postgres") as conn:
        conn.execute(f'CREATE DATABASE "{c}"')
        conn.execute(f"COMMENT ON DATABASE \"{c}\" IS '{OWNER_TAG}another-run'")
        conn.execute(f'CREATE DATABASE "{d}"')
    sessions: dict[str, psycopg.Connection] = {}
    try:
        for label, db in (("a", a), ("b", b), ("c", c), ("d", d), ("postgres", "postgres")):
            sessions[label] = psycopg.connect(urls.conninfo(db), autocommit=True)
        yield {"a": a, "b": b, "c": c, "d": d}, sessions
    finally:
        for conn in sessions.values():
            conn.close()
        with connect(urls, "postgres") as conn:
            for name in (a, b, c, d):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _identity(conn: psycopg.Connection):
    return conn.info.backend_pid, conn.execute("SELECT backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()").fetchone()[0]


def _alive(conn: psycopg.Connection) -> bool:
    try:
        return not conn.closed and conn.execute("SELECT 1").fetchone() == (1,)
    except psycopg.Error:
        return False


def _admin_user(urls: Urls) -> str:
    return make_url(urls.admin).username


def test_signal_backend_cancels_and_terminates_only_the_exactly_authorized_session(urls):
    with _signal_fixture(urls) as (names, sessions):
        victim_cancel = psycopg.connect(urls.conninfo(names["a"]), autocommit=True)
        victim_term = psycopg.connect(urls.conninfo(names["a"]), autocommit=True)
        try:
            outcome: dict = {}

            def long_query():
                try:
                    victim_cancel.execute("SELECT pg_sleep(60)")
                    outcome["result"] = "finished"
                except psycopg.errors.QueryCanceled:
                    outcome["result"] = "cancelled"

            pid, start = _identity(victim_cancel)
            worker = threading.Thread(target=long_query)
            worker.start()
            deadline = time.time() + 30
            while time.time() < deadline:  # wait until the sleep is observably running, then cancel it
                with connect(urls, "postgres") as conn:
                    if conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s AND query LIKE 'SELECT pg_sleep%%' AND state = 'active'", (pid,)).fetchone():
                        break
                time.sleep(0.05)
            signal_backend(urls, pid=pid, backend_start=start, database=names["a"], owner=RUN, kind="cancel", username=_admin_user(urls), query_prefix="SELECT pg_sleep")
            worker.join(timeout=30)
            assert outcome.get("result") == "cancelled" and _alive(victim_cancel), "cancel must stop the statement but keep the session"

            tpid, tstart = _identity(victim_term)
            signal_backend(urls, pid=tpid, backend_start=tstart, database=names["a"], owner=RUN, kind="terminate", username=_admin_user(urls))
            deadline = time.time() + 30
            while time.time() < deadline and _alive(victim_term):
                time.sleep(0.05)
            assert not _alive(victim_term), "terminate must end the authorized session"
            assert all(_alive(c) for c in sessions.values()), "no bystander session may be affected by an authorized signal"
        finally:
            victim_cancel.close()
            victim_term.close()


def test_signal_backend_refuses_anything_but_the_exact_expected_session_and_signals_nothing(urls):
    with _signal_fixture(urls) as (names, sessions):
        user = _admin_user(urls)
        a_pid, a_start = _identity(sessions["a"])
        stale = psycopg.connect(urls.conninfo(names["a"]), autocommit=True)
        stale_pid, stale_start = _identity(stale)
        stale.close()
        deadline = time.time() + 30
        while time.time() < deadline:  # wait for the server to forget the closed session
            with connect(urls, "postgres") as conn:
                if conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (stale_pid,)).fetchone() is None:
                    break
            time.sleep(0.05)
        good = {"owner": RUN, "username": user}
        cases = {
            "session on postgres, caller names postgres": dict(sessions_key="postgres", pid_of="postgres", database="postgres", **good),
            "session on postgres, caller claims database A": dict(sessions_key="postgres", pid_of="postgres", database=names["a"], **good),
            "session on another fkbench_ database (B), caller expects A": dict(sessions_key="b", pid_of="b", database=names["a"], **good),
            "session on a foreign-tagged fkbench_ database (C)": dict(sessions_key="c", pid_of="c", database=names["c"], **good),
            "session on an untagged fkbench_ database (D)": dict(sessions_key="d", pid_of="d", database=names["d"], **good),
            "correct session, incorrect owner tag": dict(sessions_key="a", pid_of="a", database=names["a"], owner="some-other-run", username=user),
            "correct session, changed identity (wrong backend_start)": dict(sessions_key="a", pid_of="a", database=names["a"], backend_start=a_start - timedelta(seconds=5), **good),
            "correct session, unexpected role": dict(sessions_key="a", pid_of="a", database=names["a"], owner=RUN, username="nobody"),
            "correct session, unexpected statement": dict(sessions_key="a", pid_of="a", database=names["a"], query_prefix="CREATE INDEX", **good),
            "stale pid (session already closed)": dict(sessions_key=None, pid=stale_pid, backend_start=stale_start, database=names["a"], **good),
            "nonexistent pid": dict(sessions_key=None, pid=2_000_000_000, backend_start=a_start, database=names["a"], **good),
            "database outside the fkbench_ namespace": dict(sessions_key="postgres", pid_of="postgres", database="postgres", **good),
        }
        for label, case in cases.items():
            case = dict(case)
            case.pop("sessions_key")
            owner_of = case.pop("pid_of", None)
            if owner_of is not None:
                pid, start = _identity(sessions[owner_of])
                case.setdefault("pid", pid)
                case.setdefault("backend_start", start)
            for kind in ("cancel", "terminate"):
                with pytest.raises(UnsafeTarget, match="refusing to signal"):
                    signal_backend(urls, kind=kind, **case)
            assert all(_alive(c) for c in sessions.values()), f"{label}: a refusal must not signal any session"
        assert _identity(sessions["a"]) == (a_pid, a_start)


def test_template_with_a_foreign_owner_tag_is_never_reused_or_dropped(urls):
    spec = data.DatasetSpec.for_rows(1000, mode="raw")
    name = f"fkbench_tpl_ci_{uuid.uuid4().hex[:6]}"
    with connect(urls, "postgres") as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
        conn.execute(f"COMMENT ON DATABASE \"{name}\" IS '{OWNER_TAG}somebody-elses-template'")
    try:
        with pytest.raises(UnsafeTarget, match="not a template this tool built"):
            bench_main.ensure_template(urls, spec, name, rebuild=True)
        assert name in _databases(urls)
    finally:
        with connect(urls, "postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# -- statistics ------------------------------------------------------------------------------------------------------------


def test_percentile_is_nearest_rank_and_never_interpolates():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 50) == 30.0
    assert percentile(values, 95) == 50.0
    assert percentile(values, 99) == 50.0
    assert percentile([], 50) is None
    assert summarize([])["p99_ms"] is None and summarize([7.0])["max_ms"] == 7.0


# -- data generation ---------------------------------------------------------------------------------------------------------


def _fingerprint(urls: Urls, db: str):
    with connect(urls, db) as conn:
        return conn.execute(
            "SELECT count(*), md5(string_agg(id::text || dedup_key || coalesce(mapping_revision_id::text, ''), ',' ORDER BY dedup_key)) "
            "FROM telemetry_reading"
        ).fetchone()


def test_seed_is_deterministic_and_matches_the_requested_shape(urls, clone):
    rows, digest = _fingerprint(urls, clone)
    assert rows == SPEC.rows
    with connect(urls, clone) as conn:
        pinned = conn.execute("SELECT count(*) FROM telemetry_reading WHERE mapping_revision_id IS NOT NULL").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM telemetry_reading WHERE (mapping_revision_id IS NULL) <> (contract_evidence IS NULL)").fetchone()[0] == 0
        assert data.count(conn, "daily_telemetry_aggregate_lineage") == SPEC.lineage_rows
        assert data.orphan_mapping_id(conn, SPEC) is not None
    assert abs(pinned / rows - SPEC.profile.pinned_share) < 0.05
    other = f"fkbench_ci_{uuid.uuid4().hex[:8]}"
    create_db(urls, other, owner=RUN)
    try:
        alembic_ok(urls, other, "upgrade", PREVIOUS).cleanup()
        with connect(urls, other) as conn:
            data.seed(conn, SPEC)
        assert _fingerprint(urls, other) == (rows, digest), "the same spec must produce the same table"
    finally:
        drop_db(urls, other, owner=RUN)


def test_profile_validation_and_skew_and_churn(urls, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"series_cnt": 5}')
    with pytest.raises(ValueError, match="unknown profile fields"):
        data.Profile.from_json(bad)
    with pytest.raises(ValueError):
        data.Profile(skew=0.5).validate()
    skewed = data.DatasetSpec(rows=4000, lineage_rows=10, mappings=20, profile=data.Profile(series_count=200, skew=4.0, churn_fraction=0.2, retention_days=10))
    name = f"fkbench_ci_{uuid.uuid4().hex[:8]}"
    create_db(urls, name, owner=RUN)
    try:
        alembic_ok(urls, name, "upgrade", PREVIOUS).cleanup()
        with connect(urls, name) as conn:
            counts = data.seed(conn, skewed)
            top_share = conn.execute(
                "SELECT max(n)::float / sum(n) FROM (SELECT count(*) AS n FROM telemetry_reading GROUP BY series_key) s"
            ).fetchone()[0]
            span_days = conn.execute("SELECT extract(epoch FROM max(occurred_at) - min(occurred_at)) / 86400 FROM telemetry_reading").fetchone()[0]
        assert top_share > 3 * (1 / 200), "a skewed profile must concentrate readings on few series (uniform would be 1/200)"
        assert span_days <= 10, "the retention window bounds the time span"
        assert counts["dead_tuples_telemetry_reading"] > 0, "churn must leave dead tuples (bloat) behind"
        assert counts["telemetry_reading"] < skewed.rows, "churn deletes rows"
    finally:
        drop_db(urls, name, owner=RUN)


def test_fk_mode_loads_real_parents_with_foreign_keys_enforced(urls, clone_fk):
    with connect(urls, clone_fk, app_role=True) as conn:  # the APPLICATION role: the normal permission boundary
        assert data.count(conn, "collector") == FK_SPEC.profile.collectors
        assert data.count(conn, "integration") == FK_SPEC.profile.integrations
        orphans = conn.execute(
            "SELECT count(*) FROM telemetry_reading r LEFT JOIN collector c ON c.id = r.collector_id WHERE c.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0, "every reading must reference a real collector"
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, "
                "metric, unit, value, occurred_at, received_at) VALUES (md5('nope')::uuid, md5('i1')::uuid, md5('m1')::uuid, 'x', 's', 'fkcheck', 'm', 'u', 1, now(), now())"
            )


# -- workload --------------------------------------------------------------------------------------------------------------------


def test_raw_workload_inserts_rows_and_reports_latency_throughput_and_counts(urls, clone):
    work = Workload(dsn=urls.conninfo(clone), mappings=SPEC.mappings, writers=1, batch_rows=5, writes_per_second=20, readers=1)
    start = time.time()
    work.start()
    time.sleep(1.5)
    work.stop()
    window = work.window(start, time.time() + 1)
    assert window["write"]["completed"] > 0 and window["write"]["failed"] == 0
    assert window["write"]["rows_per_second"] > 0 and window["read"]["completed"] > 0 and window["read"]["failed"] == 0
    with connect(urls, clone) as conn:
        assert data.count(conn, "telemetry_reading") == SPEC.rows + window["write"]["completed"] * 5


@pytest.mark.parametrize("mode", ["fk", "service"])
def test_fk_and_service_ingest_run_as_the_application_role_with_fks_enforced(urls, clone_fk, mode):
    work = Workload(
        dsn=urls.conninfo(clone_fk, admin=False), sa_url=urls.sa_url(clone_fk), mode=mode, mappings=FK_SPEC.mappings,
        writers=1, batch_rows=3, writes_per_second=10, readers=1,
    )
    start = time.time()
    work.start()
    time.sleep(2.5)
    work.stop()
    window = work.window(start, time.time() + 1)
    assert work.errors == [], work.errors
    assert window["write"]["completed"] > 0 and window["write"]["failed"] == 0
    with connect(urls, clone_fk, app_role=True) as conn:
        assert data.count(conn, "telemetry_reading") == FK_SPEC.rows + window["write"]["completed"] * 3


def test_workload_rejects_unknown_mode_and_service_without_url():
    with pytest.raises(ValueError):
        Workload(dsn="x", mappings=1, mode="bogus")
    with pytest.raises(ValueError):
        Workload(dsn="x", mappings=1, mode="service")


# -- 22-index acceptance verification ---------------------------------------------------------------------------------------------


def _upgraded(urls: Urls, clone: str) -> dict[str, int]:
    alembic_ok(urls, clone, "upgrade", "head", timeout=STEP_TIMEOUT).cleanup()
    state = integrity(urls, clone)
    assert state["defects"] == [] and state["valid_and_ready"] == 22
    return state["oids"]


def _defects(urls: Urls, clone: str, oids=None) -> dict[str, set[str]]:
    with connect(urls, clone) as conn:
        found: dict[str, set[str]] = {}
        for d in verify_indexes(conn, oids)["defects"]:
            found.setdefault(d["index"], set()).add(d["kind"])
        return found


def test_acceptance_check_reports_missing_wrong_invalid_not_ready_and_substituted_indexes(urls, clone):
    oids = _upgraded(urls, clone)
    names = sorted(oids)
    missing, wrong, invalid, not_ready, swapped_a, swapped_b, partial, tablelike = names[:8]
    with connect(urls, clone) as conn:
        conn.execute(f"DROP INDEX {missing}")
        spec_of = {f"ix_{t}_{c}": (t, c) for t, c in FK_INDEXES}
        # right name, right table, WRONG column
        conn.execute(f"DROP INDEX {wrong}")
        conn.execute(f"CREATE INDEX {wrong} ON {spec_of[wrong][0]} (id)")
        # invalid and not ready (catalog flags, as an interrupted build leaves them)
        conn.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass", (invalid,))
        conn.execute("UPDATE pg_index SET indisready = false, indisvalid = false WHERE indexrelid = %s::regclass", (not_ready,))
        # substitution by rename: two real indexes trade names
        conn.execute(f"ALTER INDEX {swapped_a} RENAME TO tmp_swap")
        conn.execute(f"ALTER INDEX {swapped_b} RENAME TO {swapped_a}")
        conn.execute(f"ALTER INDEX tmp_swap RENAME TO {swapped_b}")
        # right name, table and column but PARTIAL (a different index)
        conn.execute(f"DROP INDEX {partial}")
        conn.execute(f"CREATE INDEX {partial} ON {spec_of[partial][0]} ({spec_of[partial][1]}) WHERE {spec_of[partial][1]} IS NOT NULL")
        # a table squatting on an index's name
        conn.execute(f"DROP INDEX {tablelike}")
        conn.execute(f"CREATE TABLE {tablelike} (id integer)")
    found = _defects(urls, clone, oids)
    assert found[missing] == {"missing"}
    assert "wrong_definition" in found[wrong]
    assert "invalid" in found[invalid]
    assert {"invalid", "not_ready"} <= found[not_ready]
    assert "wrong_definition" in found[swapped_a] and "wrong_definition" in found[swapped_b], "a substituted index must fail the definition check"
    assert "replaced" in found[swapped_a] and "replaced" in found[swapped_b], "the OIDs behind the swapped names changed"
    assert {"wrong_definition", "unexpected_shape"} <= found[partial]
    assert "not_an_index" in found[tablelike]
    verdict = integrity(urls, clone, oids)
    assert verdict["valid_and_ready"] == 22 - len(found), "valid_and_ready counts only indexes with NO defect of any kind"


def test_acceptance_check_detects_an_index_replaced_by_an_identical_one(urls, clone):
    """Same name, same definition, valid: only the OID reveals that the index was rebuilt or swapped for a look-alike."""
    oids = _upgraded(urls, clone)
    name = sorted(oids)[0]
    with connect(urls, clone) as conn:
        table, col = next((t, c) for t, c in FK_INDEXES if f"ix_{t}_{c}" == name)
        conn.execute(f"DROP INDEX {name}")
        conn.execute(f"CREATE INDEX {name} ON {table} ({col})")
    assert _defects(urls, clone) == {}, "without OID tracking an identical replacement is indistinguishable"
    assert _defects(urls, clone, oids) == {name: {"replaced"}}


# -- instrumented scenarios and recovery ----------------------------------------------------------------------------------------


def test_instrumented_upgrade_reports_invariants_and_renders(urls, template):
    cfg = Config(urls=urls, spec=SPEC, template=template, run_id=RUN, step_timeout=STEP_TIMEOUT)
    result = scenarios.s2_idle_upgrade(cfg)
    assert all(result["invariants"].values()), result["invariants"]
    assert result["upgrade_idle"]["exit_code"] == 0 and result["upgrade_idle"]["resources_delta"]["wal_bytes"] > 0
    over = result["overheads_seconds"]
    assert over["database_clone_seconds"] > 0 and over["migration_alembic_seconds"] > 0, "clone and migration time are reported separately"
    host = result["upgrade_idle"]["host_observations"]
    assert set(host["availability"]) >= {"cpu_percent_vm", "memory_available", "disk_free", "lock_waits"}
    timing = result["upgrade_idle"]["builds"]["timing_source"]
    assert timing.startswith(("pg_stat_statements", "progress sampling")), "per-index timing must say where it came from"
    full = {
        "dataset": {"rows": SPEC.rows, "lineage_rows": SPEC.lineage_rows, "mappings": SPEC.mappings, "mode": "raw", "profile": {}},
        "environment": {"postgres_version": "test", "runner_platform": "test", "runner_cpu_count": 1, "runner_mem_total_bytes": None, "postgres_settings": {}},
        "workload": "none",
        "scenarios": {"s2": result},
        "threshold_breaches": [],
        "limitations": list(report.limitations("raw", "raw")),
    }
    markdown = report.render_markdown(full)
    assert "NOT YET VALIDATED" in markdown and "S2/S6" in markdown and "Metric availability" in markdown


def test_provisional_thresholds_flag_breaches_without_accepting_anything():
    ingest = {"write": {"failed": 2, "p99_ms": 400.0, "completed": 10}, "read": {}}
    base = {"write": {"failed": 0, "p99_ms": 20.0, "completed": 10}, "read": {}}
    run = {"wall_seconds": 90.0, "ingest_during": ingest, "ingest_after": base, "resources_delta": {"wal_bytes": 600 * 1048576}, "host_observations": {"disk_free_gib": {"min": 3.0}}}
    results = {"s1_s5": {"baseline_ingest": base, "upgrade_under_ingest": run, "downgrade_under_ingest": {"ingest_during": base}, "reupgrade_under_ingest": {"ingest_during": base}}}
    metrics = {b["metric"] for b in scenarios.evaluate_thresholds(results, scenarios.Thresholds(max_migration_seconds=60, max_wal_mib=500, min_free_disk_gib=5, max_ingest_p99_ms=100))}
    assert metrics == {"failed_writes", "ingest_p99_ratio_during_vs_baseline", "ingest_p99_ms_during_upgrade", "upgrade_wall_seconds", "wal_mib", "min_free_disk_gib_observed"}
    assert scenarios.evaluate_thresholds({"s1_s5": {**results["s1_s5"], "upgrade_under_ingest": {**run, "ingest_during": base, "wall_seconds": 1.0, "resources_delta": {"wal_bytes": 0}}}}, scenarios.Thresholds()) == []


def _wait_for_build_phase(urls: Urls, db: str, run: AlembicRun, prefix: str):
    """Block (bounded) until the first large build is observably in a phase starting with `prefix`; return (pid, backend_start)."""
    deadline = time.time() + STEP_TIMEOUT
    with connect(urls, db) as conn:
        while time.time() < deadline:
            row = conn.execute(
                "SELECT p.pid, a.backend_start FROM pg_stat_progress_create_index p JOIN pg_class c ON c.oid = p.index_relid "
                "JOIN pg_stat_activity a ON a.pid = p.pid WHERE c.relname = %s AND p.phase LIKE %s",
                (FIRST_INDEX, prefix + "%"),
            ).fetchone()
            if row:
                return row[0], row[1]
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
        pid, backend_start = _wait_for_build_phase(urls, clone, run, "waiting for writers")
        signal_backend(
            urls, pid=pid, backend_start=backend_start, database=clone, owner=RUN, kind=interrupt,
            username=make_url(urls.app).username, query_prefix="CREATE INDEX",
        )
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
    assert final["version"] == HEAD and final["valid_and_ready"] == 22 and final["defects"] == []
    assert final["invalid_anywhere"] == 0
