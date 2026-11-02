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

import ast
import json
import pathlib
import re
import socket
import struct
import threading
import time
import uuid

import psycopg
import pytest
from sqlalchemy.engine.url import make_url

from scripts.fk_index_benchmark import __main__ as bench_main
from scripts.fk_index_benchmark import data, report, scenarios
from scripts.fk_index_benchmark import harness as harness_module
from scripts.fk_index_benchmark import sampler as sampler_module
from scripts.fk_index_benchmark.cancel import BackendKey, NoBackendKey, PgProxy, send_cancel_request
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


def _alive(conn: psycopg.Connection) -> bool:
    try:
        return not conn.closed and conn.execute("SELECT 1").fetchone() == (1,)
    except psycopg.Error:
        return False


def _wait_sleeping(urls: Urls, pid: int) -> None:
    """Block (bounded) until `pid` is observably inside pg_sleep."""
    deadline = time.time() + 30
    with connect(urls, "postgres") as conn:
        while time.time() < deadline:
            if conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s AND state = 'active' AND query LIKE 'SELECT pg_sleep%%'", (pid,)).fetchone():
                return
            time.sleep(0.05)
    raise AssertionError("session never started sleeping")


def _sleeper(conn: psycopg.Connection, outcome: dict) -> threading.Thread:
    def run():
        try:
            conn.execute("SELECT pg_sleep(60)")
            outcome["result"] = "finished"
        except psycopg.errors.QueryCanceled:
            outcome["result"] = "cancelled"
        except psycopg.Error as exc:
            outcome["result"] = f"error: {type(exc).__name__}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_no_pid_only_signalling_exists_anywhere_in_the_benchmark():
    """pg_cancel_backend / pg_terminate_backend / pg_signal_backend are PID-only and cannot be made race-free (see cancel.py).
    No executable string may call them (including f-strings that assemble the name), and no function may exist to wrap them.
    Explanatory prose and docstrings may mention them."""
    package = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "fk_index_benchmark"
    names = re.compile(r"pg_(?:cancel|terminate|signal)_backend")
    sql_use = re.compile(r"^\s*SELECT\s+pg_(?:cancel|terminate|signal)_backend|pg_(?:cancel|terminate|signal)_backend\s*\(\s*(?:%|\w+\.pid|\d)")
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if names.fullmatch(node.value) or sql_use.search(node.value) or node.value.rstrip().endswith("SELECT pg_") or node.value.startswith("_backend("):
                    offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
            if isinstance(node, ast.JoinedStr) and re.search(r"pg_\{[^}]*\}_backend|\{[^}]*\}\(a\.pid\)", ast.unparse(node)):
                offenders.append(f"{path.name}:{node.lineno}: f-string builds a backend-signalling call")
            if isinstance(node, ast.FunctionDef) and node.name in ("signal_backend", "kill_pid", "terminate_backend", "cancel_backend"):
                offenders.append(f"{path.name}:{node.lineno}: function {node.name}")
    assert offenders == [], offenders
    assert not hasattr(harness_module, "signal_backend"), "the PID-only signalling function must not exist"


def test_the_pid_only_guard_would_catch_the_previous_implementation():
    """Mutation check of the guard itself: the previous implementation's constructs must be flagged by the same patterns."""
    previous = """
def signal_backend(urls, pid, kind):
    function = {"cancel": "pg_cancel_backend", "terminate": "pg_terminate_backend"}[kind]
    conn.execute(f"SELECT {function}(a.pid) FROM pg_stat_activity a")
    conn.execute(f"SELECT pg_{kind}_backend(%s)", (pid,))
"""
    tree = ast.parse(previous)
    names = re.compile(r"pg_(?:cancel|terminate|signal)_backend")
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and names.fullmatch(n.value)]
    joined = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and re.search(r"pg_\{[^}]*\}_backend|\{[^}]*\}\(a\.pid\)", ast.unparse(n))]
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "signal_backend"]
    assert len(hits) == 2 and len(joined) == 2 and len(funcs) == 1


