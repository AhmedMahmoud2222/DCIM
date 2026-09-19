# DCIM MVP v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Steps use checkbox syntax.

**Goal:** Deliver a testable outbound edge-collector to central telemetry/alarm/dashboard vertical slice.

**Architecture:** A standalone SQLite-backed edge package polls configured targets and sends HMAC-signed batches only to the central HTTP API. Central PostgreSQL persists deduplicated occurred/received-time readings, invokes an application alarm evaluator, and supplies bounded operational APIs/UI views.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy/Alembic/PostgreSQL, SQLite, httpx, pysnmp, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-18-dcim-mvp-v0.1-design.md`

## Global Constraints

- Start from `e2accda4a3a35b2de267ca00cff1ce455cfd343a`; reuse central audit/outbox/idempotency/RBAC and network policy.
- Edge package imports no central ORM/session/RBAC modules and never logs credentials.
- No production implementation without a failing test observed first.
- Keep history APIs bounded and preserve collector `occurred_at` separately from central `received_at`.
- No full vendor/device catalog, site-scoped human RBAC, streaming platform, or production-readiness claim.

---

### Task 1: Edge queue and configuration

**Files:** Create `edge_collector/{config.py,queue.py,migrations/001_initial.sql}`, `edge_collector/tests/test_queue.py`.

**Produces:** `QueueRecord`, `SQLiteQueue.enqueue/list_due/acknowledge/mark_retry/metrics` and `CollectorConfig`.

- [ ] Write failing tests proving restart persistence, partial ACK deletion, capped queue drop accounting, retention expiry and corrupt-DB diagnostics.
- [ ] Run `pytest edge_collector/tests/test_queue.py -v`; confirm failures are missing queue/config behavior.
- [ ] Implement versioned SQLite migration, WAL transaction boundaries and the stated queue methods; expose count/bytes/oldest/dropped/retries.
- [ ] Re-run the test file; commit `feat: add durable edge collector queue`.

### Task 2: Edge transport, retry and heartbeat

**Files:** Create `edge_collector/{client.py,runtime.py,retry.py}`, tests `test_client.py`, `test_runtime.py`.

**Produces:** `CentralClient.flush(records) -> AckResult`, `EdgeRuntime.run_once()`.

- [ ] Write failing controlled-server tests for HMAC body signing, WAN/5xx/429 retry, jitter bounds, partial/duplicate ACK, and heartbeat queue metrics.
- [ ] Run targeted tests and observe connection/ACK failures.
- [ ] Implement bounded exponential retry, per-record ACK processing and outbound-only heartbeat/ingest calls.
- [ ] Re-run tests; commit `feat: add edge store and forward runtime`.

### Task 3: Real SNMP v2c and thin mapping

**Files:** Create `edge_collector/snmp.py`; add central migration/model/API mapping files and controlled wire-agent tests.

- [ ] Write a failing real UDP SNMP-agent test for configured OID, timeout, bad community, malformed value and unknown OID.
- [ ] Implement explicit v2c transport and `IntegrationMetricMapping(source_identifier, canonical_metric, unit, scale, label)`; redact communities.
- [ ] Run wire tests; commit `feat: add MVP SNMP metric acquisition`.

### Task 4: Central telemetry and alarms

**Files:** Add one Alembic migration; create telemetry/alarm domain/application/API modules and PostgreSQL tests.

- [ ] Write failing tests for replay deduplication, delayed/out-of-order timestamps, latest/history bounds, alarm open/active/ack/clear/reopen and concurrent samples.
- [ ] Implement `TelemetryReading` and `AlarmRule`/`Alarm` with indexes, audit/outbox and application evaluator boundary.
- [ ] Execute migrations fresh/upgrade/downgrade/re-upgrade and tests; commit `feat: add MVP telemetry and alarms`.

### Task 5: Operations UI, demo and acceptance

**Files:** Modify navigation/dashboard/equipment screens; add telemetry/alarm pages, demo seeder, frontend/backend performance tests, required reports.

- [ ] Write failing API/UI tests for pagination, dashboard query count and equipment operational summary.
- [ ] Implement dashboard, history charts, collector detail, equipment telemetry/alarm section and labelled demo data.
- [ ] Run backend/frontend checks plus documented controlled SNMP, WAN replay and alarm lifecycle demonstration; write implementation/test/runtime reports and commit `feat: implement DCIM MVP v0.1 vertical slice`.

## Plan self-review

Coverage maps queue/retry/SNMP to Tasks 1–3, telemetry/alarm to Task 4, and product/demo/performance/docs to Task 5. No placeholder interfaces are referenced outside their producing task. The full VendorProfile/DeviceProfile catalog and unrelated deferred findings remain outside this plan.

## Long-term telemetry retention and downsampling

Raw sensor telemetry is retained at normal polling resolution for one year. After that,
the controlled retention job stores one daily aggregate per integration/sensor/metric/day
with average, minimum, maximum, sample count and unit. It commits the aggregate before
deleting eligible raw rows in the same transaction, so interruption cannot leave a day
with neither representation. The job is idempotent; late readings for an already
aggregated day cause that day to be recomputed/merged on the next run. History selects
raw, daily, or both across the boundary and identifies its resolution. Daily aggregates
have unlimited retention by default; configurable policy changes are processed by the
job, never by immediate destructive configuration changes. Alarm lifecycle/history is
explicitly excluded and retains its independent policy.

### Retention validation clarification

The canonical telemetry-series identity is `integration + managed asset (or the
unmanaged sentinel) + external identifier + canonical metric + unit`. It is stored as
`series_key` on both raw readings and daily aggregates, is unique with the UTC calendar
day on aggregates, and is the only identity used for late-arrival merge and compaction.
This prevents an asset replacement or unit change from being silently merged into a
historical series. The retention cutoff is calendar-day based: only UTC days strictly
before the cutoff date are compacted, so the boundary day remains raw even when part of
it is older than the rolling instant. A late reading for an existing aggregate updates
that aggregate in the ingestion transaction and is then removed; a late reading with no
aggregate remains raw for the normal compactor. Alarm `opened_at`/`cleared_at` retain the
edge `occurred_at`; telemetry `received_at` remains central receipt evidence.
