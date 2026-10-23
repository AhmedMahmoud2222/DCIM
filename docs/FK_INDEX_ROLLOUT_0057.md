# Migration 0057 -- foreign-key indexes: rollout runbook and outstanding validation

Migration: `backend/migrations/versions/0057_fk_indexes.py` (22 single-column btree indexes, `CREATE INDEX CONCURRENTLY`).

## Status of validation

| Item | Status |
| --- | --- |
| Fresh and populated upgrade, downgrade/upgrade, `alembic check`, single head | Automated (`tests/integration/test_fk_indexes_migration.py`, `tests/unit/test_alembic_single_head.py`) |
| Collision / interrupted-run handling | Automated (same module) |
| **Build time, ingest latency, CPU/I/O, WAL, disk headroom and index size on representative `telemetry_reading` data under concurrent ingestion** | **OUTSTANDING.** Not run. The local and CI databases hold no representative telemetry volume. Do not assume the build is cheap on a large table. |

Before running on a production-sized database, measure on a restored copy or staging with realistic `telemetry_reading`
volume and live ingestion: wall-clock build time per index, ingest p95/p99 latency during the build, CPU and I/O,
WAL generated, free disk (each build needs space for the new index plus sort/temp files), and final index size.

## What a concurrent build does and does not do

`CREATE INDEX CONCURRENTLY` lets normal INSERT/UPDATE/DELETE continue, but it is not free: it waits for transactions
holding older snapshots or conflicting locks (a long-running transaction delays it), takes SHARE UPDATE EXCLUSIVE (it
conflicts with schema changes, VACUUM and other index builds on that table), scans the table twice, and adds CPU, I/O
and WAL load. A failed or cancelled build leaves an INVALID index behind. See the PostgreSQL 16 documentation:
<https://www.postgresql.org/docs/16/sql-createindex.html>.

## Operator runbook

1. **Preconditions.** Backup/PITR verified; free disk comfortably above the largest expected index; no long-running
   transactions (`SELECT pid, now() - xact_start FROM pg_stat_activity WHERE xact_start IS NOT NULL ORDER BY 2 DESC`);
   no running maintenance on the target tables. Prefer a low-traffic window.
2. **Timeouts.** Set a `lock_timeout` for the migration role so a build does not queue indefinitely behind a lock:
   `ALTER ROLE dcim_app SET lock_timeout = '30s';` before, `ALTER ROLE dcim_app RESET lock_timeout;` after. Do not set a
   short `statement_timeout`: the index builds legitimately run long. (A lock timeout fails the run safely; see step 5.)
3. **Run** `alembic upgrade head` (online only; `--sql` is rejected).
4. **Monitor progress.** `SELECT * FROM pg_stat_progress_create_index;` and `pg_stat_activity` for the build backend; watch
   replication lag and WAL volume if you run replicas.
5. **Cancel/retry.** To stop, `SELECT pg_cancel_backend(<pid>)`. The migration aborts, `alembic_version` stays at 0056, and
   indexes already finished stay in place. Re-run `alembic upgrade head`: valid indexes are reused, an INVALID artifact of
   this migration is dropped and rebuilt, and an unrelated object that already uses one of the index names aborts the run
   with an explicit message (it is never dropped for you).
6. **Rollback.** `alembic downgrade -1` drops only indexes that match this migration's definition; a same-name object that
   is not ours is left untouched and logged.
7. **Verify.** All 22 indexes valid and ready, no INVALID leftovers, then `alembic check`:
   `SELECT c.relname, i.indisvalid, i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
   WHERE c.relname = ANY(ARRAY[<the 22 ix_<table>_<column> names in FK_INDEXES>]) AND NOT (i.indisvalid AND i.indisready);`
   must return no rows.