def test_proxy_reads_the_backend_key_and_a_key_authenticated_cancel_stops_only_that_session(urls):
    with PgProxy(*urls.server_address) as proxy:
        target = psycopg.connect(host=proxy.host, port=proxy.port, dbname="postgres", user=make_url(urls.admin).username, password=make_url(urls.admin).password, autocommit=True)  # via the relay
        bystander = psycopg.connect(urls.conninfo("postgres"), autocommit=True)  # direct, never seen by the relay
        try:
            key = proxy.only_key(timeout=10)
            assert key.pid == target.info.backend_pid, "the key must belong to the proxied connection"
            assert key.pid != bystander.info.backend_pid
            t_out, b_out = {}, {}
            t_thread, b_thread = _sleeper(target, t_out), _sleeper(bystander, b_out)
            _wait_sleeping(urls, target.info.backend_pid)
            _wait_sleeping(urls, bystander.info.backend_pid)
            proxy.cancel(key)
            t_thread.join(timeout=30)
            assert t_out.get("result") == "cancelled" and _alive(target), "the key-authenticated cancel must stop the proxied statement"
            time.sleep(0.5)
            assert "result" not in b_out, "the unrelated session must not be affected by another connection's cancel"
            bystander.cancel_safe()  # clean up through psycopg's own authenticated cancel of its own connection
            b_thread.join(timeout=30)
        finally:
            target.close()
            bystander.close()


def test_a_pid_without_the_right_key_cannot_target_an_unrelated_session(urls):
    with PgProxy(*urls.server_address) as proxy:
        proxied = psycopg.connect(host=proxy.host, port=proxy.port, dbname="postgres", user=make_url(urls.admin).username, password=make_url(urls.admin).password, autocommit=True)
        victim = psycopg.connect(urls.conninfo("postgres"), autocommit=True)
        try:
            real = proxy.only_key(timeout=10)
            victim_pid = victim.info.backend_pid
            out: dict = {}
            thread = _sleeper(victim, out)
            _wait_sleeping(urls, victim_pid)
            host, port = urls.server_address
            # Deliberately target the victim's PID with every wrong secret we can think of, including another session's real key.
            for guess in (0, 1, real.secret, real.secret + 1, -1):
                send_cancel_request(host, port, BackendKey(victim_pid, guess))
            time.sleep(1.0)
            assert "result" not in out and _alive(proxied), "a CancelRequest with the wrong key must be ignored by PostgreSQL"
            with connect(urls, "postgres") as conn:
                assert conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s AND state = 'active'", (victim_pid,)).fetchone()
            victim.cancel_safe()
            thread.join(timeout=30)
            assert out.get("result") == "cancelled", "the victim's own authenticated cancel still works"
        finally:
            proxied.close()
            victim.close()


def test_proxy_refuses_to_pick_among_connections_and_flags_tls():
    # TLS: a server that answers the SSLRequest with 'S' makes the stream opaque, so no key can be read and cancel is refused.
    fake = socket.socket()
    fake.bind(("127.0.0.1", 0))
    fake.listen(1)

    def serve():
        conn, _ = fake.accept()
        conn.recv(8)
        conn.sendall(b"S")
        time.sleep(0.5)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with PgProxy("127.0.0.1", fake.getsockname()[1]) as proxy:
        with socket.create_connection((proxy.host, proxy.port)) as client:
            client.sendall(struct.pack("!ii", 8, 80877103))
            assert client.recv(1) == b"S"
        with pytest.raises(NoBackendKey, match="TLS"):
            proxy.only_key(timeout=5)
    fake.close()


