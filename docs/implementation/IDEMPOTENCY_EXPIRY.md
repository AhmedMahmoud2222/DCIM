# Idempotency expiry and replacement (DB-03)

**Issue:** #357 (DB-AUDIT-01) · **Prerequisite:** DISC-ORDER-01 (PR #369, merged as `d026992f`) · **Scope:** `app/application/idempotency.py`, `app/application/idempotency_purge.py`, settings in `app/core/config.py`.

This change enforces the `expires_at` already stored on every `idempotency_key` row, and adds a purge that is **disabled** by default. It adds **no migration and no index**; the production hold on Migration 0057 and the #214 / DB-06 lane are unchanged.

## API contract: bounded-window idempotency

An `Idempotency-Key` identifies one logical write **for a bounded window**, not forever.

| Endpoint family | Window | Principal |
|---|---|---|
| `POST /managed-assets`, `/equipment`, `/equipment/instantiate`, `/racks`, `/power/pdus/instantiate` | **24 hours** per claim generation | `user:<id>` |
| `POST /collectors/{id}/ingest` (per record `dedup_key`) | `collector_idempotency_ttl_hours`, default **72 h**, minimum **36 h** | `collector:<id>` |

Within the window nothing changes: the same key and body replays the original response; the same key with a different body is rejected; a request that races a live owner waits up to five seconds before giving up. How each outcome is reported differs between the user endpoints and collector ingestion:

| Outcome | The five user endpoints | `POST /collectors/{id}/ingest` (per record) |
|---|---|---|
| Same key, different body, in window | `422 Idempotency Key Reused` | `status="rejected"`, `error_code="IDEMPOTENCY_CONFLICT"` |
| Owner still processing after the 5 s window (`IdempotencyStillProcessing`) | `503 Request Still Processing` | `status="rejected"`, `error_code="PROCESSING"` (retryable; HTTP 200 for the batch) |
| Claim taken over before it could be used (`IdempotencyClaimLost` raised by `get_or_claim`) | `503 Request Superseded`, `Retry-After: 1`, nothing written | `status="rejected"`, `error_code="PROCESSING"` (retryable); no claim is owned, so nothing is released and the new owner's claim is untouched |
| Claim lost at completion (`IdempotencyClaimLost` from `complete_claim`) | `503 Request Superseded`; transaction, domain write included, rolled back | isolated inside the record's savepoint: `status="rejected"`, `error_code="INTERNAL_PROCESSING_ERROR"` (retryable, siblings unaffected) |
| Replay of a completed record in window | original response | `status="duplicate"` |

**After the window** (`expires_at <= now`, inclusive) the key is treated as unused and the request **executes again**, evaluated *before* the request-hash comparison:

| Situation after expiry | Result |
|---|---|
| Same key, same body, create whose business key (`asset_tag`) already exists | **`409 Conflict`**, not the original `201`. No second asset, audit row or outbox event is written, and the claim is released, so every further retry also answers 409 |
| Same key, new body or a free `asset_tag` | Executes normally (`201`). It used to be a permanent `422` |
| Collector record replayed after expiry | Acknowledged `accepted` and a no-op: device, neighbor and scan-marker ingestion are order-safe since DISC-ORDER-01, so an old record can neither regress newer data nor emit events |

