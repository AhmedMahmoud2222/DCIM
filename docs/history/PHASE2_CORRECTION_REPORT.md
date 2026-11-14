# PHASE2_CORRECTION_REPORT.md

Targeted remediation of the findings in `PHASE2_INDEPENDENT_RED_TEAM_REPORT.md`
(commit `a3a79fe`), performed as the implementation engineer responsible for
correction — not as an independent reviewer. This report does not itself constitute
independent validation; a separate red-team pass against the commit this report
accompanies is the next step.

## 29.1 Executive Summary

Two findings required action: **RT-1 (HIGH)**, a genuine concurrency defect that could
permanently break a floor plan's candidate-review workflow, and **RT-2 (MEDIUM)**, a
factually inaccurate "never touches disk" claim in documentation. Both are now closed.

RT-1 is fixed with a database-enforced partial unique index plus an
`INSERT ... ON CONFLICT ... DO NOTHING` atomic-claim pattern (reusing the same idiom
`app/application/idempotency.py` already used elsewhere), backed by row-level locking for
the adjacent same-candidate race. While validating this fix against a real running
Uvicorn server and, separately, a real browser session — steps the original pytest
suite alone did not require — a second, more subtle bug surfaced: the fix's initial form
compiled its `ON CONFLICT` predicate as a bound SQL parameter rather than a literal,
which PostgreSQL's query planner can silently fail to verify against a partial index
once it switches from a custom to a generic plan (typically after ~5 executions of the
same prepared statement on one connection) — a failure mode invisible to any single
pytest test or a short-lived live-server run, since neither reuses one connection enough
times to trigger it. That was root-caused and fixed in the same commit. This is exactly
the scenario the correction prompt's "No False Pass" requirement anticipated, and the
mandated live-server/browser validation step is what caught it.

RT-2 was resolved by correcting the documentation (Option A), not by re-engineering the
upload path (Option B) — the security property the original claim actually protected
(immunity to path traversal) does not depend on avoiding disk at all, and was never at
risk.

NEW-1 and NEW-2 are carried forward, unfixed, as the correction prompt directed. RT-3 is
carried forward as a non-blocking observation, since it is unrelated to any code path
this correction touches.

Full regression suite: 191/191 passing, 3 consecutive runs. Zero regressions. Lint and
type-check are at their pre-existing baseline error counts (no new violations). Frontend
build is clean. Migration 0005 validated across fresh install, downgrade, re-upgrade, and
a synthetic pre-existing-duplicate-data scenario (dedup logic verified to preserve every
`SpatialObject`, re-pointing rather than deleting).

## 29.2 RT-1 Remediation Detail

**Root cause.** `accept_import_candidate` (`app/api/v1/floor_plans.py`) needed the
canonical `layer_type='imported'` `SpatialLayer` for a floor plan, and previously did so
with an unlocked `SELECT`, falling back to `INSERT` if none was found. Two concurrent
requests accepting different candidates of the same import job could each run the
`SELECT`, each see no existing row, and each `INSERT` — producing two "Imported" layers
for one floor plan. Once duplicated, every subsequent accept for that floor plan hit
`sqlalchemy.exc.MultipleResultsFound` on the next `scalar_one_or_none()`-adjacent lookup,
surfacing as a permanent, unrecoverable 500 — not a transient blip.

**Investigation before design.** `SpatialLayer` (`app/domain/spatial/models.py`) has no
existing uniqueness constraint on `(floor_plan_id, layer_type)`; a `grep` confirmed
`accept_import_candidate` is the *only* code path anywhere in this codebase that ever
creates a `SpatialLayer` row, and only ever with `layer_type="imported"` — no other
`layer_type` value has a creation path at all. This scoped the fix precisely: a partial
unique index on `(floor_plan_id) WHERE layer_type = 'imported'`, not a broader constraint
this codebase has no basis to assert.

**Migration `0005_correction_spatial_layer_race.py`.**
1. Deduplicates any pre-existing violating rows first (a database that already hit the
   race — this repository's own red-team validation reproduced it live — may already
   contain duplicates): the oldest row per `floor_plan_id` (by `created_at`, then `id`)
   is canonical; every `SpatialObject` pointing at a newer duplicate is re-pointed to the
   canonical layer via `UPDATE ... FROM`; the now-unreferenced duplicate layer rows are
   deleted. No `SpatialObject` is ever deleted or orphaned.