def test_proxy_refuses_to_guess_when_several_connections_are_proxied(urls):
    with PgProxy(*urls.server_address) as proxy:
        creds = dict(dbname="postgres", user=make_url(urls.admin).username, password=make_url(urls.admin).password, autocommit=True)
        a = psycopg.connect(host=proxy.host, port=proxy.port, **creds)
        b = psycopg.connect(host=proxy.host, port=proxy.port, **creds)
        try:
            with pytest.raises(NoBackendKey, match="refusing to pick"):
                proxy.only_key(timeout=5)
        finally:
            a.close()
            b.close()


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
    # WAL and I/O are instance-wide and say so; nothing claims they are the migration's alone
    assert result["upgrade_idle"]["resource_scopes"]["wal_bytes"] == "postgres_instance"
    assert "other_databases_xacts" in result["upgrade_idle"]["resources_delta"]
    wal_line = next(line for line in markdown.splitlines() if line.startswith("- WAL written during the run"))
    assert "[PostgreSQL instance-wide]" in wal_line
    assert "[PostgreSQL instance-wide]" in next(line for line in markdown.splitlines() if line.startswith("- I/O during the run"))
    assert "- Attribution:" in markdown and "NOT attributable to the migration alone" in markdown
    assert "attributable to the build alone" not in markdown and "migration-only values" not in wal_line


def test_provisional_thresholds_flag_breaches_without_accepting_anything():
    ingest = {"write": {"failed": 2, "p99_ms": 400.0, "completed": 10}, "read": {}}
    base = {"write": {"failed": 0, "p99_ms": 20.0, "completed": 10}, "read": {}}
    run = {"wall_seconds": 90.0, "ingest_during": ingest, "ingest_after": base, "resources_delta": {"wal_bytes": 600 * 1048576}, "host_observations": {"disk_free_gib": {"min": 3.0}}}
    results = {"s1_s5": {"baseline_ingest": base, "upgrade_under_ingest": run, "downgrade_under_ingest": {"ingest_during": base}, "reupgrade_under_ingest": {"ingest_during": base}}}
    metrics = {b["metric"] for b in scenarios.evaluate_thresholds(results, scenarios.Thresholds(max_migration_seconds=60, max_wal_mib=500, min_free_disk_gib=5, max_ingest_p99_ms=100))}
    assert metrics == {"failed_writes", "ingest_p99_ratio_during_vs_baseline", "ingest_p99_ms_during_upgrade", "upgrade_wall_seconds", "wal_mib", "min_free_disk_gib_observed"}
    assert scenarios.evaluate_thresholds({"s1_s5": {**results["s1_s5"], "upgrade_under_ingest": {**run, "ingest_during": base, "wall_seconds": 1.0, "resources_delta": {"wal_bytes": 0}}}}, scenarios.Thresholds()) == []


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


