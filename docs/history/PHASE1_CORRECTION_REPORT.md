# Phase 1 Correction Report

## 1. Executive Summary

This is a targeted corrective implementation, not a redesign. It closes the four named
findings from the independent Phase 1 red-team (C1, H1, M2, M3), plus three additional
findings from the same report judged legitimate and in-scope to fix (M1, L1, L3) and two
documentation-only ones (L2, L4). While validating the C1 fix, a second, deeper layer of
the same finding was discovered and closed: PostgreSQL grants a *database owner* an
implicit `DROP TABLE` right over every table in that database regardless of individual
table ownership — so `dcim_app` being the owner of the `dcim`/`dcim_test` databases
(the original setup) let it drop `audit_log`'s partitions even after table-level
ownership had been correctly transferred away. Closing this required changes beyond
`scripts/bootstrap_privileged_roles.sql`: the database-creation steps in `README.md`,
`docker-compose.yml`'s init flow, and `.github/workflows/ci.yml` no longer make
`dcim_app` the database owner.

No architecture, migration history rewrite, Phase 2 functionality, or unrelated
refactor was introduced. 76 tests pass (59 original, unmodified, + 17 new regression
tests), `ruff check` and `mypy` are clean, and every fix was independently re-verified
against real PostgreSQL 16 + Redis 7, including via genuine `dcim_app` *login*
connections (not a superuser session with `SET ROLE`, which was found during this work
to mask the database-ownership behavior above) and a separate `uvicorn` process for the
concurrency fix.

## 2. Original Implementation Commit

`2f865b9e548eab7f55e74b6169471fdd89d13605`

## 3. Red-Team Commit

`a5d32ad4925a483794ddf3d0f47df99c145d3baa`

## 4. Corrections Applied

| Finding | Severity | Correction | Files | Migration | Tests |
|---|---|---|---|---|---|
| C1 | CRITICAL | audit_log (+ partitions) ownership transferred to `dcim_retention_admin`; `dcim_app` granted only INSERT/SELECT; TRUNCATE explicitly revoked; partition creation via a scoped `SECURITY DEFINER` function; **`dcim_app` no longer owns the database** (the deeper layer found during validation) | `backend/scripts/bootstrap_privileged_roles.sql`, `backend/app/infrastructure/tasks/audit_partition_maintenance.py`, `README.md`, `docker-compose.yml`, `backend/scripts/docker-initdb/01-create-app-role.sh` (new), `.github/workflows/ci.yml`, `.env.example` | `0003_correction` (defense-in-depth TRUNCATE revoke only; ownership transfer is superuser-only, lives in the bootstrap script) | `tests/integration/test_db_constraints.py` (6 dedicated tests) |
| H1 | HIGH | Idempotency-Key handling rewritten from check-cache-then-write to an atomic claim (`INSERT ... ON CONFLICT DO NOTHING`, committed immediately) with poll/reclaim/release | `backend/app/application/idempotency.py`, `backend/app/domain/idempotency/models.py`, `backend/app/api/v1/managed_assets.py` | `0003_correction` (adds `status` column, relaxes `response_status`/`response_body` to nullable) | `tests/integration/test_idempotency_concurrency.py` (5 tests) |
| M2 | MEDIUM | `CHECK` constraint blocks `replaces_asset_id = id` | `backend/app/domain/identity/models.py` | `0003_correction` | `tests/integration/test_db_constraints.py::test_managed_asset_cannot_replace_itself` |
| M3 | MEDIUM | `BEFORE INSERT OR UPDATE` trigger walks the full replacement chain via a recursive CTE, blocking any cycle (2-node or longer), not just the direct case | `backend/migrations/versions/0003_correction_idempotency_and_replacement_integrity.py` (trigger function lives only in the migration — no ORM equivalent) | `0003_correction` | 3 tests: 2-cycle, longer cycle, non-cyclic chain still works |
| M1 | MEDIUM | `max_length` added to `ManagedAssetIn` Pydantic fields, matching each column's DB width | `backend/app/api/v1/managed_assets.py` | — (Pydantic-level, no schema change) | `tests/api/test_managed_assets.py::test_oversized_asset_tag_is_a_clean_validation_error_not_a_500` |
| L1 | LOW | Migration `0002`'s `downgrade()` now drops the monthly partitions it created, not just the default one | `backend/migrations/versions/0002_audit_partitions_retention_and_rbac_seed.py` | `0002_seed` (downgrade only) | Verified via a full downgrade-to-base/upgrade-to-head cycle (manual; no dedicated pytest, since no test exercises `alembic downgrade`) |
| L3 | LOW | `/api/v1/health/ready` returns `503` (not `200`) when any dependency is down | `backend/app/api/v1/health.py` | — | `tests/api/test_health.py::test_readiness_returns_503_when_a_dependency_is_down` |
| L2 | LOW (doc) | `PHASE1_DEVIATIONS.md` D4 wording corrected to note `npm audit`'s "moderate + high" labels, not just "moderate" | `PHASE1_DEVIATIONS.md` | — | — |
| L4 | LOW (doc) | `pip-audit` finding (`setuptools`/`PYSEC-2026-3447`) now documented | `PHASE1_DEVIATIONS.md` | — | — |

