# Migration 0057 -- benchmark framework and rollout-readiness report

**Rollout classification: NOT YET VALIDATED.** This framework measures migration `0057_fk_indexes` on a synthetic table on
one machine. It can show that the migration's invariants hold at volume and give numbers to compare; it cannot show that a
production rollout is safe, and a migration that completes here says nothing about the memory, disk, I/O or CPU headroom a
production system has. Read it together with the operator runbook, `docs/FK_INDEX_ROLLOUT_0057.md`.

The migration itself is unchanged and is exercised exactly as an operator runs it (`python -m alembic upgrade head` in a
subprocess). The framework never imports, patches or replaces migration code.

## Safety: where it will and will not run

The benchmark creates, loads and drops large databases, so it refuses to run anywhere it was not deliberately pointed at. A
database name is not evidence, so none of the checks is a name check. The run proceeds only if **all** hold
(`backend/scripts/fk_index_benchmark/safety.py`):

1. `--allow-create-databases` was passed (explicit authorization to create and drop databases).
2. The cluster was started with `cluster_name=fkbench-disposable-<name>`. That setting exists only in server start-up
   configuration (SQL cannot set it), so an operator had to put it there on purpose.
3. `--authorize-cluster <system_identifier>` equals `pg_control_system()` of the cluster actually connected to, so a stale or
   mistyped connection string cannot reach a different cluster.
4. The server is a primary (not in recovery) running PostgreSQL 16.
5. The admin role is not a superuser (unless `--allow-superuser`, which the report records) and may create databases.
6. Every database in the cluster is a system database, one named in the two connection URLs, or `fkbench_*`. Any other
   database means this is not a scratch cluster and the run is refused.

Ownership: every database the tool creates carries a database comment naming the run (templates: the dataset). The tool only
drops a database whose name starts with `fkbench_` **and** whose tag matches the caller, never reuses or drops a template it did
not build for exactly this dataset. It never signals a backend by PID (see "Cancellation design"). A refused run creates and
drops nothing. The negative tests in `test_fk_index_benchmark_harness.py` exercise each refusal.

Privileges: the tool runs as a non-superuser `CREATEDB` role (`fkbench_admin`) provisioned by `provision_cluster.sql` with only
`pg_monitor`, `pg_signal_backend`, `pg_checkpoint`, membership of the application role (to load and vacuum its tables), `SET` on
`session_replication_role` (raw mode only) and `EXECUTE` on `pg_stat_statements_reset`. The foreign-key-enforced modes never use
`session_replication_role` and load and ingest as the application role.

## Running it

```
# once, on a DISPOSABLE PostgreSQL 16 cluster started with
#   -c cluster_name=fkbench-disposable-1 -c shared_preload_libraries=pg_stat_statements
psql -U postgres -v admin_password="'...'" -f backend/scripts/fk_index_benchmark/provision_cluster.sql

TEST_ADMIN_DATABASE_URL=postgresql+asyncpg://fkbench_admin:...@host:5432/<scratch db> \
DATABASE_URL=postgresql+asyncpg://dcim_app:...@host:5432/<scratch db> \
JWT_SECRET_KEY=... CREDENTIAL_ENCRYPTION_KEY=... REDIS_URL=redis://localhost:6379/1 \
BENCH_HOST_NOTES="what this machine and storage really are" \
    python -m scripts.fk_index_benchmark --rows 1000000 --mode fk --ingest-mode service \
        --allow-create-databases --authorize-cluster <system_identifier> --repeats 3
```

The first run builds a populated 0056 template and reuses it afterwards (its name includes a fingerprint of the dataset spec).
Every scenario runs on a fresh `CREATE DATABASE ... TEMPLATE ... STRATEGY FILE_COPY` clone and drops it afterwards. Output is
`backend/benchmark-results/*.{json,md}` (git-ignored). The exit code is 2 for a refused target, 1 if an invariant failed.

Before a large run check free disk and memory yourself (the report records what it observed, not what you need). Do not
repeat the 10M scenario until you have established headroom: the template, one clone and the new indexes must fit with margin,
and the host must not be under memory pressure.

## Dataset and ingest modes (kept separate)

