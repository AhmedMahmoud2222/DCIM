# ruff: noqa: E501
"""Benchmark driver for migration 0057 (FK indexes). SYNTHETIC data; DISPOSABLE PostgreSQL clusters only; never production.

    TEST_ADMIN_DATABASE_URL=postgresql+asyncpg://fkbench_admin:...@host:5432/<scratch db> \\
    DATABASE_URL=postgresql+asyncpg://dcim_app:...@host:5432/<scratch db> \\
    JWT_SECRET_KEY=... CREDENTIAL_ENCRYPTION_KEY=... REDIS_URL=redis://localhost:6379/1 \\
        python -m scripts.fk_index_benchmark --rows 1000000 --mode fk --ingest-mode service \\
            --allow-create-databases --authorize-cluster <system_identifier>

It refuses to run unless the target is provably a scratch cluster (see safety.py and provision_cluster.sql): the cluster must
carry the operator's `cluster_name=fkbench-disposable*` start-up marker, its system identifier must be passed explicitly,
database creation must be authorized, and it must contain no unrecognised database. It runs without superuser.

Dataset modes: `raw` (FK triggers off, results comparable with the first recorded runs) and `fk` (real parents, FK enforced,
loaded and ingested as the application role). Ingest modes: `raw`, `fk` (SQL) and `service` (the application's ingest_reading).
Results go to --out (default `benchmark-results/`, git-ignored) as JSON and Markdown.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import psycopg

from . import data, report
from .harness import BACKEND_DIR, PREVIOUS, Urls, alembic_ok, connect, create_db, drop_db
from .safety import OWNER_TAG, Authorization, UnsafeTarget, verify_target
from .scenarios import SCENARIOS, Config, Thresholds, evaluate_thresholds, say


def template_owner(spec: data.DatasetSpec) -> str:
    return f"template:{spec.fingerprint}"


def ensure_template(urls: Urls, spec: data.DatasetSpec, name: str, rebuild: bool) -> dict:
    """Reuse the template this tool built for exactly this dataset, or (re)build it. Never touches a database it does not own."""
    owner = template_owner(spec)
    with connect(urls, "postgres") as conn:
        row = conn.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is not None and row[0] != f"{OWNER_TAG}{owner}":
        raise UnsafeTarget(f"database {name!r} exists but is not a template this tool built for this dataset (tag {row[0]!r}); refusing to reuse or drop it.")
    if row is not None and not rebuild:
        with connect(urls, name, app_role=spec.mode == "fk") as conn:
            have = data.count(conn, "telemetry_reading")
            lineage = data.count(conn, "daily_telemetry_aggregate_lineage")
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if lineage == spec.lineage_rows and version == PREVIOUS and have >= spec.rows * (1 - 2 * spec.profile.churn_fraction):
            say(f"reusing template {name} ({have:,} rows at {version})")
            return {"reused": True}
        say(f"template {name} does not match (rows={have:,}, version={version}); rebuilding")
    if row is not None:
        drop_db(urls, name, owner=owner)
    started = time.time()
    create_db(urls, name, owner=owner)
    try:
        alembic_ok(urls, name, "upgrade", PREVIOUS).cleanup()
        with connect(urls, name, app_role=spec.mode == "fk") as conn:
            counts = data.seed(conn, spec)
    except BaseException:
        drop_db(urls, name, owner=owner)  # never leave a half-loaded template that a later run could mistake for a good one
        raise
    seconds = round(time.time() - started, 1)
    say(f"template {name} ready in {seconds:,.0f}s: {counts}")
    return {"reused": False, "seconds": seconds, "counts": counts}


KEY_METRICS = {
    "s1_s5": {
        "upgrade_wall_s": ("upgrade_under_ingest", "wall_seconds"),
        "ingest_p99_ms_baseline": ("baseline_ingest", "write", "p99_ms"),
        "ingest_p99_ms_during_upgrade": ("upgrade_under_ingest", "ingest_during", "write", "p99_ms"),
        "ingest_rows_per_s_during_upgrade": ("upgrade_under_ingest", "ingest_during", "write", "rows_per_second"),
        "downgrade_wall_s": ("downgrade_under_ingest", "wall_seconds"),
        "reupgrade_wall_s": ("reupgrade_under_ingest", "wall_seconds"),
    },
    "s2": {"idle_upgrade_wall_s": ("upgrade_idle", "wall_seconds"), "idle_upgrade_wal_bytes": ("upgrade_idle", "resources_delta", "wal_bytes")},
    "s3": {"seconds_from_release_to_finish": ("seconds_from_release_to_finish",)},
}


def _dig(obj, path):
    for key in path:
        obj = obj[key]
    return obj


def aggregate(name: str, runs: list[dict]) -> dict:
    """Last run's detail, plus per-metric min/median/max across repeats and invariants that must hold in EVERY run."""
    merged = dict(runs[-1])
    merged["invariants"] = {k: all(r["invariants"][k] for r in runs) for k in runs[-1]["invariants"]}
    merged["repeats"] = len(runs)
    metrics = {}
    for label, path in KEY_METRICS.get(name, {}).items():
        values = [v for v in (_dig(r, path) for r in runs) if v is not None]
        if values:
            metrics[label] = {"values": values, "min": min(values), "median": statistics.median(values), "max": max(values)}
    merged["repeat_metrics"] = metrics
    return merged


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.fk_index_benchmark", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    safety = p.add_argument_group("safety (all required)")
    safety.add_argument("--allow-create-databases", action="store_true", help="authorize this run to create and drop fkbench_* databases")
    safety.add_argument("--authorize-cluster", metavar="SYSTEM_IDENTIFIER", help="pg_control_system().system_identifier of the scratch cluster")
    safety.add_argument("--allow-superuser", action="store_true", help="permit a superuser admin role (recorded in the report); default is to refuse")
    p.add_argument("--rows", type=int, default=1_000_000, help="telemetry_reading rows")
    p.add_argument("--lineage-ratio", type=float, default=0.1, help="lineage rows as a fraction of --rows (assumption)")
    p.add_argument("--mode", choices=data.MODES, default="raw", help="dataset: raw = FK triggers off; fk = real parents, FK enforced, application role")
    p.add_argument("--ingest-mode", choices=("raw", "fk", "service"), default=None, help="default: raw for --mode raw, fk for --mode fk")
    p.add_argument("--profile", type=Path, default=None, help="JSON distribution/retention/churn profile (see data.Profile); default uniform")
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    p.add_argument("--repeats", type=int, default=1, help="repeat each scenario N times; report min/median/max")
    p.add_argument("--out", type=Path, default=BACKEND_DIR / "benchmark-results")
    p.add_argument("--writers", type=int, default=2, help="concurrent ingest writers")
    p.add_argument("--readers", type=int, default=1)
    p.add_argument("--batch-rows", type=int, default=10)
    p.add_argument("--writes-per-second", type=float, default=10.0, help="per writer (offered rows/s = writers x this x batch)")
    p.add_argument("--warmup-seconds", type=float, default=30.0)
    p.add_argument("--tail-seconds", type=float, default=30.0)
    p.add_argument("--hold-seconds", type=float, default=45.0, help="S3: how long the old snapshot is held")
    thr = p.add_argument_group("provisional acceptance thresholds (operator-supplied; a breach is a flag, not a verdict)")
    thr.add_argument("--max-write-failures", type=int, default=0)
    thr.add_argument("--max-ingest-p99-ratio", type=float, default=3.0)
    thr.add_argument("--max-ingest-p99-ms", type=float, default=None)
    thr.add_argument("--max-migration-seconds", type=float, default=None)
    thr.add_argument("--max-wal-mib", type=float, default=None)
    thr.add_argument("--min-free-disk-gib", type=float, default=None)
    p.add_argument("--rebuild-template", action="store_true")
    p.add_argument("--keep-databases", action="store_true", help="do not drop scenario clones (debugging; they are large)")
    args = p.parse_args(argv)

    if args.repeats < 1 or args.writers < 1:
        p.error("--repeats and --writers must be >= 1")
    ingest_mode = args.ingest_mode or ("raw" if args.mode == "raw" else "fk")
    if args.mode == "fk" and ingest_mode == "raw":
        p.error("--ingest-mode raw needs --mode raw (replica-role ingest against an FK dataset is not a supported combination)")
    if args.mode == "raw" and ingest_mode == "service":
        p.error("--ingest-mode service needs --mode fk (it requires real parent rows and a valid conversion contract)")

    urls = Urls.from_env()
    auth = Authorization(args.authorize_cluster, args.allow_create_databases, args.allow_superuser)
    try:
        target = verify_target(urls.conninfo("postgres"), urls.admin, urls.app, auth)
    except UnsafeTarget as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
    say(f"target verified: cluster_name={target['cluster_name']} system_identifier={target['system_identifier']} superuser={target['admin_is_superuser']}")

    profile = data.Profile.from_json(args.profile) if args.profile else data.Profile()
    spec = data.DatasetSpec.for_rows(args.rows, args.lineage_ratio, mode=args.mode, profile=profile)
    template = f"fkbench_tpl_{args.rows}_{spec.fingerprint}"
    try:
        template_info = ensure_template(urls, spec, template, args.rebuild_template)
        cfg = Config(
            urls=urls, spec=spec, template=template, ingest_mode=ingest_mode, writers=args.writers, readers=args.readers,
            batch_rows=args.batch_rows, writes_per_second=args.writes_per_second, warmup_seconds=args.warmup_seconds,
            tail_seconds=args.tail_seconds, hold_seconds=args.hold_seconds, keep_databases=args.keep_databases,
            thresholds=Thresholds(
                args.max_write_failures, args.max_ingest_p99_ratio, args.max_ingest_p99_ms, args.max_migration_seconds,
                args.max_wal_mib, args.min_free_disk_gib,
            ),
        )
        with connect(urls, template) as conn:
            env = report.fingerprint(conn)
        env["target_verification"] = target
        results: dict = {}
        breaches: list[dict] = []
        for name in args.scenarios:
            runs = []
            for i in range(args.repeats):
                say(f"=== scenario {name} (run {i + 1}/{args.repeats}) ===")
                started = time.time()
                run = SCENARIOS[name](cfg)
                run["scenario_wall_seconds"] = round(time.time() - started, 1)
                runs.append(run)
                breaches += [{**b, "run": i + 1} for b in evaluate_thresholds({name: run}, cfg.thresholds)]
            results[name] = aggregate(name, runs)
    except UnsafeTarget as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(f"[bench] database error: {exc}", file=sys.stderr)
        return 3
    full = {
        "classification": report.CLASSIFICATION,
        "classification_reason": report.CLASSIFICATION_REASON,
        "dataset": {
            "rows": spec.rows, "lineage_rows": spec.lineage_rows, "mappings": spec.mappings, "mode": spec.mode,
            "profile": vars(profile), "template": template_info,
        },
        "environment": env,
        "workload": f"mode={ingest_mode}: {cfg.writers} writers x {cfg.writes_per_second:g} txn/s x {cfg.batch_rows} rows = "
        f"{cfg.writers * cfg.writes_per_second * cfg.batch_rows:g} rows/s offered, {cfg.readers} reader(s) x 10 reads/s; repeats={args.repeats}",
        "scenarios": results,
        "threshold_breaches": breaches,
        "thresholds_note": "PROVISIONAL, operator-supplied defaults. A breach is a flag for investigation; no breach is not an acceptance.",
        "limitations": list(report.limitations(spec.mode, ingest_mode)),
    }
    json_path, md_path = report.write(args.out, full)
    failed = [f"{s}:{k}" for s, r in results.items() for k, ok in r.get("invariants", {}).items() if not ok]
    say(f"report: {md_path}")
    if full["threshold_breaches"]:
        say(f"{len(full['threshold_breaches'])} PROVISIONAL threshold breach(es); see the report")
    if failed:
        say("INVARIANT FAILURES: " + ", ".join(failed))
        return 1
    say(f"all invariants held; classification remains: {report.CLASSIFICATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