2. Adds `CREATE UNIQUE INDEX uq_spatial_layer_one_imported_per_floor_plan ON spatial_layer
   (floor_plan_id) WHERE layer_type = 'imported'` — a raw partial index, not a
   `UNIQUE` table constraint, because PostgreSQL does not support `WHERE`-clause
   predicates on table-level `UNIQUE` constraints (the same reason migration 0004's own
   `uq_floor_plan_one_active_per_room` is a raw index).
3. `downgrade()` drops the index only; the deduplication is a one-way data-quality fix,
   not a reversible schema change (same precedent as migration 0004's own downgrade).

**Application fix.** `_get_or_create_imported_layer` (new helper in
`app/api/v1/floor_plans.py`) replaces the unlocked lookup with:
```python
pg_insert(table)
    .values(id=..., floor_plan_id=floor_plan_id, name="Imported", layer_type="imported", z_order=0)
    .on_conflict_do_nothing(
        index_elements=[table.c.floor_plan_id],
        index_where=text("layer_type = 'imported'"),
    )
    .returning(table.c.id)
```
followed by a plain re-`SELECT` if no row was returned (guaranteed to find the canonical
row, since nothing in this codebase ever deletes a `SpatialLayer`). This is the same
INSERT-as-atomic-claim pattern `app/application/idempotency.py` already uses — not a new
mechanism. `index_elements` + `index_where` (index inference), not `constraint=...`,
because PostgreSQL's `ON CONFLICT ON CONSTRAINT` clause only matches a true catalogued
constraint, and a partial unique index can never be expressed as one.

Both `accept_import_candidate` and `reject_import_candidate` additionally now look up the
candidate row with `SELECT ... FOR UPDATE` (the same locked-row idiom
`app/application/placement_service.py` already uses), closing the adjacent race where two
concurrent requests target the *same* candidate — the correction prompt's §7 named this
in-scope alongside the original different-candidates race.

**The sub-finding found during this correction's own validation.** The first working
version of the fix above used `index_where=(table.c.layer_type == "imported")` — a Python
comparison SQLAlchemy compiles to a bound parameter (`WHERE layer_type = $7::VARCHAR`),
confirmed by direct inspection of the compiled SQL. This passed every pytest run,
including the 20-way-concurrency regression tests below, and an initial live-Uvicorn
acceptance-test run (2 fresh datasets, zero failures). It was only the mandated
real-browser validation — which happened to run enough requests through the same
long-lived server process to push individual pooled connections past PostgreSQL's
~5-execution custom-to-generic prepared-statement-plan threshold — that surfaced
`sqlalchemy.exc.ProgrammingError: ... InvalidColumnReferenceError: there is no unique or
exclusion constraint matching the ON CONFLICT specification` for a large fraction of
concurrent accepts. A generic plan cannot statically verify a parameter's runtime value
against a partial index's predicate, so arbiter inference fails once a connection's
prepared statement is replanned generically — a failure mode that is deterministic per
connection-reuse-count, not a data race, and therefore invisible to any test that doesn't
reuse one physical connection enough times.

**Root-cause fix:** `index_where=text("layer_type = 'imported'")` — a genuine SQL literal
with no parameter for a generic plan to fail to verify. Confirmed via direct compiled-SQL
inspection that the parameter is gone entirely from the compiled statement. Reconfirmed
stable across 3 consecutive acceptance-test runs (6 datasets) against the same
long-lived server process — deliberately re-using the exact condition that exposed the
bug — with zero failures.

**Audit/outbox atomicity.** `_get_or_create_imported_layer` participates in the same
`AsyncSession`/transaction as the rest of `accept_import_candidate`; nothing about this
fix introduces a second commit, a second event-emission mechanism, or a window where a
`SpatialObject` could be created without (or duplicated with) its corresponding audit log
entry or outbox event. No change was made to `write_audit_log`/`write_outbox_event`
call sites or ordering.

**Observability.** The existing structured-logging/correlation-ID middleware
(`app/core/correlation.py`) already attaches a `correlation_id`/`request_id` to every
request and to the `unhandled_exception` log event; no new logging was added, since the
fix's success path produces no error to log, and its failure paths (409 conflicts) are
already ordinary, logged API responses — not exceptional events warranting new
instrumentation. `floor_plan_id`, `candidate_id`, and the operation name are already
present on the request path (URL parameters) and in the eventual audit log entry.

## 29.3 RT-2 Remediation Detail

**Investigation.** `app/application/svg_sanitizer.py`'s docstring and
`PHASE2_IMPLEMENTATION_REPORT.md` §14 both claimed upload processing was "entirely
in-memory... nothing is ever written to disk." Independent re-verification (own empirical
test, not inference from framework source) via live `/proc/<pid>/fd` monitoring during
real uploads against a real running Uvicorn server, at 0.5MB (control), 1.5MB, 5MB (this
project's own SVG cap), and 20MB (this project's own raster cap, its actual maximum
configured upload size): confirmed the claim false above 1MB — Starlette's `UploadFile`
spools to a real, immediately-unlinked OS temp file via `tempfile.SpooledTemporaryFile
(max_size=1MB)` *before* the endpoint or `svg_sanitizer.py` ever see the bytes, for any
upload over 1MB (the common case at this project's own size caps, not an edge case).

**Decision: Option A (correct the documentation), not Option B (re-engineer to avoid
disk).** The actual security property the original claim was protecting — immunity to
path-traversal — does not depend on avoiding disk at all: the spooled temp file's path is
generated by Python's `tempfile` module from process-random state, never from the
attacker-controlled filename, so no attacker-controlled filename can ever influence where
anything is written, regardless of whether a given upload happens to spool. Re-engineering
the upload path to force true in-memory-only handling at every size — bypassing FastAPI's
standard `UploadFile` parameter — would not close any real gap (there wasn't one), would
add complexity for no security benefit, and would be choosing Option B merely to preserve
an already-inaccurate claim, which the correction prompt explicitly disallows.

**Fix.** Corrected the claim, disclosing the >1MB spool-to-disk behavior honestly and
stating the property that does hold, in:
- `app/application/svg_sanitizer.py`'s module docstring
- `app/infrastructure/tasks/floorplan_import.py`'s module docstring (a related but
  distinct claim, about the Celery task's own transport, corrected to no longer reference
  the sanitizer's now-corrected claim)
- `app/api/v1/floor_plans.py`'s inline comment at the upload endpoint's filename-handling
  line
- `PHASE2_IMPLEMENTATION_REPORT.md` §14

No behavior changed. Path-traversal, MIME-spoofing, XXE, and resource-exhaustion
protections were re-confirmed unaffected (all pre-existing, code-level, and independent of
where the raw bytes physically sit before `svg_sanitizer.py` receives them).

## 29.4 Carry-Forward Items (Explicitly Not Fixed)

- **NEW-1 (idempotency stale-reclaim race, Medium)** — **CARRY-FORWARD — NOT FIXED.** The
  correction prompt explicitly scoped this pass to RT-1 and RT-2 only, directing that
  NEW-1 not be redesigned. No code in `app/application/idempotency.py` was touched.
- **NEW-2 (constraint naming, Low/cosmetic)** — **CARRY-FORWARD — NOT FIXED.** Lives in an
  already-applied migration (0002/0003); the correction prompt directed no unrelated
  migration cleanup. Untouched.
- **RT-3 (N+1 query pattern in `list_racks`, Low)** — **CARRY-FORWARD — NOT FIXED,
  non-blocking observation.** `list_racks` (`app/api/v1/racks.py`) and
  `get_current_rack_placement` (`app/application/placement_service.py`) are unrelated to
  `accept_import_candidate` and the floor-plan import pipeline entirely; fixing this would
  be an unrelated performance refactor outside the scope the correction prompt set
  ("only fix if trivial and local to the code path already being modified by RT-1").

## 29.5 Regression Test Evidence

All in `backend/tests/api/test_floor_plans.py`, added this pass:

| Test | Proves | Result |
|---|---|---|
| `test_rt1_sequential_acceptance_creates_exactly_one_imported_layer` (A) | Baseline non-racing case still converges on exactly one canonical layer | PASS |
| `test_rt1_20_concurrent_accepts_of_the_same_candidate_exactly_one_wins` (B) | 20 genuinely concurrent HTTP accept requests (real ASGI transport, separate connections via a fresh engine/connection pool) for the SAME candidate: exactly one 200, the other 19 clean 409s, zero 500s | PASS |
| `test_rt1_20_concurrent_accepts_of_different_candidates_no_duplicate_layer_no_500` (C + D) | 20 genuinely concurrent accepts of 20 DIFFERENT candidates of the same job: all 20 succeed, exactly one canonical "Imported" layer results, zero 500s; then (D) the floor plan remains usable — rejecting an already-accepted candidate is a clean 409, and a brand-new candidate can still be accepted (200) | PASS |
| `test_rt1_direct_db_bypass_of_duplicate_imported_layer_is_rejected` (E) | A direct ORM insert of a second `layer_type='imported'` row for the same floor plan, bypassing the application entirely, is rejected by PostgreSQL with `IntegrityError` | PASS |

Test F (a real, separately-running Uvicorn process rather than in-process ASGI) and the
mandatory §23 acceptance test were both performed — see §29.6.

**On concurrency scale in the committed pytest suite:** both 20-way tests above run at
the literal ≥20-concurrency the correction prompt requires, in-process, via real HTTP
requests over `httpx.AsyncClient`/`ASGITransport`. Reaching this reliably required fixing
a pre-existing test-harness defect unrelated to RT-1's own logic: these tests' `finally`
blocks previously called `app.dependency_overrides.pop(get_db, None)`, which removes the
`client` fixture's own override entirely rather than restoring it — any further request
in the same test then fell through to `app/db/session.py`'s real, module-level production
engine (created once at import time, bound to whichever event loop existed then), which
surfaced as a confusing asyncpg "different loop" error attributed to whichever code ran
next. The fix (restore the previous override rather than pop it) is test-harness-only and
does not touch application code; documented at both call sites.

## 29.6 The Mandatory RT-1 Acceptance Test (§23)

Run against a real PostgreSQL instance, a real Redis instance, a real running `uvicorn
app.main:app` process (not `TestClient`), and a real Celery worker (`--pool=solo`)
processing the import job — a freshly migrated database (`0001` through `0005`, plus the
privileged-roles bootstrap script) for each server lifecycle.

Procedure per dataset: create one organization → country → city → site → building →
floor → room → floor plan; upload a 20-shape SVG; wait for the real Celery worker to
parse it; fetch the resulting 20 candidates; fire 20 genuinely concurrent HTTP accept
requests, each over its own `httpx.AsyncClient` (separate TCP connection); then verify
directly against PostgreSQL.

**Actual observed results** (3 full runs, 6 datasets total, against the same long-lived
server process — deliberately reused rather than restarted each time, to also cover the
connection-reuse-count-dependent sub-finding from §29.2):

| Dataset | Canonical layer count | Duplicates | HTTP 500s from the race | Floor plan usable after (reject→409, fresh accept→200) | Orphaned `SpatialObject`s | Unique index present |
|---|---|---|---|---|---|---|
| Run 1 / dataset 1 | 1 | 0 | 0 | Yes | 0 | Yes |
| Run 1 / dataset 2 | 1 | 0 | 0 | Yes | 0 | Yes |
| Run 2 / dataset 1 | 1 | 0 | 0 | Yes | 0 | Yes |
| Run 2 / dataset 2 | 1 | 0 | 0 | Yes | 0 | Yes |
| Run 3 / dataset 1 | 1 | 0 | 0 | Yes | 0 | Yes |
| Run 3 / dataset 2 | 1 | 0 | 0 | Yes | 0 | Yes |

Every one of the 20 concurrent accept requests returned 200 in every dataset (120/120
total across all 6 datasets), 0 HTTP 500s, 0 orphaned `SpatialObject`s, and the unique
index survived every run. (An earlier run, before the §29.2 literal-predicate fix, using
this identical procedure against a long-lived server, is what surfaced the sub-finding in
the first place: 8–11 of 19–20 concurrent accepts failed with 500 once pooled connections
had been reused past PostgreSQL's generic-plan threshold. That failure mode is fully
closed by the fix above and does not reproduce in any of the 6 datasets recorded here.)

Test F (real live Uvicorn, not in-process ASGI) is satisfied by this same procedure.

## 29.7 Full Validation Results

- **Backend test suite:** `pytest -q` → **191 passed**, 3 consecutive runs, zero
  regressions, zero flakes.
- **Migration validation:**
  - Fresh install (`alembic upgrade head` from empty, migrations 0001→0005): clean.
  - Downgrade to `0004_phase2`, then re-upgrade to head: clean; unique index correctly
    dropped and recreated identically.
  - Synthetic pre-existing-duplicate-data scenario: seeded two "imported" `SpatialLayer`
    rows for one floor plan (one older, one newer) with a `SpatialObject` pointing at the
    *newer* (duplicate) one, then ran the upgrade — confirmed exactly one layer survives
    (the older, canonical one), the `SpatialObject` is re-pointed to it (not deleted or
    orphaned), and the unique index is present afterward.
  - Direct constraint-bypass attempt: see regression Test E above (DB-enforced,
    independent of the migration path taken to reach it).
- **Lint (`ruff check`):** 28 errors — at or below the pre-existing baseline of 29 (this
  correction's own new test code was written to introduce zero new violations; the
  pre-existing baseline errors are untouched, unrelated files/lines).
- **Type-check (`mypy`):** 4 errors, in 2 files — identical to the pre-existing baseline;
  this correction's own new code (the `pg_insert`/`cast(Table, ...)` construction)
  introduced and then eliminated its own transient error before being counted as clean.
- **Frontend build (`npm run build`, `tsc --noEmit && vite build`):** clean, no errors, no
  new warnings.
- **Manual frontend browser validation:** performed against the real vite dev server and
  the real fixed backend (a real login form, real client-side `<NavLink>`/`<Link>`
  navigation — the app's access token lives only in memory by design, so this is not a
  full-page-reload walkthrough — a real "Accept" button click for the happy path, then a
  19-way concurrent background race, then real client-side re-navigation back to the same
  page). Result: the UI's own "Accept" button correctly worked end-to-end (TanStack Query
  invalidated and removed the accepted candidate from the pending list); the race itself
  returned 19/19 200s, 0/19 500s; after the race, re-navigating to the same room's
  floor-plan page showed no raw error page, no duplicate/stale pending-candidates section,
  and the correct final state (candidates list empty, no crash, no console errors beyond
  one unrelated 404 for a static asset). Screenshots retained under `/tmp/redteam/` for
  this session only (not committed — session-local artifacts, matching this repository's
  existing convention for manual browser smoke-test screenshots, e.g.
  `PHASE2_TRACEABILITY_MATRIX.md`'s own references to `/tmp/smoke_*.png`).

## 29.8 Files Changed

- `backend/migrations/versions/0005_correction_spatial_layer_race.py` — **new.** Dedup +
  partial unique index (RT-1).
- `backend/app/api/v1/floor_plans.py` — RT-1 fix (`_get_or_create_imported_layer`,
  `FOR UPDATE` locking in `accept_import_candidate`/`reject_import_candidate`); RT-2
  documentation correction (inline comment).
- `backend/app/application/svg_sanitizer.py` — RT-2 documentation correction (module
  docstring).
- `backend/app/infrastructure/tasks/floorplan_import.py` — RT-2 documentation correction
  (module docstring, cross-reference update only).
- `backend/tests/api/test_floor_plans.py` — RT-1 regression tests A, B, C, D, E; a
  test-harness dependency-override fix (restore rather than pop) needed to run the B/C
  tests reliably at literal 20-way concurrency.
- `PHASE2_IMPLEMENTATION_REPORT.md` — RT-2 documentation correction (§14).
- `PHASE2_TRACEABILITY_MATRIX.md` — new "Independent red-team findings — correction
  status" section; NEW-1/NEW-2 rows updated to explicit CARRY-FORWARD wording.
- `PHASE2_CORRECTION_REPORT.md` — this report, new.

No other files were modified. No temporary correction artifacts (scripts, screenshots,
scratch databases) were committed; all lived under `/tmp/redteam/` or were dropped
PostgreSQL databases (`dcim_correction`, `dcim_migtest`, `dcim_browsertest`), cleaned up
at the end of this session.

---

**PHASE 2 CORRECTIONS COMPLETE — READY FOR INDEPENDENT RED-TEAM REVALIDATION**
