# SEC-05 collector nonce and heartbeat retention

Parent: [Phase 11 security backlog #38](https://github.com/AhmedMahmoud2222/DCIM/issues/38).
The owner approved a **minimum** of one hour for request nonces and 30 days for
heartbeat history. This change affects collector metadata only; telemetry, audit and
customer inventory retention are unchanged.

## Runtime contract

Celery beat queues `prune_collector_nonces_and_heartbeats` every hour on the existing
`maintenance` queue. The existing worker already consumes that queue. The worker
loads `NONCE_RETENTION_SECONDS` (default 3600, minimum 3600) and
`HEARTBEAT_RETENTION_DAYS` (default 30, minimum 30) from its environment. Compose
forwards these two optional settings to the worker. Increasing a value retains data
longer; values shorter than the owner-approved policy fail startup validation.
The task also refuses nonce retention at or below the application's timestamp
acceptance window (currently 300 seconds), so a future increase in the window
cannot silently weaken replay protection.

Cutoffs use server UTC time and strict `<`: records **at** the cutoff remain, older
ones are eligible. A nonce is claimed with server `seen_at` after HMAC verification;
replays still hit the unique `(collector_id, nonce)` constraint while the signed
request's timestamp can be accepted. Old signed requests fail timestamp validation
before nonce lookup even after their nonce row is pruned.

Each run makes at most 100 batches of 500 per table. Both table deletes in a batch
share one transaction and commit together. Failed batches roll back both deletes;
prior committed batches remain safely deleted and a subsequent run resumes. The
task raises a fixed-message error on failure rather than succeeding silently.
`FOR UPDATE SKIP LOCKED` allows overlapping workers and retries without waiting
on the same rows; counts represent actual committed deletions. If the cap is
reached, `batch_limit_reached=true` is logged and the next hourly run continues.
Only counts, batch index and fixed codes are logged; no nonce, collector ID,
heartbeat payload or raw SQL exception text is logged.

## Migration and operational impact

Migration `0025_collector_retention` adds time-leading B-tree indexes on
`collector_request_nonce.seen_at` and `collector_heartbeat.ts`. Inspection of
the pre-change schema found only a collector-scoped nonce index and a composite
heartbeat index beginning with `collector_id`; neither is suitable for the
all-collector cutoff and timestamp ordering. Disposable PostgreSQL tests run
`EXPLAIN` for both global range queries with sequential scans disabled to verify
the indexes support the actual query shape. Index creation uses PostgreSQL
`CONCURRENTLY` outside a transaction so ordinary inserts need not wait for a
write-blocking index build. A failed concurrent build may leave an invalid index:
inspect `pg_index.indisvalid`, remove an invalid index in a reviewed maintenance
window, and rerun the migration. Do not manually start the cleanup task before
migration and review of the database backup. The migration itself deletes no rows.

Historical heartbeat views and trend consumers will have no rows older than the
configured period after maintenance catches up. Health classification uses the
latest heartbeat and is unaffected for active collectors. Export older history
before rollout if required by a separately approved audit or reporting policy.
Monitor `collector_retention_completed` deleted counts and `batch_limit_reached`,
and alert on `collector_retention_failed` or growing maintenance queue/backlog.
No cleanup is run against an operational database as part of PR validation.
