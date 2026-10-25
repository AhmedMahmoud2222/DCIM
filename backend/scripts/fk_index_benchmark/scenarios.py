# ruff: noqa: E501
"""The benchmark scenarios. Each returns a JSON-serialisable dict; hard invariants are listed under "invariants".

An invariant is a yes/no fact (all 22 indexes match their complete expected definition and are valid, the version is unchanged
after a cancel, ...). Timings and latencies are measurements of THIS machine and THIS synthetic table; they carry no pass/fail
judgement here. Provisional acceptance thresholds are evaluated separately (see `Thresholds` and `evaluate_thresholds`).
"""

from __future__ import annotations

import statistics
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.engine.url import make_url

from . import data
from .harness import (
    HEAD,
    INDEX_NAMES,
    PREVIOUS,
    TARGET_TABLES,
    AlembicRun,
    Urls,
    connect,
    create_db,
    drop_db,
    index_sizes,
    integrity,
    signal_backend,
)
from .resources import StatementTimer
from .sampler import Sampler, delta, snapshot
from .workload import Workload

FIRST_INDEX = "ix_telemetry_reading_mapping_id"
BIG_INDEXES = (FIRST_INDEX, "ix_telemetry_reading_mapping_revision_id", "ix_daily_telemetry_aggregate_lineage_mapping_revision_id")


@dataclass(frozen=True)
class Thresholds:
    """PROVISIONAL acceptance thresholds. They are placeholders for the operator to replace with their own SLOs; breaching one is
    a flag for investigation, and passing all of them proves nothing about production."""

    max_write_failures: int = 0
    max_ingest_p99_ratio: float = 3.0  # p99 write latency during the migration / baseline p99
    max_ingest_p99_ms: float | None = None
    max_migration_seconds: float | None = None
    max_wal_mib: float | None = None
    min_free_disk_gib: float | None = None


@dataclass
class Config:
    urls: Urls
    spec: data.DatasetSpec
    template: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    ingest_mode: str = "raw"  # raw | fk | service (see workload.py); must be compatible with spec.mode
    writers: int = 2
    batch_rows: int = 10
    writes_per_second: float = 10.0
    readers: int = 1
    warmup_seconds: float = 30.0
    tail_seconds: float = 30.0
    hold_seconds: float = 45.0
    step_timeout: float = 7200.0
    keep_databases: bool = False
    thresholds: Thresholds = field(default_factory=Thresholds)
    timings: list = field(default_factory=list)  # (label, seconds) for clone/cleanup, kept apart from migration time