| Dataset `--mode` | Meaning |
| --- | --- |
| `raw` | Foreign-key triggers off (`session_replication_role = replica`). Parents other than mappings and revisions exist only as UUIDs. The mode of the first recorded 1M and 10M runs. |
| `fk` | Real `collector`, `integration`, `integration_metric_mapping`, revision (with the registry's own conversion hash) and `daily_telemetry_aggregate` rows. Nothing is bypassed. Loaded as the application role. |

| Ingest `--ingest-mode` | Meaning |
| --- | --- |
| `raw` | Raw SQL INSERT as the admin role, FK triggers off. Index-maintenance cost only. |
| `fk` | Raw SQL INSERT as the application role, FKs enforced. Adds FK checks and the permission boundary. |
| `service` | The application's own `ingest_reading()` through an AsyncSession as the application role. The closest this harness gets to real ingestion; still one process, not the HTTP API. |

Results from different modes are not comparable with each other. The recorded 1M and 10M runs are `raw`/`raw`.

`--profile profile.json` sets the shape (`series_count`, `skew`, `pinned_share`, `retention_days`, `churn_fraction`,
`integrations`, `collectors`): skew concentrates readings on few series, the retention window bounds the time span, and churn
updates and deletes a fraction of rows with autovacuum off and no vacuum afterwards so dead tuples remain. This is the path to
a production-like shape: derive the numbers from a profile of real data, record where they came from, and re-run. The default
profile is uniform.

## Scenarios

| ID | What | Measured |
| --- | --- | --- |
| S1 | `alembic upgrade head` under paced concurrent ingest, with a settled no-migration baseline window first | Wall time, exact per-index build time, ingest p50/p95/p99/max, completed and failed operations and rows/s before, during and after, read-probe latency, lock modes, WAL, temp, growth, peak size, host observations |
| S2 / S6 | The same upgrade with no concurrent ingest (the smallest load this harness can apply) | WAL, temp bytes, growth, peak size, final index and table sizes, with their measurement scope (WAL and I/O are PostgreSQL instance-wide counters, not migration-only values) |
| S3 | An old-snapshot transaction held open during the upgrade | The build waits (`waiting for old snapshots`); ingest keeps working; the upgrade completes after release |
| S4 | The first large build interrupted after 25% of its table scan by (a) a protocol CancelRequest carrying the migration connection's own key and (b) killing the alembic client process this run started. **No terminate variant** (see below) | Exit code, `alembic_version` unchanged, INVALID artifact left, no locks left, then a plain re-run: time to recover, all 22 correct |
| S5 | `alembic downgrade -1` then re-upgrade, both under ingest | Wall time and ingest latency during the `ACCESS EXCLUSIVE` window; atomicity |
| S7 | Delete of an unreferenced `integration_metric_mapping`, rolled back, `EXPLAIN (ANALYZE)` before and after | The referential-check time the new indexes exist to remove |
| S8 | After S1/S5 | All 22 indexes correct, no INVALID index anywhere, `alembic check` clean, advisory lock released |

Ingest is open-loop: each operation's latency is measured from when it was scheduled, so a stall cannot hide itself by delaying
the next request. A short settle period is discarded before the baseline window.

## Cancellation design (why there is no PID-only signalling)

`pg_cancel_backend(pid)` and `pg_terminate_backend(pid)` are PID-only. In PostgreSQL 16 `pg_signal_backend()`
(`src/backend/storage/ipc/signalfuncs.c`) looks the backend up by PID, checks permissions, and then calls `kill(-pid, sig)`; its
own comment says the process may end and the PID be recycled before the `kill()` and that "there's no way to acquire a lock on an
arbitrary process to prevent that". Checks made in SQL beforehand (database, owner tag, `backend_start`, a role, one wrapping
statement) happen before that lookup and cannot close the window, so the benchmark does not use these functions at all, and a
test fails if any executable string or function in the package reintroduces them.

Cancellation instead uses the protocol's own mechanism: a backend gives its client a secret `BackendKeyData` (pid + cancel key),
and the postmaster acts on a CancelRequest only when **both** match a backend it is running (`processCancelRequest` in
`postmaster.c`). The Alembic session is a subprocess whose connection the harness does not own, so that one connection is relayed
through `PgProxy` (`cancel.py`), a transparent TCP relay that reads the `BackendKeyData` as it passes. The harness refuses to
cancel unless exactly one connection was proxied, the key's pid is the backend running the build, and the stream was not TLS.

Residual risks: only cancel exists, because PostgreSQL has no authenticated terminate, so the terminate scenario is **disabled and
reported as NOT RUN** rather than run with a weaker guarantee (the killed-client variant still covers an abruptly lost client).
The key is 32 bits, which guards against recycled PIDs and accidents, not against an attacker on the server's network path; the
benchmark cluster is scratch. A TLS server makes the key unreadable and cancellation unavailable. The relay is used only in the
cancellation scenario, never in timed measurements. Cancel interrupts the connection's current statement, as `pg_cancel_backend`
does.

## Acceptance check for the 22 indexes

