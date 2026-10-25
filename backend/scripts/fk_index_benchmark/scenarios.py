# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""The benchmark scenarios. Each returns a JSON-serialisable dict; hard invariants are listed under "invariants".

An invariant is a yes/no fact (all 22 indexes valid and ready, the version unchanged after a cancel, ...). Timings and
latencies are measurements of THIS machine and THIS synthetic table and carry no pass/fail judgement here.
"""

from __future__ import annotations

import statistics
import time
import uuid
from dataclasses import dataclass, field

from . import data
from .harness import (
    HEAD,
    PREVIOUS,
    TARGET_TABLES,
    AlembicRun,
    Urls,
    connect,
    create_db,
    drop_db,
    index_sizes,
    integrity,
)
from .sampler import Sampler, delta, snapshot
from .workload import Workload

FIRST_INDEX = "ix_telemetry_reading_mapping_id"
BIG_INDEXES = (
    FIRST_INDEX,
    "ix_telemetry_reading_mapping_revision_id",
    "ix_daily_telemetry_aggregate_lineage_mapping_revision_id",
)


@dataclass
class Config:
    urls: Urls
    spec: data.DatasetSpec
    template: str
    writers: int = 2
    batch_rows: int = 10
    writes_per_second: float = 10.0
    warmup_seconds: float = 30.0
    tail_seconds: float = 30.0
    hold_seconds: float = 45.0
    step_timeout: float = 7200.0
    keep_databases: bool = False
    log: list = field(default_factory=list)


def say(message: str) -> None:
    print(f"[bench {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Clone:
    """A scratch database cloned from the 0056 template, dropped on exit unless the run keeps databases."""

    def __init__(self, cfg: Config, label: str):
        self.cfg, self.name = cfg, f"fkbench_{label}_{uuid.uuid4().hex[:6]}"

    def __enter__(self) -> str:
        t = time.time()
        create_db(self.cfg.urls, self.name, template=self.cfg.template)
        say(f"cloned {self.cfg.template} -> {self.name} in {time.time() - t:.1f}s")
        return self.name

    def __exit__(self, *exc) -> None:
        if not self.cfg.keep_databases:
            drop_db(self.cfg.urls, self.name)


def _workload(cfg: Config, db: str) -> Workload:
    return Workload(
        dsn=cfg.urls.conninfo(db),
        mappings=cfg.spec.mappings,
        writers=cfg.writers,
        batch_rows=cfg.batch_rows,
        writes_per_second=cfg.writes_per_second,
    )


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


def run_upgrade(cfg: Config, db: str, *args: str, direction: str = "upgrade") -> MigrationRun:
    """Run alembic to completion with in-database sampling and resource deltas."""
    tables = TARGET_TABLES
    sampler = Sampler(cfg.urls.conninfo(db), tables)
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
    )
    run.cleanup()
    return result


def _build_summary(builds: dict) -> dict:
    large = {k: v for k, v in builds.items() if k in BIG_INDEXES}
    return {
        "observed_indexes": len(builds),
        "unobserved_note": "indexes built between two 0.1 s samples are not listed; this is a sampling limit, not a zero",
        "large_table_indexes": large,
    }


def _run_view(r: MigrationRun) -> dict:
    return {
        "exit_code": r.rc,
        "wall_seconds": r.seconds,
        "resources_delta": r.resources,
        "peak_database_bytes": r.peak_db_bytes,
        "lock_modes_seen_on_target_tables": r.locks,
        "builds": _build_summary(r.builds),
        "output_tail": r.output_tail if r.rc != 0 else "",
    }


def s1_upgrade_under_ingest_and_s5_downgrade(cfg: Config) -> dict:
    """S1 (upgrade under paced ingest, with a no-migration baseline window), S5 (downgrade, re-upgrade), S7, S8."""
    out: dict = {}
    with Clone(cfg, "s1") as db:
        work = _workload(cfg, db)
        work.start()
        say(f"S1 warm-up {cfg.warmup_seconds:.0f}s (baseline ingest, no migration)")
        base_start = time.time()
        time.sleep(cfg.warmup_seconds)
        base_end = time.time()
        out["baseline_ingest"] = work.window(base_start, base_end)
        out["parent_delete_before"] = parent_delete_probe(cfg, db)

        say("S1 alembic upgrade head under ingest")
        up = run_upgrade(cfg, db)
        time.sleep(cfg.tail_seconds)
        tail_end = time.time()
        out["upgrade_under_ingest"] = {
            **_run_view(up),
            "ingest_during": work.window(up.started, up.ended),
            "ingest_after": work.window(up.ended, tail_end),
        }
        inv_up = integrity(cfg.urls, db)
        out["parent_delete_after"] = parent_delete_probe(cfg, db)
        out["sizes_after_upgrade"] = index_sizes(cfg.urls, db)

        say("S5 alembic downgrade -1 under ingest")
        down = run_upgrade(cfg, db, direction="downgrade")
        time.sleep(cfg.tail_seconds / 2)
        inv_down = integrity(cfg.urls, db)
        out["downgrade_under_ingest"] = {
            **_run_view(down),
            "ingest_during": work.window(down.started, down.ended),
            "note": "ACCESS EXCLUSIVE is held from lock acquisition to commit; writers queue behind it for that interval",
        }
        say("S5 re-upgrade under ingest")
        re_up = run_upgrade(cfg, db)
        work.stop()
        out["reupgrade_under_ingest"] = {**_run_view(re_up), "ingest_during": work.window(re_up.started, re_up.ended)}
        inv_final = integrity(cfg.urls, db)
        check = AlembicRun(cfg.urls, db, "check")
        check_rc = check.wait(900)
        check.cleanup()
        with connect(cfg.urls, db) as conn:
            readings = data.count(conn, "telemetry_reading")
        out["rows_after_run"] = {"telemetry_reading": readings, "ingested_during_run": readings - cfg.spec.rows}
    out["invariants"] = {
        "upgrade_exit_zero": up.rc == 0,
        "all_22_valid_and_ready_after_upgrade": inv_up["valid_and_ready"] == 22 and inv_up["version"] == HEAD,
        "no_invalid_indexes_after_upgrade": inv_up["invalid_anywhere"] == 0,
        "downgrade_exit_zero": down.rc == 0,
        "downgrade_atomic": inv_down["version"] == PREVIOUS and inv_down["present"] == 0,
        "reupgrade_exit_zero": re_up.rc == 0,
        "all_22_valid_and_ready_after_reupgrade": inv_final["valid_and_ready"] == 22 and inv_final["version"] == HEAD,
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
    with Clone(cfg, "s2") as db:
        up = run_upgrade(cfg, db)
        inv = integrity(cfg.urls, db)
        sizes = index_sizes(cfg.urls, db)
    return {
        "upgrade_idle": _run_view(up),
        "sizes": sizes,
        "invariants": {
            "upgrade_exit_zero": up.rc == 0,
            "all_22_valid_and_ready": inv["valid_and_ready"] == 22 and inv["version"] == HEAD,
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
            "SELECT p.pid, c.relname, p.phase, p.blocks_total, p.blocks_done, p.lockers_total, p.lockers_done "
            "FROM pg_stat_progress_create_index p LEFT JOIN pg_class c ON c.oid = p.index_relid"
        ).fetchall()


def s3_old_snapshot_blocker(cfg: Config) -> dict:
    """A long-lived transaction with an old snapshot: the build must wait for it, ingest must not."""
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
        run.cleanup()
    return {
        "phases_seen_on_first_index": sorted(phases_seen),
        "held_seconds": round(hold_end - hold_start, 1),
        "migration_still_waiting_at_end_of_hold": still_waiting,
        "ingest_while_build_waited": ingest_hold,
        "seconds_from_release_to_finish": round(finished - released, 1),
        "exit_code": rc,
        "output_tail": output if rc != 0 else "",
        "invariants": {
            "build_waited_for_old_snapshot": still_waiting,
            "ingest_not_blocked_while_waiting": ingest_hold["write"]["failed"] == 0 and ingest_hold["write"]["count"] > 0,
            "upgrade_completed_after_release": rc == 0 and inv["valid_and_ready"] == 22 and inv["version"] == HEAD,
        },
    }


def s4_cancellation_and_recovery(cfg: Config, variants=("cancel", "terminate", "kill_client")) -> dict:
    """Interrupt the large-table build three ways, then prove a plain re-run recovers."""
    out: dict = {}
    invariants: dict = {}
    for variant in variants:
        with Clone(cfg, f"s4{variant[:3]}") as db:
            run = AlembicRun(cfg.urls, db, "upgrade", "head")
            try:

                def _building() -> bool:
                    rows = [r for r in _progress(cfg, db) if r[1] == FIRST_INDEX and r[2].startswith("building index")]
                    return bool(rows) and rows[0][3] > 0 and rows[0][4] >= rows[0][3] * 0.25

                _wait_for(_building, run, "the first large build to pass 25% of its table scan", cfg.step_timeout)
                with connect(cfg.urls, db) as conn:
                    pid = next(r[0] for r in _progress(cfg, db) if r[1] == FIRST_INDEX)
                    interrupted_at = time.time()
                    if variant == "cancel":
                        conn.execute("SELECT pg_cancel_backend(%s)", (pid,))
                    elif variant == "terminate":
                        conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
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
            final = integrity(cfg.urls, db)
        out[variant] = {
            "exit_code_of_interrupted_run": interrupted_rc,
            "state_after_interrupt": after_interrupt,
            "locks_left_on_target_tables": leftover_locks,
            "rerun": _run_view(rerun),
            "state_after_rerun": final,
            "seconds_from_interrupt_to_recovered": round(rerun.ended - interrupted_at, 1),
        }
        invariants[f"{variant}__version_not_advanced_by_interrupted_run"] = after_interrupt["version"] == PREVIOUS
        invariants[f"{variant}__no_locks_left"] = leftover_locks == 0
        invariants[f"{variant}__advisory_lock_released"] = after_interrupt["advisory_locks_held"] == 0
        if variant != "kill_client":
            invariants[f"{variant}__interrupted_run_failed"] = interrupted_rc != 0
            invariants[f"{variant}__invalid_artifact_left_behind"] = FIRST_INDEX in after_interrupt["invalid_named"]
        invariants[f"{variant}__rerun_recovers_all_22"] = (
            rerun.rc == 0 and final["valid_and_ready"] == 22 and final["version"] == HEAD
        )
        invariants[f"{variant}__no_invalid_left_after_rerun"] = final["invalid_anywhere"] == 0
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