@pytest.mark.parametrize("interrupt", ["cancel", "kill_client"])
def test_interrupted_build_leaves_invalid_artifact_and_a_plain_rerun_recovers(urls, clone, interrupt):
    """Barrier: an open transaction that wrote to telemetry_reading, so CREATE INDEX CONCURRENTLY waits for writers."""
    blocker = connect(urls, clone, autocommit=False)
    blocker.execute("SET session_replication_role = replica")
    blocker.execute(
        "INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, "
        "metric, unit, value, occurred_at, received_at) SELECT md5('c')::uuid, md5('i')::uuid, md5('m1')::uuid, 'x', 's', 'blocker', 'm', 'u', 1, now(), now()"
    )
    proxy = PgProxy(*urls.server_address) if interrupt == "cancel" else None
    run = AlembicRun(urls, clone, "upgrade", "head", via=(proxy.host, proxy.port) if proxy else None)
    try:
        pid = _wait_for_build_phase(urls, clone, run, "waiting for writers")
        if proxy:
            key = proxy.only_key(timeout=10)
            assert key.pid == pid, "the proxied connection must be the one running the build"
            proxy.cancel(key)
            rc = run.wait(STEP_TIMEOUT)
        else:
            run.proc.kill()  # a process this test started: no PID lookup involved
            rc = run.proc.wait()
            blocker.rollback()  # let the orphaned server-side build finish or fail, then wait for it to go away
            deadline = time.time() + STEP_TIMEOUT
            with connect(urls, clone) as conn:
                while time.time() < deadline and conn.execute("SELECT count(*) FROM pg_stat_progress_create_index").fetchone()[0]:
                    time.sleep(0.2)
    finally:
        if run.poll() is None:
            run.proc.kill()
        run.cleanup()
        if proxy:
            proxy.close()
    assert rc != 0, "an interrupted build must fail the migration"
    state = integrity(urls, clone)
    assert state["version"] == PREVIOUS, "alembic_version must not advance when the run was interrupted"
    if interrupt == "cancel":
        assert FIRST_INDEX in state["invalid_named"], "an interrupted concurrent build leaves an INVALID index"
    assert state["advisory_locks_held"] == 0, "the migration advisory lock must be released with the connection"

    blocker.rollback()
    blocker.close()
    alembic_ok(urls, clone, "upgrade", "head", timeout=STEP_TIMEOUT).cleanup()
    final = integrity(urls, clone)
    assert final["version"] == HEAD and final["valid_and_ready"] == 22 and final["defects"] == []
    assert final["invalid_anywhere"] == 0


def test_terminate_scenario_is_explicitly_disabled_not_silently_weakened():
    assert "terminate" not in scenarios.s4_cancellation_and_recovery.__defaults__[0]
    assert "no authenticated way to terminate" in scenarios.TERMINATE_UNAVAILABLE
    markdown = report.render_markdown(
        {
            "dataset": {"rows": 1, "lineage_rows": 1, "mappings": 1, "mode": "raw", "profile": {}},
            "environment": {"postgres_version": "t", "runner_platform": "t", "runner_cpu_count": 1, "runner_mem_total_bytes": None, "postgres_settings": {}},
            "workload": "none",
            "scenarios": {"s4": {"terminate": {"status": "not_run", "reason": scenarios.TERMINATE_UNAVAILABLE}, "invariants": {}}},
            "threshold_breaches": [],
            "limitations": list(report.limitations("raw", "raw")),
        }
    )
    assert "terminate: NOT RUN" in markdown and "PID-only" in markdown


# -- measurement attribution (WAL / I/O scopes) -------------------------------------------------------------------------------------


def test_every_measured_metric_declares_a_scope_and_wal_and_io_are_instance_wide(urls, clone):
    with connect(urls, clone) as conn:
        snap = sampler_module.snapshot(conn)
    assert set(snap) == set(sampler_module.SNAPSHOT_KEYS)
    for key in snap:
        assert sampler_module.METRIC_SCOPES[key] in sampler_module.SCOPE_LABELS, key
    for key in ("wal_bytes", "io_read_bytes", "io_write_bytes", "other_databases_xacts"):
        assert sampler_module.METRIC_SCOPES[key] == "postgres_instance", key
    for key in ("temp_bytes", "db_bytes"):
        assert sampler_module.METRIC_SCOPES[key] == "database", key
    assert all(sampler_module.METRIC_SCOPES[k] == "host_vm" for k in ("cpu_percent_vm", "memory_available_mib", "disk_free_gib"))
    assert "NOT attributable to the migration alone" in sampler_module.ATTRIBUTION_NOTE


# -- per-repeat retention ------------------------------------------------------------------------------------------------------------


