"""Populated lock rehearsal for migration 0048 (Issue #128 / G4-A). SYNTHETIC data; run against a scratch database only.

    TEST_ADMIN_DATABASE_URL=postgresql+asyncpg://postgres:...@localhost:5432/dcim_test \\
        PYTHONPATH=. python scripts/aggregate_migration_rehearsal.py --rows 2000000

The database must already be at 0048. The script downgrades 0048 (the aggregate table is emptied first), fills
daily_telemetry_aggregate with --rows synthetic rows, then runs the real upgrade the way migrations/env.py does
(inside begin_transaction(), so autocommit_block() commits) while a second connection, with lock_timeout 500 ms,
continuously reads and updates the table and samples the lock modes other backends hold on it. It prints per-phase
timings, the worst read and write latency in each phase, how many probes timed out, and the lock modes seen.

Everything it reports is a measurement of this machine and this synthetic table, not of any production installation.
"""

import argparse
import importlib.util
import os
import threading
import time
from pathlib import Path

import psycopg
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/versions/0048_daily_aggregate_exact_sum.py"
TABLE = "daily_telemetry_aggregate"


def _load():
    spec = importlib.util.spec_from_file_location("rehearsal_0048", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Phases:
    """Wraps alembic's `op` to timestamp each statement group."""

    def __init__(self, real_op, marks):
        self._real, self._marks = real_op, marks

    def execute(self, statement, *args, **kwargs):
        sql = " ".join(str(statement).split())
        if "ADD COLUMN" in sql:
            label = "add_column"
        elif "ADD CONSTRAINT" in sql:
            label = "add_constraint_not_valid"
        elif "VALIDATE CONSTRAINT" in sql:
            label = "validate:" + sql.rsplit(" ", 1)[-1][-32:]
        else:
            label = "other"
        started = time.perf_counter()
        result = self._real.execute(statement, *args, **kwargs)
        self._marks.append((label, started, time.perf_counter()))
        return result

    def get_context(self):
        return self._real.get_context()

    def __getattr__(self, name):
        return getattr(self._real, name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=2_000_000)
    args = parser.parse_args()
    dsn = os.environ["TEST_ADMIN_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg://")
    plain = dsn.replace("postgresql+psycopg://", "postgresql://")
    engine = create_engine(dsn)
    module = _load()

    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {TABLE}"))
        conn.commit()
        context = MigrationContext.configure(conn)
        with Operations.context(context), context.begin_transaction():
            module.downgrade()
        conn.commit()
        integration = conn.execute(text("SELECT id FROM integration LIMIT 1")).scalar()
        if integration is None:
            conn.execute(text("""INSERT INTO integration (id, name, integration_type, enabled, target_host, config,
                poll_interval_seconds, consecutive_failures, version)
                VALUES (gen_random_uuid(), 'rehearsal', 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"""))
            integration = conn.execute(text("SELECT id FROM integration LIMIT 1")).scalar()
        conn.commit()
        started = time.perf_counter()
        conn.execute(text(f"""
            INSERT INTO {TABLE} (id, integration_id, external_identifier, series_key, metric, unit, day, average_value,
                minimum_value, maximum_value, sample_count, registry_version)
            SELECT gen_random_uuid(), :i, 'e', 'registry:1:k-' || (g % 20000), 'power_kw', 'kW',
                   date '2024-01-01' + (g / 20000), 2, 1, 3, 288, '1'
            FROM generate_series(0, :n - 1) g"""), {"i": integration, "n": args.rows})
        conn.commit()
        conn.execute(text(f"ANALYZE {TABLE}"))
        conn.commit()
        size = conn.execute(text(f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE}'))")).scalar()
        print(f"populated {args.rows:,} rows in {time.perf_counter() - started:.1f}s; table + indexes {size}")

    samples: list[tuple[float, str, float, bool]] = []
    modes: list[tuple[float, set[str]]] = []
    stop = threading.Event()

    def probe() -> None:
        with psycopg.connect(plain, autocommit=True) as other:
            other.execute("SET lock_timeout = '500ms'")
            while not stop.is_set():
                for kind, sql in (
                    ("read", f"SELECT count(*) FROM {TABLE} WHERE series_key = 'registry:1:k-1'"),
                    ("write", f"UPDATE {TABLE} SET average_value = average_value WHERE series_key = 'registry:1:k-2' AND day = date '2024-01-01'"),
                ):
                    t0 = time.perf_counter()
                    try:
                        other.execute(sql)
                        ok = True
                    except psycopg.errors.LockNotAvailable:
                        ok = False
                    samples.append((t0, kind, time.perf_counter() - t0, ok))
                held = {row[0] for row in other.execute(
                    f"SELECT mode FROM pg_locks WHERE relation = '{TABLE}'::regclass AND granted AND pid <> pg_backend_pid()")}
                modes.append((time.perf_counter(), held))
                time.sleep(0.01)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    time.sleep(0.5)
    marks: list[tuple[str, float, float]] = []
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        module.op = _Phases(module.op, marks)
        begin = time.perf_counter()
        with Operations.context(context), context.begin_transaction():
            module.upgrade()
        conn.commit()
        total = time.perf_counter() - begin
    time.sleep(0.5)
    stop.set()
    thread.join()

    print(f"upgrade total {total:.2f}s")
    validate_start = min(s for label, s, _ in marks if label.startswith("validate"))
    validate_end = max(e for label, _, e in marks if label.startswith("validate"))
    for label, s, e in marks:
        print(f"  {label:<48} {e - s:8.3f}s")
    for name, lo, hi in (("before validation (add column + NOT VALID constraints)", begin, validate_start),
                         ("during validation", validate_start, validate_end)):
        window = [x for x in samples if lo <= x[0] <= hi]
        for kind in ("read", "write"):
            values = [x for x in window if x[1] == kind]
            if values:
                print(f"  {name}: {kind}s={len(values)} worst={max(v[2] for v in values) * 1000:.0f} ms "
                      f"timeouts={sum(1 for v in values if not v[3])}")
        seen = set().union(*(m for t, m in modes if lo <= t <= hi), set())
        print(f"  {name}: lock modes held by other backends sampled = {sorted(seen)}")
    own = set().union(*(m for t, m in modes if validate_start <= t <= validate_end), set())
    print("  ACCESS EXCLUSIVE held during validation:", "AccessExclusiveLock" in own)


if __name__ == "__main__":
    main()