Every index is checked against its **complete** expected definition, not just its name and validity
(`harness.verify_indexes`): present; an index (not a table or view squatting on the name); `pg_get_indexdef` equal to
`CREATE INDEX <name> ON public.<table> USING btree (<column>)`; valid, ready and live; not unique, primary, exclusion, partial,
expression or carrying options; one key column; not constraint-owned; same owner as its table. Defects are reported per index
as `missing`, `not_an_index`, `wrong_definition` (this also catches an index of the right name on the wrong table or column,
i.e. a substitution), `invalid`, `not_ready`, `not_live`, `constraint_owned`, `owner_mismatch`, `unexpected_shape`, or `replaced`
(same name, different OID than recorded earlier in the same run, which catches a look-alike swapped in). The tests tamper with
each of these and assert they are reported.

## Monitoring and what is available

Per-index time is **exact** (`pg_stat_statements`, statement wall time, including waits) when the extension is installed;
otherwise the report falls back to 0.1 s progress samples and says so. Alongside PostgreSQL's own views (progress, locks, WAL,
`pg_stat_io`, temp bytes, size) the sampler records, at 0.5 s: VM-wide CPU, `MemAvailable`, swap, pressure-stall info,
filesystem free space of the runner root, OOM kills in the runner cgroup, and lock waits (samples with waiters, maximum
concurrent waiters, maximum age of a waiting statement as an upper bound). Every report lists which of these were actually
available. Caveats: in Docker Desktop/WSL2 `/proc` is the VM shared with PostgreSQL, so CPU and memory are VM-wide; the runner's
root filesystem is a sparse virtual disk that can greatly overstate free space on the host drive; disk I/O throughput is not
measured directly. **A migration that completes does not establish available production headroom.**

**What each figure measures.** The report labels every resource figure with its scope. WAL (LSN delta) and `pg_stat_io` are
**PostgreSQL instance-wide** counters: they include autovacuum, checkpoints, every other database and any concurrent ingest, and
PostgreSQL does not attribute them per session. They are therefore upper bounds on the migration's cost, never migration-only
values, even in the idle scenario. `temp_bytes` and database growth are **database-scoped** but include every writer in that
database. CPU, memory, swap, pressure and disk are **host/VM-wide**. `other_databases_xacts` records transactions in other
databases during the run so a reader can see how quiet the instance was.

## Reading the results

* **Invariants** are yes/no and fail the run: all 22 correct, no INVALID leftovers, version stamped only on success, downgrade
  atomic, interruption leaves 0056 and a recoverable INVALID artifact and releases its locks, a plain re-run recovers,
  `alembic check` clean, no failed ingest writes, indexes not replaced during the ingest tail.
* **Measurements** are reported with the environment fingerprint and are not judged. `--repeats N` runs each scenario N times and
  reports min/median/max; invariants must hold in every run. The summary never replaces the data: `repeat_details` in the JSON
  and the per-repeat tables in the Markdown keep each repeat's baseline/during/after p50/p95/p99, completed and failed writes,
  throughput, migration durations and threshold breaches. A value a repeat did not record is shown as unavailable with a reason, never
  filled in from another repeat.
* **Provisional threshold breaches** (`--max-write-failures`, `--max-ingest-p99-ratio`, `--max-ingest-p99-ms`,
  `--max-migration-seconds`, `--max-wal-mib`, `--min-free-disk-gib`) are listed in the report. The defaults are placeholders for
  your own objectives; a breach is a flag to investigate, and no breach is not an acceptance.
* The report splits time into migration, database clone, cleanup and other, so index-creation time is not mixed with
  dataset cloning. Template seeding is reported once under the dataset.

## Recorded runs

Historical development-machine evidence, kept as published: `docs/benchmark-results/0057/` holds the first 1,000,000 and
10,000,000-row runs (`raw` dataset, `raw` ingest, one run each, sampled build times, no host metrics, run as a superuser on an
unmarked cluster before the safety controls existed). All invariants held in both. Newer runs are listed in
`docs/benchmark-results/0057/README.md`. The 2026-10-25 `fk`/`service` report predates per-repeat retention, so it records only the last repeat's detail plus the metric
values; for repeat 1 the post-upgrade latency percentiles were not recorded and are not reconstructed. None of them changes the
classification: **NOT YET VALIDATED**.

## CI

`backend/tests/integration/test_fk_index_benchmark_harness.py` runs at a few thousand rows with no timing assertions: the
safety refusals (no authorization, no marker, wrong system identifier, superuser, unrecognised database, drops of databases not
owned, signalling an unrelated session, a foreign-tagged template), generator determinism and profile behaviour, the `fk`
dataset with enforced foreign keys, raw/fk/service ingest, the full-definition acceptance check against deliberate tampering,
an instrumented upgrade and its report, provisional threshold evaluation, and (deterministically, using an open writer
transaction as the barrier rather than a sleep) cancel and terminate of a build followed by recovery. The 1M and 10M runs are
manual and are not part of CI.