def say(message: str) -> None:
    print(f"[bench {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Clone:
    """A scratch database cloned from the 0056 template, owned by this run and dropped on exit unless the run keeps databases."""

    def __init__(self, cfg: Config, label: str):
        self.cfg, self.name = cfg, f"fkbench_{label}_{uuid.uuid4().hex[:6]}"

    def __enter__(self) -> str:
        t = time.time()
        create_db(self.cfg.urls, self.name, owner=self.cfg.run_id, template=self.cfg.template)
        seconds = time.time() - t
        self.cfg.timings.append(("clone", seconds))
        say(f"cloned {self.cfg.template} -> {self.name} in {seconds:.1f}s")
        return self.name

    def __exit__(self, *exc) -> None:
        if not self.cfg.keep_databases:
            t = time.time()
            drop_db(self.cfg.urls, self.name, owner=self.cfg.run_id)
            self.cfg.timings.append(("cleanup", time.time() - t))


def _workload(cfg: Config, db: str) -> Workload:
    app_side = cfg.ingest_mode != "raw"
    return Workload(
        dsn=cfg.urls.conninfo(db, admin=not app_side),
        sa_url=cfg.urls.sa_url(db) if cfg.ingest_mode == "service" else None,
        mode=cfg.ingest_mode,
        mappings=cfg.spec.mappings,
        collectors=cfg.spec.profile.collectors,
        integrations=cfg.spec.profile.integrations,
        writers=cfg.writers,
        batch_rows=cfg.batch_rows,
        writes_per_second=cfg.writes_per_second,
        readers=cfg.readers,
    )


def _overheads(cfg: Config, migration_seconds: float, scenario_seconds: float) -> dict:
    """Keep index-creation time apart from cloning, cleanup and everything else the scenario spends time on."""
    clone = sum(s for k, s in cfg.timings if k == "clone")
    cleanup = sum(s for k, s in cfg.timings if k == "cleanup")
    cfg.timings.clear()
    return {
        "migration_alembic_seconds": round(migration_seconds, 2),
        "database_clone_seconds": round(clone, 2),
        "database_cleanup_seconds": round(cleanup, 2),
        "other_seconds": round(max(scenario_seconds - migration_seconds - clone - cleanup, 0.0), 2),
        "scenario_total_seconds": round(scenario_seconds, 2),
        "note": "template creation/seeding happens once before the scenarios and is reported in the dataset section",
    }


def parent_delete_probe(cfg: Config, db: str, repeats: int = 3) -> dict:
    """Time the referential checks of deleting an UNREFERENCED parent (rolled back, so nothing changes).

    Deleting `integration_metric_mapping` makes PostgreSQL look for referencing rows in every child table; without an index
    on the referencing column that is a sequential scan. This is the work the new indexes remove.
    """
    with connect(cfg.urls, db, autocommit=False) as conn:
        orphan = data.orphan_mapping_id(conn, cfg.spec)
        conn.rollback()
        runs = []
        for _ in range(repeats):
            plan = conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) DELETE FROM integration_metric_mapping WHERE id = %s", (orphan,)
            ).fetchone()[0][0]
            conn.rollback()
            triggers = {t["Constraint Name"]: round(t["Time"], 3) for t in plan.get("Triggers", []) if t["Time"] >= 0.001}
            runs.append({"execution_ms": round(plan["Execution Time"], 3), "referential_check_ms": triggers})
    return {"repeats": runs, "median_execution_ms": round(statistics.median(r["execution_ms"] for r in runs), 3)}


@dataclass
class MigrationRun:
    rc: int
    seconds: float
    started: float
    ended: float
    resources: dict
    builds: dict
    locks: dict
    peak_db_bytes: int
    output_tail: str
    host: dict
    exact_statement_ms: dict | None


def _timer(cfg: Config, db: str) -> StatementTimer:
    return StatementTimer(cfg.urls.conninfo("postgres"), db)


def run_upgrade(cfg: Config, db: str, *args: str, direction: str = "upgrade") -> MigrationRun:
    """Run alembic to completion with in-database sampling, resource deltas, host observations and exact statement times."""
    sampler = Sampler(cfg.urls.conninfo(db), TARGET_TABLES)
    timer = _timer(cfg, db)
    have_timer = timer.reset()
    with connect(cfg.urls, db) as obs:
        before = snapshot(obs)
        sampler.start()
        run = AlembicRun(cfg.urls, db, direction, *(args or ("head" if direction == "upgrade" else "-1",)))
        try:
            rc = run.wait(cfg.step_timeout)
        finally:
            sampler.stop()
        after = snapshot(obs)
    result = MigrationRun(
        rc=rc,
        seconds=run.seconds,
        started=run.started,
        ended=run.ended or time.time(),
        resources=delta(before, after),
        builds=sampler.builds(),
        locks={t: sorted(f"{m}{'' if g else ' (waiting)'}" for m, g in modes) for t, modes in sampler.locks.items()},
        peak_db_bytes=sampler.peak_db_bytes,
        output_tail=run.output(1200),
        host=sampler.resources(),
        exact_statement_ms=timer.read() if have_timer else None,
    )
    run.cleanup()
    return result


def _build_summary(r: MigrationRun) -> dict:
    large = {k: v for k, v in r.builds.items() if k in BIG_INDEXES}
    exact = None
    if r.exact_statement_ms is not None:
        exact = {k: v for k, v in r.exact_statement_ms.items() if k in INDEX_NAMES}
    return {
        "observed_indexes": len(r.builds),
        "sampled_large_table_indexes": large,
        "exact_statement_times_ms": exact,
        "timing_source": "pg_stat_statements (exact per-statement wall time)" if exact is not None else "progress sampling only (0.1 s resolution); pg_stat_statements unavailable",
        "sampling_note": "sampled builds shorter than the 0.1 s interval may be missing from the sampled view; that is a sampling limit, not a zero",
    }


