# MVP Retention & Monitoring Validation

Baseline: `c0e900c46cf603b47c93222a0ed5f92864872405` on
`origin/codex/dcim-mvp-v0.1-signed`.

## Findings corrected

- **High — inconsistent daily-series identity.** Raw grouping included asset and unit,
  while aggregate lookup/uniqueness did not. Migration `0012_retention_series_identity`
  adds a durable canonical `series_key` to raw and aggregate tables, backfills existing
  data, and changes aggregate uniqueness to `(series_key, day)`.
- **High — partial UTC day compaction.** The original rolling timestamp predicate could
  compact only part of the cutoff day. The compactor now selects days strictly before
  the cutoff date.
- **Medium — concurrent compaction exposure.** Each selected raw series/day now locks
  its individual raw rows with `FOR UPDATE SKIP LOCKED`, computes exactly those rows,
  flushes the aggregate, then deletes exactly those locked IDs in the caller-owned
  transaction.
- **Medium — tied alarm history pagination.** The cursor now encodes both `opened_at`
  and `alarm.id`, matching the descending composite order.
- **Medium — unaudited monitoring policy edits.** Monitoring-policy updates now write a
  synchronous existing-framework audit entry containing the complete before/after
  policy. Validation rejects unsupported intervals and non-positive finite retention.
- **Medium — schedule drift.** Edge scheduling now uses a stable per-integration phase
  grid rather than adding jitter after each previous start. A slow in-flight poll blocks
  overlap; missed slots collapse to one next due execution.
- **Low — time-sensitive Edge queue tests.** SQLiteQueue accepts a test clock; queue and
  runtime tests no longer depend on wall-clock date.

## Retention and time semantics

Raw readings remain full resolution for 365 days. A daily aggregate stores mean, min,
max and actual sample count; missing polling intervals are therefore represented by the
count rather than assumed cadence. Daily aggregates and alarm history have unlimited
retention by default. No settings update invokes cleanup.

The compactor persists and flushes the aggregate before deleting raw rows. Both actions
are in the caller transaction; rollback after a persistence error, after flush, or after
delete leaves durable data recoverable. An old late reading merges into an existing
aggregate in the ingestion transaction and deletes its raw row only with that successful
aggregate update. Without an aggregate it remains raw for the normal compactor.

Alarm lifecycle timestamps use the Edge `occurred_at`; `received_at` is retained on the
telemetry reading and copied into alarm details for central receipt traceability. Older
out-of-order readings cannot overwrite an alarm whose latest reading occurred later, or
reopen a lifecycle already cleared by a later reading.

## Executed evidence

- `git fetch origin --prune`: remote baseline resolved to `c0e900c...`.
- `pytest --noconftest backend/tests/unit/test_monitoring_policy_contract.py backend/tests/unit/test_alarm_history_cursor.py edge_collector/tests -q`:
  **30 passed** (one non-fatal existing pytest-cache warning).
- Targeted Ruff: **passed**.
- Targeted mypy for retention, alarms and settings: **passed**.
- `alembic heads`: one head, `0012_retention_series_identity`.

### Authoritative PostgreSQL CI evidence

GitHub Actions [CI run 35413529110](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/35413529110)
ran against the existing PostgreSQL **16** service configuration on commit
`764f42dbd49b132aceadf1b1f2b1e4d61589c761`.

- Fresh database migration to head: passed.
- Retention migration validation: `head → 0010_mvp_alarms → head` passed; exactly
  one Alembic head was asserted before and after.
- `pytest -q tests/integration/test_mvp_retention_hostile.py`: **6 passed in
  1.51s**. This executed aggregate arithmetic, independent series/metric isolation,
  complete-day boundary handling, repeat processing, late-arrival merge,
  no-existing-aggregate behavior, rollback recoverability, database uniqueness, and
  two overlapping PostgreSQL compactor workers.
- Backend regression: **531 passed, 14 warnings in 191.02s**. This includes the
  existing telemetry, alarm, collector/integration, RBAC, audit and outbox coverage.
- Ruff, mypy, frontend typecheck/lint/build: passed.

The first CI attempt identified a test-fixture issue rather than a retention defect:
the rollback test had not committed its setup reading before intentionally rolling back
the compaction transaction. It was corrected in `e1b41f4`; the succeeding CI run above
is the authoritative result. The CI workflow was also corrected to expose the repository
root on `PYTHONPATH`, allowing the existing monitoring contract test to import the Edge
scheduler package during backend regression.

## Remaining limitations

The local Windows workspace still has no PostgreSQL/Docker service, but that limitation
is closed for this checkpoint by the authoritative GitHub Actions PostgreSQL evidence.
This is an MVP validation result, not a production scale/load or long-duration retention
benchmark. Dashboard/UI work has intentionally not started.

POSTGRESQL RETENTION CI VALIDATION PASSED — READY FOR INDEPENDENT REVIEW