## 5. Audit Security Correction

### Original Vulnerability

`dcim_app` (the application's own runtime/migration database role) could destroy or
structurally modify `audit_log` using nothing but its own configured credentials:
- `TRUNCATE TABLE audit_log` succeeded (migration `0002` revoked `UPDATE`/`DELETE` but
  never `TRUNCATE`, a distinct, separately-grantable privilege).
- `ALTER TABLE audit_log ADD COLUMN ...` and `DROP TABLE <any partition>` succeeded
  because `dcim_app` *owned* `audit_log` (having created it via Alembic), and PostgreSQL
  treats DROP/ALTER as inherent to ownership, not a revocable ACL privilege.

### Deeper Layer Found While Validating the Fix

After transferring `audit_log`'s table ownership to `dcim_retention_admin` and
re-verifying, `TRUNCATE`/`UPDATE`/`DELETE`/`ALTER` were all correctly denied — but
`DROP TABLE audit_log_2026_09` (a partition) still **succeeded** for `dcim_app`. Isolated
reproduction (a throwaway partitioned table, parent and child both owned by a third
role, `dcim_app` granted nothing on either) confirmed this is a general PostgreSQL
behavior, not specific to `audit_log`: **the owner of the *database* can `DROP TABLE`
any table in it, regardless of that table's own ownership or grants.** A control test
against a role that was *not* the database owner, with the identical grant pattern,
correctly got `must be owner of table`. `dcim_app` was the owner of `dcim`/`dcim_test`
(`CREATE DATABASE dcim OWNER dcim_app`, the pattern this repository originally used and
recommended in `README.md`), so table-ownership transfer alone could not close this.

### Corrected Security Boundary

- `audit_log` and every partition (existing and future) are owned by a dedicated
  `NOLOGIN`, `NOINHERIT` role, `dcim_retention_admin`.
- `dcim_app` is granted exactly `INSERT, SELECT` on `audit_log`; `TRUNCATE` is
  additionally revoked as defense-in-depth (empirically confirmed `REVOKE TRUNCATE`
  does bind even a table's owner, unlike `ALTER`/`DROP`).
- Creating a new monthly partition requires ALTER-level rights on the parent (attaching
  a partition is an implicit `ALTER TABLE`), which `dcim_app` no longer has directly. It
  is granted `EXECUTE` on `dcim_create_audit_log_partition(...)`, a `SECURITY DEFINER`
  SQL function owned by `dcim_retention_admin` (with `SET search_path` against
  search-path hijacking and input validation on the partition-name format) — the
  `audit_partition_maintenance` Celery task now calls this function instead of raw DDL.
- `dcim_app` is no longer the owner of the `dcim`/`dcim_test` databases. `README.md`,
  `docker-compose.yml`'s init flow (`backend/scripts/docker-initdb/01-create-app-role.sh`,
  new), and `.github/workflows/ci.yml` now create the database owned by the superuser
  (`postgres`) and grant `dcim_app` exactly `CREATE, USAGE` on the `public` schema — enough
  to run migrations and own the tables it creates, not enough to bypass another role's
  table ownership. `scripts/bootstrap_privileged_roles.sql` additionally reassigns the
  database away from `dcim_app` defensively if it is ever found to be the owner, so this
  holds regardless of how a given environment's database was created.

### PostgreSQL Evidence

All of the following were run against a genuine `dcim_app` **login** connection
(`psql "postgresql://dcim_app@localhost:5432/dcim"`), not a superuser session using
`SET ROLE` (which does not reliably reproduce the database-ownership behavior for
manual ad-hoc testing — the automated pytest suite's `db_session` fixture, which
connects as `dcim_app` directly, was unaffected by this distinction throughout):

```
TRUNCATE TABLE audit_log;                       -- ERROR: permission denied for table audit_log
ALTER TABLE audit_log ADD COLUMN backdoor text;  -- ERROR: must be owner of table audit_log
DROP TABLE audit_log_2026_09;                    -- ERROR: must be owner of table audit_log_2026_09
DROP TABLE audit_log;                            -- ERROR: must be owner of table audit_log
UPDATE audit_log SET action='x';                 -- ERROR: permission denied for table audit_log
DELETE FROM audit_log;                           -- ERROR: permission denied for table audit_log

INSERT INTO audit_log (...) VALUES (...);        -- INSERT 0 1  (succeeds)
SELECT count(*) FROM audit_log WHERE ...;         -- succeeds
SELECT dcim_create_audit_log_partition('audit_log_2099_09', '2099-09-01', '2099-10-01');
                                                  -- succeeds; new partition owned by dcim_retention_admin
```

`pg_database`/`pg_class` inspection: `dcim`/`dcim_test` owned by `postgres`;
`audit_log` and all its partitions owned by `dcim_retention_admin`.

### Application Evidence

76/76 tests pass including the new privilege tests; a live `uvicorn` process against the
rebuilt `dcim` database created audit rows normally through the API (login, asset
creation) throughout this session with no functional regression.

### Regression Tests

`tests/integration/test_db_constraints.py`:
`test_audit_log_is_append_only_at_the_database_level` (pre-existing, UPDATE/DELETE),
`test_audit_log_cannot_be_truncated_by_the_application_role`,
`test_audit_log_cannot_be_altered_or_dropped_by_the_application_role`,
`test_audit_log_partition_cannot_be_dropped_by_the_application_role` (the deeper-layer
finding), `test_dcim_app_does_not_own_the_database`,
`test_audit_log_insert_and_select_still_work_for_the_application_role`,
`test_audit_log_partition_creation_still_works_through_the_scoped_function`.

## 6. Idempotency Concurrency Correction

### Original Behavior

`check_cached_response()` → perform the write → `store_response()`, with no
serialization between the check and the store. Ten truly concurrent identical requests
(same `Idempotency-Key`) produced `[201, 409, 409, 409, 409, 409, 409, 409, 409, 201]` —
only 2/10 succeeded; the other 8 received an incorrect `409` instead of the idempotently
correct `201` replay, even though the database's own `asset_tag` uniqueness prevented
any actual duplicate row.

### Corrected Behavior

`get_or_claim()` (`app/application/idempotency.py`) attempts an atomic claim —
`INSERT ... ON CONFLICT (key, endpoint) DO NOTHING RETURNING id`, committed immediately
so the claim is visible to other transactions under MVCC before any work begins. Exactly
one concurrent request wins (PostgreSQL's unique index serializes this correctly). The
winner performs the write and calls `complete_claim()` in the *same* transaction, so the
domain write and the idempotency completion become visible together atomically. Losers
poll (bounded, ~5s) for the winner's row to reach `'completed'`, then replay its
response; a losing request whose key was used with a different request hash gets an
immediate `422`, without waiting. If the claim owner's write fails, `release_claim()`
deletes the row (in a fresh transaction) so the key is not permanently poisoned. A claim
stuck in `'processing'` past `STALE_CLAIM_TIMEOUT` (30s) may be reclaimed by a waiting
request via a compare-and-swap `UPDATE`, mirroring the outbox dispatcher's existing
stale-processing reclaim pattern.

### Concurrency Test Results

Against a real running server (both an in-process ASGI app using per-request database
sessions from a dedicated connection pool — not the shared single-session test client —
and, independently, a genuinely separate `uvicorn` OS process):

```
10 concurrent identical requests: [201]*10, 1 distinct asset id, 1 audit row
20 concurrent identical requests: [201]*20, 1 distinct asset id
```

versus the original `[201, 409, 409, 409, 409, 409, 409, 409, 409, 201]` for 10 requests.

### Failure/Retry Behavior

- Same key, different payload → deterministic `422` (`test_same_key_different_payload_is_rejected_deterministically`).
- Claim owner's write fails (asset_tag collides with an unrelated existing row) →
  claim row is deleted, not left stuck; a retry with the same key and a valid body
  succeeds (`test_failed_first_request_does_not_permanently_poison_the_key`).
- A `'processing'` row manually back-dated past the stale timeout is reclaimed and
  completed by the next request with that key, rather than waiting forever or returning
  a false conflict (`test_stale_processing_claim_is_reclaimed`).

### Regression Tests

`tests/integration/test_idempotency_concurrency.py` (5 tests, all against a real,
per-request-session-isolated database connection, not a shared session):
`test_ten_concurrent_identical_requests_produce_exactly_one_resource`,
`test_twenty_concurrent_identical_requests_produce_exactly_one_resource`,
`test_same_key_different_payload_is_rejected_deterministically`,
`test_failed_first_request_does_not_permanently_poison_the_key`,
`test_stale_processing_claim_is_reclaimed`.

## 7. Asset Replacement Corrections

### Self-Reference

`CHECK (replaces_asset_id IS NULL OR replaces_asset_id != id)` added to `managed_asset`
(migration `0003_correction`, also declared in the ORM model so autogenerate stays
consistent). `UPDATE managed_asset SET replaces_asset_id = id ...` now fails at
statement-execution time (not deferred to commit — ordinary PostgreSQL `CHECK`
constraints are not deferred by default).

### Cycles

A `CHECK` constraint cannot express a cross-row invariant, so a `BEFORE INSERT OR
UPDATE OF replaces_asset_id` trigger (`check_managed_asset_replacement_acyclic()`) walks
the full replacement chain via a recursive CTE starting at the asset being claimed as
replaced; if the row being written appears anywhere in that chain, the write is
rejected. This closes both the direct 2-cycle case the red-team demonstrated (A replaces
B, then B replaces A) and arbitrary longer cycles (A→B→C→A) — tested explicitly, not
just the minimal case. A legitimate, non-cyclic chain (A replaced by B replaced by C)
is unaffected.

### Regression Tests

`tests/integration/test_db_constraints.py`:
`test_managed_asset_cannot_replace_itself`,
`test_managed_asset_replacement_cannot_form_a_two_cycle`,
`test_managed_asset_replacement_cannot_form_a_longer_cycle`,
`test_managed_asset_non_cyclic_replacement_chain_still_works`.

## 8. Other Red-Team Findings

| Finding | Disposition | Reason |
|---|---|---|
| M1 (oversized input → 500) | Fixed | Simple, contained, no architectural implication — `max_length` added to the request schema |
| L1 (migration downgrade incomplete) | Fixed | Contained fix to `0002`'s `downgrade()`; verified via a full downgrade-to-base/upgrade-to-head cycle |
| L3 (readiness 200 while degraded) | Fixed | Contained fix; matches the finding's own recommended direction (non-2xx on degraded) |
| L2 (deviations doc severity wording) | Fixed (doc only) | `PHASE1_DEVIATIONS.md` D4 updated |
| L4 (undocumented pip-audit finding) | Fixed (doc only) | `PHASE1_DEVIATIONS.md` new row added |
| OBS1 (no CSP/HSTS headers) | Deferred, unchanged | Not required by any Phase 1 architecture section; no security regression from leaving it |
| OBS2 (Redis-failure test inconclusive in the red-team's sandbox) | Deferred, unchanged | Infrastructure/environment limitation, not a code defect; not re-attempted in this correction (out of the four named findings' scope, and the original report already recorded it honestly as "evidence unavailable" rather than a pass/fail) |
| OBS3 (no true-concurrency tests existed) | Resolved as a byproduct | `test_idempotency_concurrency.py` is exactly this category of test; no longer a gap |

## 9. Database Verification

- Full clean-install cycle repeated after every schema-affecting change: drop
  `dcim`/`dcim_test`, recreate (owned by `postgres`, not `dcim_app`), `alembic upgrade
  head` (migrations `e9fd19228f19` → `0002_seed` → `0003_correction`, clean, no errors),
  `scripts/bootstrap_privileged_roles.sql` (superuser-run, idempotent — re-run
  successfully multiple times during this session).
- Full downgrade cycle: `alembic downgrade base` then `alembic upgrade head` again,
  clean, confirming `0003_correction`'s and the corrected `0002_seed`'s reversibility
  (with the pre-existing, documented limitation that reversing `0002_seed` in an
  environment where the bootstrap script has already run requires a superuser to
  manually reassign `audit_log`'s ownership back to `dcim_app` first — unchanged from
  before this correction, and consistent with `dcim_retention_admin`'s own creation
  already being outside Alembic's downgrade path).
- Privilege/ownership inspected directly via `pg_catalog`/`information_schema`
  (`pg_class.relowner`, `pg_database.datdba`, `pg_get_userbyid`), not ORM behavior or
  assumption, both before and after every fix.
- Constraint/trigger behavior verified via raw SQL against real rows (self-reference,
  2-cycle, 3-cycle, legitimate chain), not only through the ORM.

## 10. Security Verification

TRUNCATE/UPDATE/DELETE/ALTER/DROP all independently re-denied for `dcim_app` via a
genuine login connection, both on the parent `audit_log` table and on individual
partitions, both immediately after the fix and after a full database rebuild. No
privilege escalation path found in the corrected RBAC-adjacent code (RBAC itself was
not a target of this correction — no red-team finding named it). No secret/credential
leakage introduced by any new log line or error path (the new `IdempotencyStillProcessing`
503 and `IdempotencyConflict` 422 responses carry no internal detail beyond what the
original responses already did).

## 11. Reliability Verification

Idempotency: see §6. Stale-claim reclaim mirrors the existing, already-tested outbox
stale-processing pattern. Failed-write release verified explicitly. Outbox/audit
atomicity re-confirmed unaffected (`test_outbox.py`, unmodified, still passing).
Migration reversibility re-verified (§9). Celery task registration re-verified from a
genuinely clean, freshly-spawned worker OS process (not an in-process import) after the
`audit_partition_maintenance.py` change — all three tasks
(`ensure_future_partitions`, `dispatch_pending_outbox_events`, `process_outbox_event`)
correctly registered.

## 12. Test Results

76 passed, 0 failed (59 original + 17 new). `ruff check app tests`: clean. `mypy app`:
clean, 0 errors across 49 source files. Frontend (untouched by this correction, verified
unaffected): `tsc --noEmit` clean, `eslint` clean, `vite build` succeeds.

## 13. Migration Verification

`0003_correction_idempotency_and_replacement_integrity.py`: applies cleanly on top of
`0002_seed` from a clean database; `downgrade()` reverses the idempotency column changes
and the M2/M3 constraint/trigger cleanly. Its `TRUNCATE` revoke line and its
`downgrade()`'s corresponding `GRANT` share the same pre-existing limitation as
`0002_seed`'s audit grants once the bootstrap script has transferred ownership (documented
in the migration's own downgrade comment, not hidden).

`0002_audit_partitions_retention_and_rbac_seed.py`: `downgrade()` now additionally drops
the monthly partitions it created (Finding L1), verified by inspecting `pg_class`
immediately after the downgrade step — only the bare `audit_log` parent remained, no
leftover `audit_log_20*` tables.

## 14. Documentation Changes

`PHASE1_IMPLEMENTATION.md` (§7 Audit, the §2/§4 Idempotency and Identity/Replacement
rows, §13 test count — all updated to describe the corrected implementation, with an
explicit correction notice), `PHASE1_IMPLEMENTATION_REPORT.md` (correction notice added
at the top; body preserved as originally written, not rewritten),
`PHASE1_TRACEABILITY_MATRIX.md` (Audit, Idempotency, and `ManagedAsset.id` immutability
rows updated with corrected status and an explanatory note column), `PHASE1_DEVIATIONS.md`
(D4 wording corrected; D5/D6 added for the two documentation-only findings; a note added
clarifying C1/H1 were implementation defects, not deviations, and are covered in this
report instead).

## 15. Remaining Deviations

Unchanged from `PHASE1_DEVIATIONS.md` D1–D3 (Docker Compose runtime unvalidated — no
Docker daemon in this session, though `docker-compose.yml` and the new
`backend/scripts/docker-initdb/01-create-app-role.sh` were statically reviewed and are
internally consistent; no application metrics infrastructure; the v1.0 schema
documentation gap). D4 restated with corrected wording (D5) plus one new documentation
addition (D6) — see §8/§14 above. No new *implementation* deviations were introduced by
this correction.

## 16. Evidence Limitations

- Docker Compose changes (`docker-compose.yml`, `backend/scripts/docker-initdb/`) are
  **static edits only, not runtime-validated** — no Docker daemon is available in this
  session (unchanged from the existing D1 limitation). They mechanically mirror the
  independently-verified non-Docker local setup and CI's role-provisioning steps, but
  `docker compose up` itself was not run.
- The Redis-failure evidence gap the red-team recorded (OBS2) was not re-attempted in
  this correction session; it remains "evidence unavailable," not a pass or fail.
- The `SET ROLE`-under-superuser testing artifact described in §5 (which initially
  produced misleading, inconsistent-looking results during manual ad-hoc verification of
  the database-ownership finding) affected only this session's own interactive debugging
  — it never affected the automated pytest suite, which connects as `dcim_app` directly
  throughout and was the actual basis for every "PASS" claim in this report.
- No load/performance testing was performed; the concurrency tests validate
  correctness under true concurrency (10 and 20 simultaneous requests), not throughput
  or behavior at production scale.

## 17. Phase 2 Readiness Assessment

Not assessed here — that determination belongs to the next independent red-team
validation gate, not to this correction's own author. What can be said factually: every
named finding (C1, H1, M2, M3) now has a corrected implementation, a passing regression
test that would have failed on the original commit, and empirical evidence gathered
independently of the original implementation's own claims (direct `pg_catalog`
inspection, genuine login connections, a separate live process for the concurrency
fix) — the same evidentiary standard the red-team itself required.

## 18. Final Verdict

**PHASE 1 CORRECTIONS COMPLETE — READY FOR FINAL RED-TEAM VALIDATION**

---

STOP — PHASE 1 TARGETED CORRECTIONS COMPLETE — WAITING FOR INDEPENDENT RED-TEAM RE-VALIDATION