def _run_view(r: MigrationRun) -> dict:
    return {
        "exit_code": r.rc,
        "wall_seconds": r.seconds,
        "resources_delta": r.resources,
        "peak_database_bytes": r.peak_db_bytes,
        "lock_modes_seen_on_target_tables": r.locks,
        "builds": _build_summary(r),
        "host_observations": r.host,
        "output_tail": r.output_tail if r.rc != 0 else "",
    }


def s1_upgrade_under_ingest_and_s5_downgrade(cfg: Config) -> dict:
    """S1 (upgrade under paced ingest, with a no-migration baseline window), S5 (downgrade, re-upgrade), S7, S8."""
    out: dict = {"ingest_mode": cfg.ingest_mode}
    scenario_started = time.time()
    migration_seconds = 0.0
    with Clone(cfg, "s1") as db:
        work = _workload(cfg, db)
        work.start()
        say(f"S1 warm-up {cfg.warmup_seconds:.0f}s (baseline ingest, no migration)")
        time.sleep(min(3.0, cfg.warmup_seconds * 0.25))  # settle: connections and caches warm up; not part of the baseline window
        base_start = time.time()
        time.sleep(cfg.warmup_seconds)
        base_end = time.time()
        out["baseline_ingest"] = work.window(base_start, base_end)
        out["parent_delete_before"] = parent_delete_probe(cfg, db)

        say("S1 alembic upgrade head under ingest")
        up = run_upgrade(cfg, db)
        migration_seconds += up.seconds
        time.sleep(cfg.tail_seconds)
        tail_end = time.time()
        out["upgrade_under_ingest"] = {
            **_run_view(up),
            "ingest_during": work.window(up.started, up.ended),
            "ingest_after": work.window(up.ended, tail_end),
        }
        inv_up = integrity(cfg.urls, db)
        time.sleep(1)
        inv_after_tail = integrity(cfg.urls, db, previous_oids=inv_up["oids"])
        out["parent_delete_after"] = parent_delete_probe(cfg, db)
        out["sizes_after_upgrade"] = index_sizes(cfg.urls, db)

        say("S5 alembic downgrade -1 under ingest")
        down = run_upgrade(cfg, db, direction="downgrade")
        migration_seconds += down.seconds
        time.sleep(cfg.tail_seconds / 2)
        inv_down = integrity(cfg.urls, db)
        out["downgrade_under_ingest"] = {
            **_run_view(down),
            "ingest_during": work.window(down.started, down.ended),
            "note": "ACCESS EXCLUSIVE is held from lock acquisition to commit; writers queue behind it for that interval",
        }
        say("S5 re-upgrade under ingest")
        re_up = run_upgrade(cfg, db)
        migration_seconds += re_up.seconds
        work.stop()
        out["reupgrade_under_ingest"] = {**_run_view(re_up), "ingest_during": work.window(re_up.started, re_up.ended)}
        out["ingest_errors_seen"] = work.errors
        inv_final = integrity(cfg.urls, db)
        check = AlembicRun(cfg.urls, db, "check")
        check_rc = check.wait(900)
        check.cleanup()
        with connect(cfg.urls, db) as conn:
            readings = data.count(conn, "telemetry_reading")
        out["rows_after_run"] = {"telemetry_reading": readings, "ingested_during_run": readings - cfg.spec.rows}
    out["overheads_seconds"] = _overheads(cfg, migration_seconds, time.time() - scenario_started)
    out["invariants"] = {
        "upgrade_exit_zero": up.rc == 0,
        "all_22_match_expected_definition_and_valid_after_upgrade": inv_up["valid_and_ready"] == 22 and inv_up["version"] == HEAD,
        "no_index_defects_after_upgrade": inv_up["defects"] == [],
        "indexes_not_replaced_during_ingest_tail": inv_after_tail["defects"] == [],
        "no_invalid_indexes_after_upgrade": inv_up["invalid_anywhere"] == 0,
        "downgrade_exit_zero": down.rc == 0,
        "downgrade_atomic": inv_down["version"] == PREVIOUS and inv_down["present"] == 0,
        "reupgrade_exit_zero": re_up.rc == 0,
        "all_22_match_expected_definition_and_valid_after_reupgrade": inv_final["valid_and_ready"] == 22 and inv_final["version"] == HEAD and inv_final["defects"] == [],
        "alembic_check_clean": check_rc == 0,
        "advisory_lock_released": inv_final["advisory_locks_held"] == 0,
        "no_failed_writes": all(
            w["write"]["failed"] == 0
            for w in (out["upgrade_under_ingest"]["ingest_during"], out["upgrade_under_ingest"]["ingest_after"])
        ),
    }
    return out