**Guidance for client authors.** Treat a `409` on a *retried* create as "possibly my own earlier success": `GET` the resource by its `asset_tag` before reporting failure. Generate a fresh key per logical submission (the frontend already does, #338). The 409 is possible only after the window; retries inside it still replay.

A claim generation is renewed (`expires_at` reset) when it starts: new claim, stale reclaim (owner silent for 30 s) or expired replacement. Completion never renews it.

## Mechanics

* **Replacement** is one compare-and-swap `UPDATE ... WHERE id = (SELECT id ... AND claim_generation = :seen AND expires_at <= :now FOR UPDATE SKIP LOCKED)`. It bumps `claim_generation` exactly once, clears the stored response, records the new hash and expiry, and commits immediately. A locked or changed row yields zero rows: it never waits and never rolls back (a session rollback would expire the caller's ORM objects, e.g. `ctx.user`). The new claim is reloaded with `populate_existing=True`.
* **Fencing** is unchanged: `complete_claim` / `release_claim` act only on `status='processing'` and the caller's generation, so a stale owner gets `IdempotencyClaimLost` and its transaction, domain write included, is rolled back (see the status table above for how each caller reports it). The claim statements return the generation they issued, and the reload verifies exactly that generation and `status='processing'`: a caller whose claim was reclaimed, replaced, completed or deleted between the statement's commit and the reload gets `IdempotencyClaimLost` instead of adopting the newer owner's fencing token.
* **Two clocks.** `_now()` (injectable) decides expiry and staleness; the polling deadline is `time.monotonic()` taken at function entry, so a blocked first INSERT shares the five-second budget and a frozen expiry clock cannot hang the loop. Every non-returning loop iteration checks the deadline before sleeping.
* **No hard response-time SLA.** The five-second bound covers the application's polling only. A database lock wait or connection-pool wait inside a single statement is outside application control (DB-12).
* **Principal guard.** `get_or_claim` accepts only `user:` and `collector:` principals; `legacy:unscoped` and malformed values raise `ValueError` before any database access. Legacy rows stay inert and become eligible for generic purge once expired.

## Collector TTL must exceed the edge replay window

The Edge Collector drops a queued record once `occurred_at < now - retention_seconds` (default 24 h, no CLI/env override in the shipped entrypoint) and retries with exponential backoff capped at 300 s (jitter included). A conforming collector therefore re-sends a record for at most about `retention_seconds + 300 s + HTTP timeout`. Central cannot read the edge value, so `collector_idempotency_ttl_hours` (default 72, validated `>= 36`) is a guard, not proof.

**Development qualification only (owner clarification, 2026-11-20):** no Edge Collector is deployed; this is verified from source and tests, and the repository default is **not** a deployed configuration.

### Mandatory deployment gate

Before any Edge Collector is deployed or this change is promoted, record in the deployment evidence register (`docs/OPERATIONS.md`) that

`collector_idempotency_ttl_hours  >  effective edge retention_seconds + maximum retry delay + HTTP timeout + operating margin`

holds for **every** appliance, including any queue restored from backup, copied SQLite file, alternate collector implementation or disaster-recovery path that can replay records. Because device, neighbor and scan-marker ingestion are idempotent and order-safe, a violated margin yields a harmless `accepted` no-op rather than a duplicate; the gate still stands because the collector ledger is the fast-path skip and the `duplicate` acknowledgement.

## Purge (disabled by default)

`idempotency_purge_enabled` defaults to `False` and **no shipped configuration enables it**. While disabled, the Celery task (`purge_expired_idempotency_keys`, registered every 300 s) returns `0` before any engine, session or SQL exists. Enabling it is a configuration change plus worker restart that needs separate written authorization and the capacity and index gates from the DB-03 design (#357): ledger volume, `EXPLAIN (ANALYZE, BUFFERS)` of the batch, backlog drain plan, and monitoring. An `expires_at` index is **not** part of this change and, if the gates require it, ships only through the #214 / DB-06 lane.

When enabled, each batch is one statement (`WITH doomed AS (SELECT id ... WHERE expires_at <= :cutoff LIMIT :batch FOR UPDATE SKIP LOCKED) DELETE ... USING doomed`) under a transaction-local `statement_timeout` of at most 2000 ms. Rows must be `idempotency_purge_grace_seconds` (default and minimum 3600) past `expires_at`. Runs are time-budgeted (`idempotency_purge_max_runtime_seconds`, default 30) and throttled (`idempotency_purge_pause_ms`, default 50); batch size defaults to 500 and is capped at 5000. Failures log a fixed code only, never driver text.

| Setting | Default | Validation |
|---|---|---|
| `collector_idempotency_ttl_hours` | 72 | `>= 36` |
| `idempotency_purge_enabled` | `false` | bool |
| `idempotency_purge_grace_seconds` | 3600 | `>= 3600` |
| `idempotency_purge_batch_size` | 500 | 1 to 5000 |
| `idempotency_purge_max_runtime_seconds` | 30 | `> 0`, `<= 240` |
| `idempotency_purge_pause_ms` | 50 | 0 to 5000 |
| `idempotency_purge_statement_timeout_ms` | 2000 | 1 to 2000 |

## Release notes

* **Behaviour change:** after the 24 h (user) or 72 h (collector) window an `Idempotency-Key` no longer replays or conflicts; the request executes again. A retried successful create therefore returns `409` instead of the original `201`, and reusing an expired key with a different body no longer returns `422`.
* New settings listed above; defaults preserve existing deployments. Collector ingestion claims now live 72 h instead of 24 h.
* Reserved or malformed idempotency principals are rejected.
* Purge task registered but inert. **No migration, no index, no data deleted.**

## Verification evidence

`backend/tests/integration/test_idempotency_expiry.py`, `test_idempotency_purge.py`, `backend/tests/api/test_idempotency_post_expiry_http.py` and `test_idempotency_collector_expiry.py` run against real PostgreSQL, including `INSERT ... ON CONFLICT DO NOTHING` waiting on an uncommitted delete, `SKIP LOCKED` replacement under a held lock, completion racing replacement and purge, identity-map freshness, and the fixed-code purge timeout path.
