# Migration 0057 -- foreign-key indexes: rollout runbook and outstanding validation

Migration: `backend/migrations/versions/0057_fk_indexes.py` (22 single-column btree indexes, `CREATE INDEX CONCURRENTLY`).

## Status of validation

| Item | Status |
| --- | --- |
| Fresh and populated upgrade, downgrade/upgrade, `alembic check`, single head | Automated (`tests/integration/test_fk_indexes_migration.py`, `tests/unit/test_alembic_single_head.py`) |
| Collision / interrupted-run handling, plan-to-action races, downgrade atomicity, advisory-lock lifetime | Automated (same module; deterministic lock barriers, bounded, mutation-checked against the previous revision) |
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

## How the migration protects itself

* **Nothing acts on a stale plan.** Pre-existing same-name objects are validated, then: an index that should simply be
  reused is re-proven at the very end (all 22 re-inspected under `SHARE UPDATE EXCLUSIVE` on the target tables, held until
  `alembic_version` is written and committed); an interrupted artifact is dropped only under `ACCESS EXCLUSIVE` after its
  OID is re-checked. The index objects themselves are also pinned (a rename to a scratch name and straight back, SHARE UPDATE EXCLUSIVE on the index; never a COMMENT, which could overwrite a concurrent comment change), because
  `ALTER INDEX ... RENAME` takes no table lock and could otherwise swap a wrong index into a verified name. A replacement made
  by another session aborts the run and is left untouched; a lock cycle with another session is broken by PostgreSQL's
  deadlock detector and fails the run safely (retry).
* **One runner at a time.** A session advisory lock is taken by the migration and released by `env.py` after the version
  update commits. A second concurrent `alembic upgrade` fails fast with "another migration invocation is active".
* **Downgrade is all-or-nothing** (see step 6): it needs a brief `ACCESS EXCLUSIVE` window on the affected tables.

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
   with an explicit message (it is never dropped for you). A "possibly active build" refusal means another session holds a
   `SHARE UPDATE EXCLUSIVE`-or-stronger lock on the table (e.g. a running `CREATE INDEX CONCURRENTLY`, VACUUM or another
   migration): wait for it, then re-run.
6. **Rollback.** `alembic downgrade -1` is atomic: it locks every affected table in `ACCESS EXCLUSIVE` mode (reads and writes
   on them wait from lock acquisition until commit; the work itself is metadata-only), then removes all matching indexes with
   a single `DROP INDEX a, b, ...` in the same transaction as the version change. It fails as a whole (lock wait bounded by
   `lock_timeout`, 5s if none is set) and leaves 0057 stamped with all 22 indexes intact; retry in a quieter window. Only
   indexes that match this migration's definition are dropped; a same-name object that is not ours is left in place and logged.
   Unlike the upgrade this is deliberately not `CONCURRENTLY`: concurrent drops commit one by one and cannot be made atomic.
7. **Verify.** All 22 indexes valid and ready, no INVALID leftovers, then `alembic check`:
   `SELECT c.relname, i.indisvalid, i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
   WHERE c.relname = ANY(ARRAY[<the 22 ix_<table>_<column> names in FK_INDEXES>]) AND NOT (i.indisvalid AND i.indisready);`
   must return no rows.
