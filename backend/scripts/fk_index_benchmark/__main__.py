# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""Benchmark driver for migration 0057 (FK indexes). SYNTHETIC data; scratch PostgreSQL only; never run against production.

    TEST_ADMIN_DATABASE_URL=postgresql+asyncpg://postgres:...@host:5432/dcim_bench \\
    DATABASE_URL=postgresql+asyncpg://dcim_app:...@host:5432/dcim_bench \\
    JWT_SECRET_KEY=... CREDENTIAL_ENCRYPTION_KEY=... REDIS_URL=redis://localhost:6379/1 \\
        python -m scripts.fk_index_benchmark --rows 1000000 --scenarios s2 s1_s5 s3 s4

The admin role must be a superuser (database cloning, replica-role loading). The first run builds a populated 0056 template
database (`fkbench_tpl_<rows>`) and reuses it afterwards; every scenario runs on a fresh file-copy clone of it.
Results go to --out (default `benchmark-results/`, git-ignored) as JSON and Markdown.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import data, report
from .harness import BACKEND_DIR, PREVIOUS, Urls, alembic_ok, connect, create_db, db_exists, drop_db
from .scenarios import SCENARIOS, Config, say


def ensure_template(urls: Urls, spec: data.DatasetSpec, name: str, rebuild: bool) -> None:
    if db_exists(urls, name) and not rebuild:
        with connect(urls, name) as conn:
            have = data.count(conn, "telemetry_reading")
            lineage = data.count(conn, "daily_telemetry_aggregate_lineage")
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if have == spec.rows and lineage == spec.lineage_rows and version == PREVIOUS:
            say(f"reusing template {name} ({have:,} rows at {version})")
            return
        say(f"template {name} does not match (rows={have:,}, version={version}); rebuilding")
    drop_db(urls, name)
    create_db(urls, name)
    t = time.time()
    alembic_ok(urls, name, "upgrade", PREVIOUS).cleanup()
    try:
        with connect(urls, name) as conn:
            counts = data.seed(conn, spec)
    except BaseException:
        drop_db(urls, name)  # never leave a half-loaded template that a later run could mistake for a good one
        raise
    say(f"template {name} ready in {time.time() - t:,.0f}s: {counts}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m scripts.fk_index_benchmark", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--rows", type=int, default=1_000_000, help="telemetry_reading rows (default 1,000,000; 10,000,000 for the large scale)"
    )
    p.add_argument(
        "--lineage-ratio", type=float, default=0.1, help="lineage rows as a fraction of --rows (assumption, default 0.1)"
    )
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    p.add_argument("--out", type=Path, default=BACKEND_DIR / "benchmark-results")
    p.add_argument("--writers", type=int, default=2)
    p.add_argument("--batch-rows", type=int, default=10)
    p.add_argument("--writes-per-second", type=float, default=10.0, help="per writer (offered rows/s = writers x this x batch)")
    p.add_argument("--warmup-seconds", type=float, default=30.0)
    p.add_argument("--tail-seconds", type=float, default=30.0)
    p.add_argument("--hold-seconds", type=float, default=45.0, help="S3: how long the old snapshot is held")
    p.add_argument("--rebuild-template", action="store_true")
    p.add_argument("--keep-databases", action="store_true", help="do not drop scenario clones (debugging; they are large)")
    args = p.parse_args(argv)

    urls = Urls.from_env()
    spec = data.DatasetSpec.for_rows(args.rows, args.lineage_ratio)
    template = f"fkbench_tpl_{args.rows}"
    ensure_template(urls, spec, template, args.rebuild_template)
    cfg = Config(
        urls=urls,
        spec=spec,
        template=template,
        writers=args.writers,
        batch_rows=args.batch_rows,
        writes_per_second=args.writes_per_second,
        warmup_seconds=args.warmup_seconds,
        tail_seconds=args.tail_seconds,
        hold_seconds=args.hold_seconds,
        keep_databases=args.keep_databases,
    )
    with connect(urls, template) as conn:
        env = report.fingerprint(conn)
    results: dict = {}
    for name in args.scenarios:
        say(f"=== scenario {name} ===")
        started = time.time()
        results[name] = SCENARIOS[name](cfg)
        results[name]["scenario_wall_seconds"] = round(time.time() - started, 1)
    full = {
        "classification": report.CLASSIFICATION,
        "classification_reason": report.CLASSIFICATION_REASON,
        "dataset": {"rows": spec.rows, "lineage_rows": spec.lineage_rows, "mappings": spec.mappings},
        "environment": env,
        "workload": f"{cfg.writers} writers x {cfg.writes_per_second:g} txn/s x {cfg.batch_rows} rows = "
        f"{cfg.writers * cfg.writes_per_second * cfg.batch_rows:g} rows/s offered, 1 reader x 10 reads/s",
        "scenarios": results,
        "limitations": list(report.LIMITATIONS),
    }
    json_path, md_path = report.write(args.out, full)
    failed = [f"{s}:{k}" for s, r in results.items() for k, ok in r.get("invariants", {}).items() if not ok]
    say(f"report: {md_path}")
    if failed:
        say("INVARIANT FAILURES: " + ", ".join(failed))
        return 1
    say(f"all invariants held; classification remains: {report.CLASSIFICATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