def s2_idle_upgrade(cfg: Config) -> dict:
    """S2/S6: the same upgrade with no concurrent load, so WAL, temp and size figures are attributable to the build alone."""
    started = time.time()
    with Clone(cfg, "s2") as db:
        up = run_upgrade(cfg, db)
        inv = integrity(cfg.urls, db)
        sizes = index_sizes(cfg.urls, db)
    return {
        "upgrade_idle": _run_view(up),
        "sizes": sizes,
        "overheads_seconds": _overheads(cfg, up.seconds, time.time() - started),
        "invariants": {
            "upgrade_exit_zero": up.rc == 0,
            "all_22_match_expected_definition_and_valid": inv["valid_and_ready"] == 22 and inv["version"] == HEAD and inv["defects"] == [],
        },
    }


def _wait_for(predicate, run: AlembicRun, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        if run.poll() is not None:
            raise RuntimeError(f"alembic exited (rc={run.proc.returncode}) before: {what}\n{run.output(1500)}")
        time.sleep(0.1)
    raise RuntimeError(f"timed out after {timeout:.0f}s waiting for: {what}")


def _progress(cfg: Config, db: str) -> list[tuple]:
    with connect(cfg.urls, db) as conn:
        return conn.execute(
            "SELECT p.pid, c.relname, p.phase, p.blocks_total, p.blocks_done, p.lockers_total, p.lockers_done, a.backend_start "
            "FROM pg_stat_progress_create_index p LEFT JOIN pg_class c ON c.oid = p.index_relid "
            "LEFT JOIN pg_stat_activity a ON a.pid = p.pid"
        ).fetchall()


def s3_old_snapshot_blocker(cfg: Config) -> dict:
    """A long-lived transaction with an old snapshot: the build must wait for it, ingest must not."""
    started = time.time()
    with Clone(cfg, "s3") as db:
        work = _workload(cfg, db)
        work.start()
        time.sleep(5)
        holder = connect(cfg.urls, db, autocommit=False)
        holder.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT count(*) FROM (SELECT 1 FROM telemetry_reading LIMIT 1) s").fetchone()
        run = AlembicRun(cfg.urls, db, "upgrade", "head")
        waiting_since = None
        phases_seen: set[str] = set()
        try:
            deadline = time.time() + cfg.step_timeout
            while time.time() < deadline and run.poll() is None:
                rows = [r for r in _progress(cfg, db) if r[1] == FIRST_INDEX]
                for r in rows:
                    phases_seen.add(r[2])
                    if "waiting for old snapshots" in r[2] and waiting_since is None:
                        waiting_since = time.time()
                if waiting_since is not None:
                    break
                time.sleep(0.2)
            if waiting_since is None:
                raise RuntimeError(f"never observed the build waiting for old snapshots; phases seen: {sorted(phases_seen)}")
            say(f"S3 build is waiting for the old snapshot; holding it for {cfg.hold_seconds:.0f}s")
            hold_start = time.time()
            time.sleep(cfg.hold_seconds)
            hold_end = time.time()
            still_waiting = run.poll() is None
            ingest_hold = work.window(hold_start, hold_end)
            holder.rollback()
            holder.close()
            released = time.time()
            rc = run.wait(cfg.step_timeout)
        finally:
            work.stop()
            if run.poll() is None:
                run.proc.kill()
        finished = time.time()
        inv = integrity(cfg.urls, db)
        output = run.output(800)
        migration_seconds = run.seconds
        run.cleanup()
    return {
        "phases_seen_on_first_index": sorted(phases_seen),
        "held_seconds": round(hold_end - hold_start, 1),
        "migration_still_waiting_at_end_of_hold": still_waiting,
        "ingest_while_build_waited": ingest_hold,
        "seconds_from_release_to_finish": round(finished - released, 1),
        "exit_code": rc,
        "output_tail": output if rc != 0 else "",
        "overheads_seconds": _overheads(cfg, migration_seconds, time.time() - started),
        "invariants": {
            "build_waited_for_old_snapshot": still_waiting,
            "ingest_not_blocked_while_waiting": ingest_hold["write"]["failed"] == 0 and ingest_hold["write"]["completed"] > 0,
            "upgrade_completed_after_release": rc == 0 and inv["valid_and_ready"] == 22 and inv["version"] == HEAD and inv["defects"] == [],
        },
    }


def s4_cancellation_and_recovery(cfg: Config, variants=("cancel", "terminate", "kill_client")) -> dict:
    """Interrupt the large-table build three ways, then prove a plain re-run recovers."""
    out: dict = {}
    invariants: dict = {}
    started = time.time()
    migration_seconds = 0.0
    for variant in variants:
        with Clone(cfg, f"s4{variant[:3]}") as db:
            run = AlembicRun(cfg.urls, db, "upgrade", "head")
            try:

                def _building() -> bool:
                    rows = [r for r in _progress(cfg, db) if r[1] == FIRST_INDEX and r[2].startswith("building index")]
                    return bool(rows) and rows[0][3] > 0 and rows[0][4] >= rows[0][3] * 0.25

                _wait_for(_building, run, "the first large build to pass 25% of its table scan", cfg.step_timeout)
                build = next(r for r in _progress(cfg, db) if r[1] == FIRST_INDEX)
                pid, backend_start = build[0], build[7]
                interrupted_at = time.time()
                if variant in ("cancel", "terminate"):
                    # Exact identity: this clone, this run's owner tag, this pid AND its backend_start, the application role, a CREATE INDEX
                    signal_backend(
                        cfg.urls, pid=pid, backend_start=backend_start, database=db, owner=cfg.run_id, kind=variant,
                        username=make_url(cfg.urls.app).username, query_prefix="CREATE INDEX",
                    )
                else:
                    run.proc.kill()  # the client dies without telling the server
                if variant == "kill_client":
                    run.proc.wait()
                    run.ended = run.ended or time.time()
                    # The server-side build may well keep going; wait for it to stop (bounded) and record what it left.
                    _wait_idle(cfg, db, cfg.step_timeout)
                else:
                    run.wait(600)
                after_interrupt = integrity(cfg.urls, db)
                interrupted_rc = run.proc.returncode
                migration_seconds += run.seconds
            finally:
                if run.poll() is None:
                    run.proc.kill()
                run.cleanup()
            with connect(cfg.urls, db) as conn:
                leftover_locks = conn.execute(
                    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname = ANY(%s) AND l.pid <> pg_backend_pid()",
                    (TARGET_TABLES,),
                ).fetchone()[0]
            say(f"S4/{variant}: interrupted; re-running")
            rerun = run_upgrade(cfg, db)
            migration_seconds += rerun.seconds
            final = integrity(cfg.urls, db)
        out[variant] = {
            "exit_code_of_interrupted_run": interrupted_rc,
            "state_after_interrupt": {k: v for k, v in after_interrupt.items() if k != "oids"},
            "locks_left_on_target_tables": leftover_locks,
            "rerun": _run_view(rerun),
            "state_after_rerun": {k: v for k, v in final.items() if k != "oids"},
            "seconds_from_interrupt_to_recovered": round(rerun.ended - interrupted_at, 1),
        }
        invariants[f"{variant}__version_not_advanced_by_interrupted_run"] = after_interrupt["version"] == PREVIOUS
        invariants[f"{variant}__no_locks_left"] = leftover_locks == 0
        invariants[f"{variant}__advisory_lock_released"] = after_interrupt["advisory_locks_held"] == 0
        if variant != "kill_client":
            invariants[f"{variant}__interrupted_run_failed"] = interrupted_rc != 0
            invariants[f"{variant}__invalid_artifact_left_behind"] = FIRST_INDEX in after_interrupt["invalid_named"]
        invariants[f"{variant}__rerun_recovers_all_22"] = (
            rerun.rc == 0 and final["valid_and_ready"] == 22 and final["version"] == HEAD and final["defects"] == []
        )
        invariants[f"{variant}__no_invalid_left_after_rerun"] = final["invalid_anywhere"] == 0
    out["overheads_seconds"] = _overheads(cfg, migration_seconds, time.time() - started)
    out["invariants"] = invariants
    return out


def _wait_idle(cfg: Config, db: str, timeout: float) -> None:
    """Wait until no CREATE INDEX backend is left in `db`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _progress(cfg, db):
            time.sleep(1.0)
            if not _progress(cfg, db):
                return
        time.sleep(0.5)
    raise RuntimeError("an orphaned index build was still running at the timeout")


SCENARIOS = {
    "s1_s5": s1_upgrade_under_ingest_and_s5_downgrade,
    "s2": s2_idle_upgrade,
    "s3": s3_old_snapshot_blocker,
    "s4": s4_cancellation_and_recovery,
}


def evaluate_thresholds(results: dict, thresholds: Thresholds, repeats_note: str = "") -> list[dict]:
    """Return the PROVISIONAL thresholds breached by `results` (one entry per breach). An empty list is not an acceptance."""
    breaches: list[dict] = []

    def add(scenario: str, metric: str, value, limit) -> None:
        breaches.append({"scenario": scenario, "metric": metric, "value": value, "limit": limit, "provisional": True})

    s1 = results.get("s1_s5")
    if s1:
        up = s1["upgrade_under_ingest"]
        failures = sum(w["write"]["failed"] for w in (up["ingest_during"], up["ingest_after"], s1["downgrade_under_ingest"]["ingest_during"], s1["reupgrade_under_ingest"]["ingest_during"]))
        if failures > thresholds.max_write_failures:
            add("s1_s5", "failed_writes", failures, thresholds.max_write_failures)
        base_p99, during_p99 = s1["baseline_ingest"]["write"]["p99_ms"], up["ingest_during"]["write"]["p99_ms"]
        if base_p99 and during_p99 is not None and during_p99 / base_p99 > thresholds.max_ingest_p99_ratio:
            add("s1_s5", "ingest_p99_ratio_during_vs_baseline", round(during_p99 / base_p99, 2), thresholds.max_ingest_p99_ratio)
        if thresholds.max_ingest_p99_ms is not None and during_p99 is not None and during_p99 > thresholds.max_ingest_p99_ms:
            add("s1_s5", "ingest_p99_ms_during_upgrade", during_p99, thresholds.max_ingest_p99_ms)
        if thresholds.max_migration_seconds is not None and up["wall_seconds"] > thresholds.max_migration_seconds:
            add("s1_s5", "upgrade_wall_seconds", up["wall_seconds"], thresholds.max_migration_seconds)
        if thresholds.max_wal_mib is not None and up["resources_delta"]["wal_bytes"] / 1048576 > thresholds.max_wal_mib:
            add("s1_s5", "wal_mib", round(up["resources_delta"]["wal_bytes"] / 1048576, 1), thresholds.max_wal_mib)
        disk = up["host_observations"]["disk_free_gib"]
        if thresholds.min_free_disk_gib is not None and disk and disk["min"] < thresholds.min_free_disk_gib:
            add("s1_s5", "min_free_disk_gib_observed", disk["min"], thresholds.min_free_disk_gib)
    s2 = results.get("s2")
    if s2 and thresholds.max_wal_mib is not None and s2["upgrade_idle"]["resources_delta"]["wal_bytes"] / 1048576 > thresholds.max_wal_mib:
        add("s2", "wal_mib", round(s2["upgrade_idle"]["resources_delta"]["wal_bytes"] / 1048576, 1), thresholds.max_wal_mib)
    return breaches
