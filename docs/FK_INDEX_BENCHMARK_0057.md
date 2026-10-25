# Migration 0057 -- benchmark framework and rollout-readiness report

**Rollout classification: NOT YET VALIDATED.** This framework measures migration `0057_fk_indexes` on a synthetic table on
one machine. It can show that the migration's invariants hold at volume and give numbers to compare; it cannot show that a
production rollout is safe. Read it together with the operator runbook, `docs/FK_INDEX_ROLLOUT_0057.md`.

The migration itself is unchanged and is exercised exactly as an operator runs it (`python -m alembic upgrade head` in a
subprocess). The framework never imports, patches or replaces migration code.

## Running it

Scratch PostgreSQL 16 only; the admin role must be a superuser (it clones databases and loads data with
`session_replication_role = replica`). Never point it at production.

```
TEST_ADMIN_DATABASE_URL=postgresql+asyncpg://postgres:...@host:5432/<scratch db> \
DATABASE_URL=postgresql+asyncpg://dcim_app:...@host:5432/<scratch db> \
JWT_SECRET_KEY=... CREDENTIAL_ENCRYPTION_KEY=... REDIS_URL=redis://localhost:6379/1 \
BENCH_HOST_NOTES="what this machine and storage really are" \
    python -m scripts.fk_index_benchmark --rows 1000000   # or --rows 10000000
```

The first run builds a populated 0056 template (`fkbench_tpl_<rows>`); later runs reuse it. Every scenario runs on a fresh
`CREATE DATABASE ... TEMPLATE ... STRATEGY FILE_COPY` clone and drops it afterwards (`--keep-databases` to inspect). Output is
`backend/benchmark-results/fk_index_benchmark_<rows>_<timestamp>.{json,md}` (git-ignored). The exit code is non-zero if any
invariant fails.

Disk: plan for the template plus one clone plus the new indexes. At 10M rows expect several GiB each; check free space first.

## Dataset

Deterministic and seeded by row number (same arguments, same table). `telemetry_reading` rows keep a realistic shape: eight
collectors, twenty integrations, 5,000 series, 90 days of timestamps, 4 of every 5 rows pinned to a mapping revision (the
rest NULL, as the revision/evidence check requires), and the table's four pre-existing indexes in place so the build competes
with real write amplification. `daily_telemetry_aggregate_lineage` holds 10% as many rows (an assumption, `--lineage-ratio`).
2,000 real `integration_metric_mapping` rows (plus revisions, plus one unreferenced "orphan") are the parents of the two
columns being indexed. The other 19 FK tables are empty.

## Scenarios

| ID | What | Measured |
| --- | --- | --- |
| S1 | `alembic upgrade head` under paced concurrent ingest, with a no-migration baseline window first | Wall time, per-index observed build and phase seconds, ingest p50/p95/p99/max and failures before, during and after, read-probe latency, lock modes seen, WAL, temp, growth, peak size |
| S2 / S6 | The same upgrade with no concurrent load | WAL, temp bytes, database growth, peak size, final index sizes and table sizes attributable to the build alone |
| S3 | An old-snapshot transaction held open during the upgrade | The build waits (`waiting for old snapshots`); ingest keeps working; the upgrade completes after release |
| S4 | The first large build interrupted by `pg_cancel_backend`, `pg_terminate_backend`, and a killed client, each after 25% of the table scan | Exit code, `alembic_version` unchanged, INVALID artifact left, no locks left, then a plain re-run: time to recover, 22/22 valid, nothing INVALID |
| S5 | `alembic downgrade -1` then re-upgrade, both under ingest | Wall time and ingest latency during the `ACCESS EXCLUSIVE` window; atomicity (version and indexes change together) |
| S7 | Delete of an unreferenced `integration_metric_mapping`, rolled back, `EXPLAIN (ANALYZE)` before and after | The referential-check time the new indexes exist to remove |
| S8 | After S1/S5 | 22/22 valid and ready, no INVALID index anywhere, `alembic check` clean, advisory lock released |

Ingest is open-loop: each operation's latency is measured from when it was scheduled, so a stall cannot hide itself by
delaying the next request. Everything else is sampled from PostgreSQL's own views at 0.1 s: `pg_stat_progress_create_index`,
`pg_locks` on the target tables, WAL position, `pg_stat_database.temp_bytes`, `pg_stat_io`, database size.

## What counts as a result

* **Invariants** are yes/no and fail the run: all 22 indexes valid and ready, no INVALID leftovers, version stamped only on
  success, downgrade atomic, an interrupted run leaves 0056 and a recoverable INVALID artifact and releases its locks, a plain
  re-run recovers, `alembic check` clean, no failed ingest writes.
* **Measurements** (times, latencies, bytes) are reported with the environment fingerprint and are not judged. Compare them
  across runs on the same machine; do not read them as production numbers.

## Interpreting disk and WAL

Take the WAL, temp and growth figures from S2 (no concurrent writers). Peak database size minus starting size is the headroom
the build needed on this table at this scale; scale it to your row count, add your own safety margin, and confirm on a restored
copy. The 1M and 10M runs let you check whether the relation is linear for this data; they do not make production's index
sizes knowable.

## Known limitations

See the Limitations section the report prints. In short: synthetic uniform data; one machine and instance, no replicas;
ingest writers bypass foreign-key triggers; builds shorter than 0.1 s are not individually timed; host CPU is not sampled; only
two tables are populated; the offered load is a fixed rate, not production's.

## CI

`backend/tests/integration/test_fk_index_benchmark_harness.py` checks the harness at a few thousand rows with no timing
assertions: generator determinism, workload bookkeeping, an instrumented upgrade and its rendering, and (deterministically,
using an open writer transaction as the barrier rather than a sleep) cancel and terminate of a build followed by recovery.
The 1M and 10M runs are manual and are not part of CI.

## Recorded runs

Raw reports (JSON plus rendered Markdown) from the first two executions are in `docs/benchmark-results/0057/`, 1,000,000 and
10,000,000 `telemetry_reading` rows, on a Windows 11 laptop under Docker Desktop/WSL2 (8 CPUs, about 8 GiB) with PostgreSQL 16.15
in a named Docker volume. All invariants held in both runs. They are development-machine measurements of synthetic data and do
not change the classification: **NOT YET VALIDATED**.
