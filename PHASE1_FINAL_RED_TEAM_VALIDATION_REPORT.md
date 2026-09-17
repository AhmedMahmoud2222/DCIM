# Phase 1 Final Red-Team Validation Report

## 1. Executive Summary

This is an independent validation of the targeted correction commit
`2bbf0a522144c72c607a5be5b838f00a47d6210d`, which claimed to close CRITICAL finding C1
(audit log not append-only), HIGH finding H1 (idempotency incorrect under concurrency),
and MEDIUM findings M2/M3 (asset-replacement self-reference/cycle). No source code,
migration, schema, Docker, or CI file was modified during this validation — every claim
below was independently re-derived against real PostgreSQL 16, using genuine `dcim_app`
**login** connections (not a superuser session with `SET ROLE`, which was shown in the
correction's own report to mask database-ownership behavior), a completely fresh,
independently-created database, and a separately-launched `uvicorn` process.

**Result: C1, H1, M2, and M3 are genuinely closed.** Every attack from the required
matrix — TRUNCATE, DELETE, UPDATE, ALTER, DROP (parent and partition), ownership
takeover, GRANT/REVOKE self-service, role escalation, SECURITY DEFINER injection/hijack,
self-reference, 2/4-node replacement cycles — was independently attempted against a
genuine `dcim_app` connection and correctly denied, both on the existing database and on
a from-scratch clean install. The idempotency fix was independently re-verified with 5
repeated trials (3×10, 2×20 concurrent requests) against a live, separate process,
100% correct each time.

Two new issues were found during this validation that the correction did not address
(neither was claimed as closed, so neither is a regression against the correction's own
claims):

- **NEW-1 (MEDIUM):** a narrow residual race in the idempotency stale-claim mechanism —
  if an original claim owner is slow enough to exceed the 30-second stale timeout while
  a second identical request reclaims and completes the operation, the original owner's
  eventual completion attempt receives an incorrect `409` instead of the correct
  idempotent replay (no data corruption; the reclaiming caller's response is correct).
- **NEW-2 (LOW):** migration `0003_correction`'s `managed_asset` self-reference `CHECK`
  constraint is created in the database as `ck_managed_asset_ck_managed_asset_no_self_replacement`
  (a doubled naming-convention prefix), not `ck_managed_asset_no_self_replacement` as the
  ORM model declares. Cosmetic only — verified the constraint still functions correctly
  and the mismatch does not currently produce Alembic autogenerate drift — but it is a
  real inconsistency between declared and actual schema state.

One additional, pre-existing functional observation (not introduced by the correction,
not a security issue) is recorded in §16.

No CRITICAL or HIGH finding remains open. No previous finding regressed.

## 2. Commit Under Test

`2bbf0a522144c72c607a5be5b838f00a47d6210d` ("fix: close Phase 1 red-team findings")

`git status --short`: clean, both before and after this validation. No file was modified
by this validation session.

## 3. Environment

| Component | Version |
|---|---|
| Python | 3.11.15 |
| Node | v22.22.2 |
| PostgreSQL | 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1) |
| Redis | 7.0.15 |
| OS | Linux 6.18.44, x86_64 |

PostgreSQL and Redis were not running at the start of this validation session (sandbox
restart between conversation turns, consistent with prior sessions in this repository's
history) and were restarted; the on-disk `dcim`/`dcim_test` databases and role state
survived the restart intact.

## 4. Validation Scope

Independent re-derivation of every claim in `PHASE1_CORRECTION_REPORT.md`: PostgreSQL
ownership/ACL state, the audit attack matrix, the `SECURITY DEFINER` partition function,
future-partition behavior, database-owner separation, idempotency concurrency and
failure/race scenarios, replacement self-reference/cycle enforcement, full migration
clean-install/upgrade/downgrade cycle, RBAC/CORS/outbox regression spot checks, and a
static review of the Docker/CI privilege model (no Docker daemon available — marked
`UNAVAILABLE` for runtime, static-only).

## 5. Previous Findings Revalidation

| Finding | Previous Severity | Previous Finding | Validation Method | Result | Evidence |
|---|---|---|---|---|---|
| C1 | CRITICAL | audit_log not append-only (TRUNCATE + owner DDL + DB-owner DROP all open) | TESTED — genuine `dcim_app` login, existing DB + fresh clean install | **CLOSED** | §6, §10 |
| H1 | HIGH | Idempotency incorrect under true concurrency | TESTED — 5 repeated trials (10/20 concurrent) against live separate process | **CLOSED** (with NEW-1 residual edge case, see §7) | §7 |
| M2 | MEDIUM | Replacement self-reference | TESTED — genuine login, INSERT + UPDATE paths, existing DB + fresh install | **CLOSED** | §8 |
| M3 | MEDIUM | Replacement cycles | TESTED — 2-cycle and 4-node cycle, INSERT + UPDATE paths | **CLOSED** | §9 |
| M1 | MEDIUM | Oversized input → 500 | TESTED — live API, `asset_tag` of 100,000 chars | **CLOSED** — now `422` | §16 |
| L1 | LOW | Migration 0002 downgrade left monthly partitions behind | TESTED — fresh DB, `alembic upgrade 0002_seed` then `downgrade -1`, inspected `pg_class` | **CLOSED** — only bare `audit_log` remains | §12 |
| L3 | LOW | `/health/ready` returned 200 while degraded | TESTED — live server, Redis stopped, checked HTTP status | **CLOSED** — now `503` | §16 |
| L2 | LOW (doc) | Deviations doc severity wording | STATICALLY VERIFIED — `PHASE1_DEVIATIONS.md` D5 | **CLOSED** | §16 |
| L4 | LOW (doc) | Undocumented pip-audit finding | STATICALLY VERIFIED — `PHASE1_DEVIATIONS.md` D6 | **CLOSED** | §16 |
| OBS1 | OBSERVATION | No CSP/HSTS headers | STATICALLY VERIFIED | **NOT CLOSED, unchanged** — correctly deferred, not required by Phase 1 scope | §16 |
| OBS2 | OBSERVATION | Redis-failure evidence unavailable in original red-team's sandbox | TESTED — this session could stop/start Redis; conclusively confirmed `503` | **CLOSED** (evidence gap resolved, behavior correct) | §16 |
| OBS3 | OBSERVATION | No true-concurrency tests existed | STATICALLY VERIFIED — `test_idempotency_concurrency.py`, 5 tests | **CLOSED** | §7 |

## 6. C1 Audit Immutability Validation

### PostgreSQL ownership/ACL (existing `dcim` database, independently queried)

```sql
SELECT datname, pg_get_userbyid(datdba) FROM pg_database WHERE datname='dcim';
--  dcim | postgres

SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public';
--  public | pg_database_owner    (i.e. postgres — NOT dcim_app)

SELECT relname, pg_get_userbyid(relowner), relacl FROM pg_class WHERE relname LIKE 'audit_log%';
--  audit_log and all 3 monthly partitions + default: owner = dcim_retention_admin
--  audit_log.relacl: dcim_app=ar/dcim_retention_admin  (SELECT+INSERT only)

SELECT r.rolname, g.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
  JOIN pg_roles g ON g.oid=m.roleid WHERE r.rolname='dcim_app';
--  (0 rows) — dcim_app is not a member of any other role
```

### Attack matrix (genuine `dcim_app` login connection — `psql "postgresql://dcim_app@localhost:5432/dcim"`)

| Attack | Result |
|---|---|
| `DELETE FROM audit_log` | `ERROR: permission denied for table audit_log` |
| `TRUNCATE TABLE audit_log` | `ERROR: permission denied for table audit_log` |
| `TRUNCATE TABLE audit_log_2026_09` (partition) | `ERROR: permission denied for table audit_log_2026_09` |
| `DROP TABLE audit_log_2026_09` | `ERROR: must be owner of table audit_log_2026_09` |
| `DROP TABLE audit_log_default` | `ERROR: must be owner of table audit_log_default` |
| `DROP TABLE audit_log [CASCADE]` | `ERROR: must be owner of table audit_log` |
| `ALTER TABLE audit_log ADD COLUMN ...` | `ERROR: must be owner of table audit_log` |
| `ALTER TABLE audit_log_2026_09 ADD COLUMN ...` | `ERROR: must be owner of table audit_log_2026_09` |
| `UPDATE audit_log SET action='tampered'` | `ERROR: permission denied for table audit_log` |
| `ALTER TABLE audit_log OWNER TO dcim_app` | `ERROR: must be owner of table audit_log` |
| `GRANT ALL ON audit_log TO dcim_app` | `WARNING: no privileges were granted` (no-op; dcim_app has no grant option) |
| `REVOKE SELECT ON audit_log FROM dcim_retention_admin` | `WARNING: no privileges could be revoked` (no-op) |
| `ALTER FUNCTION dcim_create_audit_log_partition(...) OWNER TO dcim_app` | `ERROR: must be owner of function` |
| `GRANT EXECUTE ON FUNCTION dcim_create_audit_log_partition(...) TO PUBLIC` | `WARNING: no privileges were granted` (no-op) |
| `INSERT INTO audit_log (...) VALUES (...)` (legitimate) | Succeeds |
| `SELECT ... FROM audit_log` (legitimate) | Succeeds |

Every destructive/escalation attempt failed; both legitimate operations succeeded. The
`GRANT`/`REVOKE` self-service attempts are worth noting explicitly: PostgreSQL allowed
the *statement* to run (no syntax/permission error on the GRANT/REVOKE command itself)
but performed no actual privilege change, because `dcim_app` holds no `WITH GRANT
OPTION` on anything and is not the owner — this is the correct, safe PostgreSQL
behavior, not a bypass.

### Broader escalation attempts (genuine `dcim_app` login)

```
CREATE ROLE sneaky SUPERUSER;              → permission denied to create role
ALTER ROLE dcim_app SUPERUSER;             → permission denied to alter role
ALTER DATABASE dcim OWNER TO dcim_app;     → must be owner of database dcim
GRANT dcim_retention_admin TO dcim_app;    → permission denied to grant role
COPY audit_log TO PROGRAM 'cat > /tmp/x';  → permission denied to COPY to/from an external program
SET ROLE dcim_retention_admin;             → permission denied to set role "dcim_retention_admin"
```

No path to privilege escalation found.

## 7. H1 Idempotency Concurrency Validation

### Repeated true-concurrency trials (separate live `uvicorn` process, port 8000)

```
RUN1-10req: codes=[201]*10  distinct_ids=1
RUN2-10req: codes=[201]*10  distinct_ids=1
RUN3-10req: codes=[201]*10  distinct_ids=1
RUN1-20req: codes=[201]*20  distinct_ids=1
RUN2-20req: codes=[201]*20  distinct_ids=1
```

5/5 trials: 100% success rate, exactly one resource created each time. This directly
contradicts the original H1 evidence (`[201,409,409,409,409,409,409,409,409,201]` for 10
requests) — the fix holds under repeated, independent re-testing.

Existing regression suite (`tests/integration/test_idempotency_concurrency.py`, 5
tests: 10-concurrent, 20-concurrent, conflicting-payload, failed-claim-release,
stale-claim-reclaim) re-run independently: all pass.

### NEW-1: original-owner-resumes-after-reclaim race (MEDIUM)

The required test matrix explicitly calls for "original owner resumes after another
caller reclaimed the key" (§10, scenario 6). This was tested by replaying the exact SQL
sequence the application issues, interleaved in the dangerous order, as genuine
`dcim_app`:

1. Session A claims key K (`INSERT ... status='processing'`, backdated 40s old to
   simulate elapsed time), committed.
2. Session B reclaims the now-stale claim (`UPDATE ... WHERE status='processing' AND
   updated_at < now()-30s`), succeeds, committed.
3. Session B performs the write (`INSERT INTO managed_asset ...`) and completes the
   claim (`UPDATE idempotency_key SET status='completed', response_body=...`).
4. Session A "wakes up" and attempts its own write with the identical body (guaranteed
   identical, since two requests sharing a key must have matching `request_hash` to
   reach this point at all) → `ERROR: duplicate key value violates unique constraint
   "uq_managed_asset_asset_tag"` (A's write correctly fails — no duplicate row).
5. Session A's failure-path cleanup, `DELETE FROM idempotency_key WHERE id=K AND
   status='processing'`, affects **0 rows** (status is now `'completed'`, not
   `'processing'`) — so B's completed record is correctly *not* deleted or corrupted.
6. Final state: exactly one `managed_asset` row (B's, correct); `idempotency_key` row
   intact with B's correct `201` response.

**Impact:** No data corruption and no duplicate resource — the data-integrity property
holds. But session A's own exception (the `IntegrityError` from step 4) propagates
unhandled to A's original HTTP caller, which receives a generic `409 Conflict` via the
global exception handler, not the idempotent replay of B's successful `201`. This is the
same *class* of defect as the original H1 (a caller that should see success sees an
error instead) but with a much narrower trigger: it requires a request slow enough to
exceed the 30-second `STALE_CLAIM_TIMEOUT` while a second identical request is also in
flight and reclaims. Ordinary concurrency (the scenario H1 was about, and which is now
proven correct above) is unaffected.

**Root cause:** `release_claim()`'s `DELETE ... WHERE status = 'processing'` correctly
avoids destroying a reclaimer's completed record, but there is no corresponding path for
the original owner to detect it has been superseded and replay the reclaimer's result
instead of re-attempting (and failing) its own write.

**Recommended remediation direction (no code):** before treating an `IntegrityError` (or
any failure) in the write path as fatal, re-check whether the claim row this caller
believes it owns is still in `'processing'` status and still associated with this
caller's own attempt (e.g., a generation/fencing token bumped by the reclaim) — if it
has moved to `'completed'` under a different claimant, replay that response instead of
propagating the error.

**Phase impact:** Not blocking — narrow trigger window, no data-integrity impact, and
the fix direction is well-scoped and consistent with the existing design.

## 8. M2 Replacement Self-Reference Validation

Genuine `dcim_app` login, both paths, both on the existing `dcim` database and
independently on a fresh clean-install database (`dcim_val`):

```sql
UPDATE managed_asset SET replaces_asset_id = id WHERE ...;
-- ERROR: managed_asset <id> cannot replace itself

INSERT INTO managed_asset (id, ..., replaces_asset_id) VALUES ('<id>', ..., '<id>');
-- ERROR: managed_asset <id> cannot replace itself
```

Both the UPDATE and INSERT paths are blocked, consistently, on two independent
databases. No API endpoint exists for asset replacement in Phase 1 (confirmed by
`grep -rn replaces_asset_id backend/app/api/v1/*.py` returning nothing outside the
model file), matching the documented deferral of the `ReplaceAsset` workflow — the
database-level invariant is what protects this column today, and it holds.

## 9. M3 Replacement Cycle Validation

Beyond the original 2-cycle and 3-cycle tests, this validation additionally constructed
a 4-node cycle (W→X→Y→Z, then attempted to close it W←Z):

```sql
-- W replaces nothing; X replaces W; Y replaces X; Z replaces Y  (all succeed — valid chain)
UPDATE managed_asset SET replaces_asset_id = (SELECT id FROM managed_asset WHERE asset_tag='M3-Z')
  WHERE asset_tag='M3-W';
-- ERROR: managed_asset <id> replacement would create a cycle
```

Also verified:
- Setting `replaces_asset_id = NULL` (clearing a replacement) is always permitted — the
  trigger's `IF NEW.replaces_asset_id IS NULL THEN RETURN NEW; END IF;` guard is correct.
- A legitimate non-cyclic chain (an asset replacing the just-cleared node) succeeds
  normally after the NULL clear.
- Both 2-cycle and 4-cycle attempts were rejected via the `UPDATE` path specifically
  (not just `INSERT`), confirming the `BEFORE INSERT OR UPDATE OF replaces_asset_id`
  trigger scope is correct.

No bypass found for arbitrary-length cycles.

## 10. PostgreSQL Ownership & ACL Analysis

See §6 for the primary evidence. Additional analysis: `default privileges`
(`pg_default_acl`) returned zero rows — no default-privilege leakage that would
auto-grant future objects to unintended roles. `public` schema ACL:
`dcim_app=UC/pg_database_owner, dcim_retention_admin=UC/pg_database_owner` — both roles
have `CREATE, USAGE` on `public` and nothing else at the schema level, consistent with
the documented design (dcim_app needs CREATE to run migrations; it does not have it at
the database level, only the schema level, which is the intended, narrower grant).

One functional (not security) observation: `dcim_retention_admin` — the *owner* of
`audit_log` — itself has no `UPDATE`/`DELETE` ACL entry on the table (`relacl` shows
`dcim_retention_admin=arxt`, i.e. SELECT/INSERT/REFERENCES/TRIGGER only, missing
UPDATE/DELETE/TRUNCATE). Verified via `SET ROLE dcim_retention_admin; DELETE FROM
audit_log WHERE false;` → `permission denied for table audit_log`, even for the table's
own owner. `TRUNCATE` and `ALTER` *do* still work for `dcim_retention_admin` (owner
default, never explicitly revoked from it), confirming this is not a broad
ownership-model bug — only `UPDATE`/`DELETE` are affected, most likely inherited
artifacts of migration `0002`'s `REVOKE UPDATE, DELETE ... FROM dcim_app` having been
issued while `dcim_app` was still the table's owner-at-the-time. **This means the
privileged retention role cannot currently perform row-level retention/redaction
operations (e.g., deleting rows past a retention period) without an additional
explicit `GRANT`.** Not exploitable by `dcim_app` (if anything, it is more restrictive
than intended) and no code path currently calls for `dcim_retention_admin` to
UPDATE/DELETE rows (Phase 1's retention duration is an open, non-blocking
organizational decision per `PHASE1_IMPLEMENTATION.md` §14), so this is recorded as an
OBSERVATION for whichever future phase implements retention, not a Phase 1 defect.

## 11. SECURITY DEFINER Analysis

`dcim_create_audit_log_partition(text, date, date)`:
- Owner: `dcim_retention_admin`. `SECURITY DEFINER`: true. `proconfig`:
  `{search_path=public}` — baked into the function definition itself (confirmed via
  `\sf`), not merely a session-level setting, so it cannot be bypassed by the caller's
  own `search_path`.
- `proacl`: `dcim_retention_admin=X/dcim_retention_admin, dcim_app=X/dcim_retention_admin`
  — EXECUTE granted only to these two roles; PUBLIC has no access (confirmed by the
  `GRANT EXECUTE ... TO PUBLIC` self-service attempt in §6 being a no-op for `dcim_app`,
  which lacks grant option).
- Input validation: `partition_name !~ '^audit_log_[0-9]{4}_[0-9]{2}$'` rejects anything
  not matching the expected pattern *before* any dynamic SQL executes. Tested directly:
  `dcim_create_audit_log_partition('foo; DROP TABLE managed_asset; --', ...)` and a
  quote-breaking variant (`'audit_log_9999_99''; DROP TABLE managed_asset; --'`) were
  both rejected by the regex with `invalid audit_log partition name: ...`, before
  reaching `EXECUTE format(...)`. `managed_asset` row count confirmed unaffected.
- The dynamic SQL itself uses `%I` (identifier quoting) for the partition name and `%L`
  (literal quoting) for the date bounds, and the date parameters are already
  type-coerced to `date` by PostgreSQL's function-parameter binding before the function
  body ever sees them (no string concatenation of user-controlled date text).
- The target table (`audit_log`) is hardcoded in the function body, not parameterized —
  the function cannot be redirected to attach a partition to any other table.
- Search-path hijack attempt: `dcim_app` could not even `CREATE SCHEMA` (`permission
  denied for database dcim` — no database-level CREATE), so the classic "shadow object
  in an attacker-controlled schema earlier in search_path" setup could not be
  constructed at all; and even if it could, the function's own baked-in `search_path` is
  the actual defense, independent of that.
- Legitimate operation re-verified: a new partition created via the function is owned by
  `dcim_retention_admin` (not the caller), and — tested directly — `dcim_app` cannot
  `TRUNCATE`, `DROP`, or directly `SELECT` the new partition by name (only through the
  parent, which correctly routes `INSERT`s to it).

No privilege-escalation, injection, or search-path-hijack path found in this function.

## 12. Migration Validation

**Clean install** (fresh `dcim_val` database, never touched by any prior migration):
`alembic upgrade head` ran cleanly through all three revisions
(`e9fd19228f19` → `0002_seed` → `0003_correction`); `scripts/bootstrap_privileged_roles.sql`
ran cleanly and idempotently; the full C1/M2 attack matrix (§6, §8) was independently
re-run against this fresh database with identical results to the existing `dcim`
database.

**Downgrade path (L1 focus):** on a fresh database *before* bootstrap has run (so
`dcim_app` still owns `audit_log` at downgrade time — the scenario where L1 actually
applies), `alembic downgrade -1` from `0002_seed` correctly drops all three monthly
partitions and the default partition, leaving only the bare `audit_log` parent table —
confirmed via direct `pg_class` inspection. This is a genuine, independent
re-verification of the L1 fix, not a re-run of the correction's own test.

**Downgrade path (post-bootstrap):** on a database where bootstrap *has* run (matching
this validation's primary `dcim` and the freshly-migrated `dcim_val`), `alembic
downgrade -1` from `0003_correction` to `0002_seed` succeeds (no ownership dependency),
but downgrading further (`0002_seed` → `e9fd19228f19`) fails with `must be owner of
table audit_log_2026_09` — exactly the documented, disclosed limitation in
`PHASE1_CORRECTION_REPORT.md` §9 and the migration's own downgrade comment, reproduced
independently here. Alembic's transactional DDL correctly rolled back the failed
statement, leaving `alembic_version` accurately at `0002_seed` (not a corrupted
intermediate state) — a subsequent `alembic upgrade head` succeeded cleanly. This is
**CONFIRMED AS DOCUMENTED**, not a new finding.

**NEW-2: constraint naming double-prefix (LOW).** `op.create_check_constraint("ck_managed_asset_no_self_replacement", "managed_asset", ...)`
in migration `0003_correction` actually creates a constraint named
`ck_managed_asset_ck_managed_asset_no_self_replacement` in the database (confirmed via
`SELECT conname FROM pg_constraint WHERE conrelid='managed_asset'::regclass AND
contype='c'` on two independent databases) — the project's bound `MetaData`
naming convention (`ck_%(table_name)s_%(constraint_name)s`) is applied a second time on
top of the already-conventional name passed to `op.create_check_constraint`, producing
the double prefix. This does not match the ORM model's declared name
(`ck_managed_asset_no_self_replacement`, from `CheckConstraint(..., name="no_self_replacement")`
+ naming convention). **Verified this has no current functional impact:**
`alembic revision --autogenerate` against the corrected schema produces no diff
mentioning this constraint (SQLAlchemy/Alembic's CHECK-constraint comparison did not
flag the name mismatch in this configuration), and the migration's own `downgrade()`
(`op.drop_constraint("ck_managed_asset_no_self_replacement", ...)`) succeeds because the
same doubling is applied consistently to the drop call too — verified by a full
downgrade/upgrade round-trip removing and recreating the constraint correctly. Impact is
limited to schema-inspection confusion (the name a DBA sees in `\d managed_asset` does
not match what the codebase's model declares) and latent risk if a future Alembic/
SQLAlchemy version changes CHECK-constraint comparison behavior.

**Upgrade path from pre-correction state:** `alembic upgrade 0002_seed` (simulating the
original, pre-correction Phase 1 state) followed by `alembic upgrade head` applies
`0003_correction` cleanly on top of realistic pre-existing schema state, no manual
intervention required.

## 13. Security Regression Review

RBAC/authentication/CORS were not targets of the correction (no red-team finding named
them) and were spot-checked, not exhaustively re-audited: unauthenticated request to
`/api/v1/managed-assets` → `401` (unchanged); CORS does not reflect an untrusted origin
(`Origin: https://evil.example.com` → no `Access-Control-Allow-Origin` header)
(unchanged). `tests/api/test_security.py`, `tests/api/test_locations.py`,
`tests/integration/test_outbox.py` (20 tests) re-run independently, all pass — no
regression in areas the correction did not touch.

The two new `IdempotencyConflict`/`IdempotencyStillProcessing` error responses were
inspected for information disclosure: both carry only the same generic
`title`/`detail`/`request_id` shape every other `ApiError` in this codebase uses,
nothing internal.

## 14. Docker/CI Review

**Static only — no Docker daemon available in this session (marked UNAVAILABLE for
runtime).** `docker-compose.yml`: `POSTGRES_USER` is no longer set (defaults to the
official image's `postgres` superuser); every application service
(`backend`/`migrate`/`celery-worker`/`celery-beat`) connects via `DATABASE_URL` using
`dcim_app:${DCIM_APP_PASSWORD}`, a distinct credential from `${POSTGRES_PASSWORD}` (the
superuser's); the new `bootstrap-privileges` service explicitly runs as `PGUSER:
postgres`. `.github/workflows/ci.yml`: the "Provision least-privilege dcim_app role and
database" step creates `dcim_app` as an ordinary role and `CREATE DATABASE dcim_test;`
(no `OWNER dcim_app`), then explicitly `GRANT CREATE, USAGE ON SCHEMA public TO
dcim_app` — matching exactly the sequence this validation independently exercised
against `dcim_val`/`dcim_l1` and confirmed produces the correct privilege boundary.
`.env.example` correctly documents two distinct passwords and warns against reusing
them. No file grants `dcim_app` superuser, `CREATEROLE`, `CREATEDB`, or database
ownership anywhere in the reviewed configuration.

## 15. Test Results

`pytest -q` (backend, `dcim_test`): **76 passed, 0 failed, 0 skipped, 0 errors.**
`ruff check app tests`: clean. `mypy app`: clean, 0 errors across 49 source files.
Frontend (untouched by the correction): `tsc --noEmit` clean, `eslint . --ext ts,tsx`
clean.

Every test in the suite that asserts a security/correctness property was checked for
whether it actually proves that property (per §19 instructions) — none were found to be
tautological or to pass without exercising real behavior; the two categories the
original red-team specifically flagged as structurally unable to catch C1/H1 (no
`TRUNCATE`/DDL attempts, no true-concurrency requests) are now covered by dedicated
tests that fail against the pre-correction code path (independently confirmed by this
validation's own re-derivation of the same attacks outside pytest, in §6 and §7).

## 16. New Findings

**NEW-1 (MEDIUM)** — idempotency: original claim owner resuming after a stale-timeout
reclaim receives an incorrect `409` instead of the reclaimer's successful result. See §7
for full detail, reproduction, and evidence.

**NEW-2 (LOW)** — migration: `managed_asset` self-reference `CHECK` constraint's actual
database name (`ck_managed_asset_ck_managed_asset_no_self_replacement`) does not match
the ORM model's declared name (`ck_managed_asset_no_self_replacement`), due to the
project's naming convention being applied twice by `op.create_check_constraint`. See
§12.

**OBSERVATION** — `dcim_retention_admin` cannot itself `UPDATE`/`DELETE` `audit_log`
despite owning it (only `TRUNCATE`/`ALTER` work for the owner by default); a future
retention/redaction feature will need an explicit additional `GRANT`. See §10. Not a
Phase 1 defect (no code path needs this today).

**CLOSED-AND-RECONFIRMED** — M1 (oversized input → 500, now `422`), L1 (migration
downgrade completeness), L3 (readiness status code), L2/L4 (documentation), OBS2 (Redis
failure now conclusively testable and correct) — see §5 table and §12/§16 evidence.

No CRITICAL or HIGH finding was discovered. No previously-closed finding regressed.

## 17. Risk Assessment

| Item | Severity | Blocks Phase 2? |
|---|---|---|
| NEW-1 (idempotency stale-reclaim race) | MEDIUM | No — narrow trigger (30s+ stall + concurrent duplicate), no data corruption, well-scoped fix direction exists |
| NEW-2 (constraint naming double-prefix) | LOW | No — cosmetic, no functional impact confirmed |
| dcim_retention_admin UPDATE/DELETE gap | OBSERVATION | No — no current code path needs it |
| Docker Compose runtime unvalidated | Pre-existing (D1) | No — non-Docker path fully validated; static review of the corrected privilege model found no defect |

## 18. Evidence Classification

- **TESTED (executed):** §6 attack matrix (both databases), §7 concurrency trials and
  NEW-1 reproduction, §8/§9 self-reference/cycle attacks, §11 SECURITY DEFINER injection/
  hijack attempts, §12 clean-install/downgrade/upgrade cycles, §13 RBAC/CORS/outbox spot
  checks, §15 full test suite.
- **STATICALLY VERIFIED:** §11 function source (`\sf`) and ACL inspection, §14 Docker/CI
  YAML review, §16 documentation findings (L2/L4).
- **OBSERVED:** `git status`/`git log` baseline (§2), environment versions (§3).
- **INFERRED:** none material to the verdict — every load-bearing claim in this report
  was executed, not inferred.
- **UNAVAILABLE:** Docker Compose runtime behavior (`docker compose up` itself) — no
  Docker daemon in this session; static review only, consistent with pre-existing
  deviation D1.

## 19. Residual Risks / Observations

- NEW-1 and NEW-2 (above) remain open per this validation's own rule not to fix findings.
- Docker runtime remains unvalidated end-to-end (D1, pre-existing, unrelated to this
  correction's substance — the corrected privilege-separation logic was verified
  equivalently via direct database commands matching the Docker init script's exact
  sequence).
- No load/performance testing was performed at any point in this validation chain; the
  concurrency evidence establishes correctness under true concurrency (up to 20
  simultaneous requests), not throughput or production-scale behavior.
- The `dcim_retention_admin` UPDATE/DELETE gap (§10) should be addressed whenever a
  retention/redaction feature is designed, not before.

## 20. Final Verdict

**PHASE 1 IMPLEMENTATION APPROVED — PROCEED TO PHASE 2**

All CRITICAL (C1) and HIGH (H1) findings are independently confirmed closed, re-derived
against real PostgreSQL via genuine role connections rather than trusting the
correction's own claims. M2/M3 are closed with broader coverage than originally
required (4-node cycle, not just 2/3). Migration clean-install, upgrade, and downgrade
paths all behave correctly or match a disclosed, pre-existing limitation. No new
CRITICAL or HIGH issue was discovered; the two new findings (NEW-1 MEDIUM, NEW-2 LOW)
are narrow, well-understood, non-corrupting, and do not represent an architectural flaw
— they are exactly the kind of residual issue that is normal to carry forward and
address in the ordinary course of Phase 2 work, not a reason to hold Phase 1 in place.

## 21. Recommendation for Next Phase

Carry NEW-1 and NEW-2 forward as tracked, non-blocking defects (the correction report's
own §17 already anticipated this validation gate would be the authority on Phase 2
readiness, not the correction's author). When Phase 2 designs the `ReplaceAsset`
workflow, revisit the `dcim_retention_admin` UPDATE/DELETE gap (§10) if that work also
touches retention/redaction. No re-architecture, no repeat of this validation gate, is
warranted before proceeding.

---

## FINAL VERDICT

**PHASE 1 IMPLEMENTATION APPROVED — PROCEED TO PHASE 2**

### Blocking Findings

None.

### Non-Blocking Findings

- NEW-1 (MEDIUM) — idempotency stale-reclaim original-owner race (§7)
- NEW-2 (LOW) — `managed_asset` CHECK constraint naming double-prefix (§12)
- OBSERVATION — `dcim_retention_admin` cannot UPDATE/DELETE despite ownership (§10)
- OBSERVATION — Docker Compose runtime remains unvalidated end-to-end (pre-existing D1)

### Evidence Confidence

**High.** Every load-bearing claim (C1's full attack matrix, H1's concurrency behavior,
M2/M3's constraint enforcement) was independently executed against real PostgreSQL using
genuine role connections on two separate databases (the existing `dcim` and a
from-scratch clean install), plus a separately-launched live process for the
concurrency evidence. Nothing in the verdict rests on trusting the correction report's
own narrative — each of its claims was independently re-derived from first principles,
and the one place its own methodology had a documented blind spot (`SET ROLE` under a
superuser session masking database-ownership behavior) was avoided throughout by using
genuine login connections.

### Phase 2 Readiness

The evidence supports proceeding. The foundation's core integrity guarantees (audit
immutability, idempotent writes under concurrency, replacement-graph acyclicity) hold
under adversarial, independently-designed testing that went beyond the original
red-team's own test matrix (4-node cycles, repeated concurrency trials, a from-scratch
clean-install re-run of the entire attack matrix, and a specific reclaim-race scenario
the correction had not itself tested). The two new findings are real but narrow, with
clear, low-risk remediation directions already identified — appropriate to track and fix
during Phase 2, not to gate on.

---

STOP — FINAL PHASE 1 RED-TEAM VALIDATION COMPLETE — WAITING FOR HUMAN DECISION