def _window(p50, p95, p99, ok=100, failed=0, rate=200.0):
    return {"write": {"p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "completed": ok, "failed": failed, "rows_per_second": rate}, "read": {}}


def _s1_run(base, during, after, up=5.0, down=3.0, re_up=6.0):
    return {
        "baseline_ingest": base,
        "upgrade_under_ingest": {"wall_seconds": up, "ingest_during": during, "ingest_after": after},
        "downgrade_under_ingest": {"wall_seconds": down},
        "reupgrade_under_ingest": {"wall_seconds": re_up},
        "invariants": {"upgrade_exit_zero": True},
    }


def test_aggregate_keeps_every_repeat_and_never_reconstructs_missing_values():
    run1 = _s1_run(_window(80, 100, 140), _window(70, 85, 93), _window(None, None, None, ok=0))  # no successful post-upgrade writes recorded
    run2 = _s1_run(_window(79, 107, 156), _window(1214, 1934, 1958, rate=201.9), _window(723, 1900, 1929), up=8.9)
    breach = {"scenario": "s1_s5", "metric": "ingest_p99_ratio_during_vs_baseline", "value": 12.5, "limit": 3.0, "run": 2}
    merged = bench_main.aggregate("s1_s5", [run1, run2], [[], [breach]])
    r1, r2 = merged["repeat_details"]
    assert (r1["run"], r2["run"]) == (1, 2)
    assert r1["windows"]["baseline"]["p99_ms"] == 140 and r2["windows"]["baseline"]["p99_ms"] == 156
    assert r1["windows"]["during_upgrade"]["p99_ms"] == 93 and r2["windows"]["during_upgrade"]["p99_ms"] == 1958
    assert r1["windows"]["after_upgrade"]["p99_ms"] == {"available": False, "reason": "no successful operations in this window"}
    assert r2["windows"]["after_upgrade"]["p99_ms"] == 1929, "run 2's value must not leak into run 1 (nor the reverse)"
    assert (r1["migration_seconds"]["upgrade"], r2["migration_seconds"]["upgrade"]) == (5.0, 8.9)
    assert r1["threshold_breaches"] == [] and r2["threshold_breaches"] == [breach]
    summary = merged["repeat_metrics"]["upgrade_wall_s"]
    assert (summary["min"], summary["median"], summary["max"], summary["values"]) == (5.0, 6.95, 8.9, [5.0, 8.9])
    # a metric that exists in only one repeat is summarised from the repeats that have it, and never invented for the other
    sparse = bench_main.aggregate("s1_s5", [run1, {**run2, "downgrade_under_ingest": {}}])
    assert sparse["repeat_details"][1]["migration_seconds"]["downgrade"] == {"available": False, "reason": "not present in this run's data"}
    assert sparse["repeat_metrics"]["downgrade_wall_s"]["values"] == [3.0]


def test_markdown_shows_each_repeat_and_marks_unavailable_data_explicitly():
    run1 = _s1_run(_window(80, 100, 140), _window(70, 85, 93), _window(None, None, None, ok=0))
    run2 = _s1_run(_window(79, 107, 156), _window(1214, 1934, 1958), _window(723, 1900, 1929), up=8.9)
    result = bench_main.aggregate("s1_s5", [run1, run2], [[], []])
    text = "\n".join(report._repeat_tables("s1_s5", result))
    assert "| 1 | 5.0 |" in text and "| 2 | 8.9 |" in text
    assert "93.0" in text or "93" in text
    assert "n/a (no successful operations in this window)" in text, "unavailable figures are labelled, not blanked or filled"


def test_a_historical_report_without_per_repeat_detail_is_rendered_truthfully():
    path = pathlib.Path(__file__).resolve().parents[3] / "docs" / "benchmark-results" / "0057" / "fk_index_benchmark_1000000_20261025T090918Z.json"
    historical = json.loads(path.read_text())
    assert "repeat_details" not in historical["scenarios"]["s1_s5"], "this fixture must be the pre-retention report"
    markdown = report.render_markdown(historical)
    assert "Per-repeat detail was NOT recorded" in markdown and "not reconstructed" in markdown
    assert "[scope not recorded]" in markdown or "PostgreSQL instance-wide" in markdown
    assert "| run |" not in markdown, "no per-repeat table may be fabricated for a report that never recorded one"
