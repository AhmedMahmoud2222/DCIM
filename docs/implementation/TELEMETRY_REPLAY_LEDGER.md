# Telemetry replay protection (Issue #128 / G4-R)

Version 1.0

Scope: stop a redelivered telemetry record from being counted twice in `daily_telemetry_aggregate` after its raw row is gone.
Out of scope: scheduling compaction (nothing in this repository does), evidence lifetime and legal hold (G6), history trust
labels (G7). Alembic revision `0049_telemetry_replay_ledger` (head was 0048).

## The defect

`compact_eligible_raw` and `merge_late_reading` fold a raw reading into a daily aggregate and delete the row. The row carried
the unique `(collector_id, dedup_key)` key, so a collector that redelivers the record gets a fresh insert and the value is
folded a second time.

## Mechanism

* Table `telemetry_consumed_key(key_hash BYTEA PRIMARY KEY, consumed_day DATE)`. `key_hash` is the first 16 bytes of
  SHA-256 over `collector_id || 0x00 || dedup_key`. The ledger stores no identifier, value, series or timestamp.
* Both paths claim a record's key with `INSERT ... ON CONFLICT DO NOTHING RETURNING` before folding. Only the transaction
  whose insert succeeded may fold the value. A conflict means the value is already counted.
* Compaction deletes every locked raw row but folds only freshly claimed ones. A raw row whose key already has a tombstone
  is deleted without being folded.
* Late merge order: lock the aggregate row, apply the horizon, claim the key, fold. A duplicate returns
  `LateMergeOutcome.DUPLICATE`; ingest deletes the new reading and acknowledges `duplicate` (never `accepted`).
* Everything runs in the caller's transaction. A failure (arithmetic error, horizon refusal, first-aggregate insert conflict)
  rolls back the tombstone with the aggregate and the deletes. Lock order is unchanged: aggregate row, then ledger.
* A simultaneous redelivery and compaction serialise on the raw row's unique key, then on the ledger primary key.

## Horizon: `monitoring_policy.late_replay_horizon_days`

| Value | Behaviour |
|---|---|
| `NULL` (default) | No age-based rejection. The prune task is a strict no-op. **The ledger grows without bound.** |
| `H >= 1` | A late record for an already-compacted day is refused when `now - occurred_at > H` (equal is accepted). The prune task deletes tombstones older than `H`. |

No value is chosen in this change. Profiling a representative database is **blocked** (no authorized source); the SQL is not
run anywhere. Setting a finite `H` needs that profile and a separate owner decision. At the API, `PUT /settings/monitoring`
changes the field only when the request includes it; omitting it keeps the stored value.

Refusal returns the permanent ack code `REPLAY_HORIZON_EXCEEDED`. The collector treats it as acknowledged-and-dropped. Older
collectors treat an unknown code as retry, so Central can deploy first (see below).

### `ledger_complete_since` and the effective horizon

`telemetry_replay_ledger_state.ledger_complete_since` is the instant from which the ledger holds every tombstone still
needed. It starts at the migration time and the prune task advances it before deleting. Merges use
`H_eff = min(H, now - ledger_complete_since)`. Raising `H` therefore takes effect gradually and never trusts tombstones that
were pruned or never existed. Lowering `H` takes effect at once. Right after the migration, a newly configured finite `H`
refuses records older than the time since the migration; that is the intended conservative behaviour.

### Pruning

Hourly Celery beat `prune-consumed-telemetry-keys` (queue `maintenance`) calls `prune_consumed_keys`, following the
nonce/heartbeat pattern: batches of 5,000, at most 100 batches per run, `SKIP LOCKED`, one commit per batch, a run that fails
leaves earlier batches committed and the next run resumes. It deletes `consumed_day < utc_day(now - H) - 1 day`. **This is
not a compaction runner**; no telemetry compaction task exists or is added. Monthly partitioning is not used.

## Hold release (operator)

Releasing a contract hold into an already-compacted day whose record is older than the effective horizon returns HTTP 409
("Hold is past the replay horizon"). The savepoint rolls back, the hold stays `held` with its evidence, and no RBAC override
exists. Releases into uncompacted days, and any release while `H` is `NULL`, are unchanged.

## Pre-ledger coverage gap (accepted, R10a)

Readings consumed before migration 0049 left no key. Nothing can be backfilled because their raw rows are already deleted.
A redelivery of such a record is still counted twice. With a finite `H` it is refused once it is older than the effective
horizon, so the gap closes as `ledger_complete_since` ages past `H`; with `H = NULL` it stays open. Test:
`test_pre_ledger_coverage_gap_is_bounded_by_ledger_complete_since`.

**Operations warning:** if compaction has ever run in an environment, aggregates created before 0049 can contain duplicates
that no later step can detect. Do not report those days as duplicate-free.

## Exposure bound

A conforming collector can only double-count when `R + 360 s > P days`, where `R` is the collector's `retention_seconds`
(default 86,400, no upper bound) and `P` is `raw_retention_days` (API minimum 1, default 365). Operator hold release has no
age bound, so the horizon, not collector behaviour, is the control for held records.

## External compaction runner gate

Repository search cannot prove that no out-of-repo job calls `compact_eligible_raw`. Before any deployment of this change,
the operator confirms in writing whether such automation exists. Query Q01 of the profiling SQL detects it from the database
(only `compact_eligible_raw` creates aggregates). Enabling or adding a runner is a separate decision.

## Storage and backup

Synthetic sandbox measurement: 104 to 111 bytes per tombstone, against about 514 bytes per raw reading. These are not
production figures. Treat backup and restore as one database unit: restoring `telemetry_consumed_key`,
`daily_telemetry_aggregate` and `telemetry_reading` from different points in time, or any partial-table restore, is
unsupported and can reintroduce double counting.

## Monitoring

* Structured logs with fixed fields only (no key, hash or collector id): `telemetry_replay_duplicate_suppressed` (path,
  count), `telemetry_replay_horizon_refused`, `telemetry_ledger_pruned` (deleted, batch_limit_reached),
  `telemetry_ledger_prune_skipped`, `telemetry_ledger_prune_failed`. These are log events, not persisted counters.
* `GET /api/v1/telemetry/replay-ledger` (`telemetry:read`): horizon, effective horizon, `ledger_complete_since`, last prune,
  oldest `consumed_day`, approximate tombstone count and `warnings`. Alert on `horizon_unset_ledger_grows_without_bound` and
  `prune_lagging`.

## G6 evidence lifetime

The ledger is a separate evidence-lifetime tier: it outlives raw readings and, while `H` is `NULL`, it outlives everything
else. G6 decides its retention and legal-hold rules; this change only documents the tier.

## Deployment order

1. Confirm the external runner gate above.
2. Deploy Central (migration 0049 is additive; it alters only the one-row `monitoring_policy` table).
3. Collectors can update later; an old collector retries `REPLAY_HORIZON_EXCEEDED` instead of dropping it.

Downgrade of 0049 refuses while any tombstone exists or a horizon is set.
