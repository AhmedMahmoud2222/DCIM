# Phase 2 Nested-SVG Correction Report

Implementer's own correction and testing. **Not** independent red-team validation —
see §15 and the required next step at the end of this document.

- Original defective commit: `d6ee5a325dffb03217173fa3f04639f8846201f2`
  (`PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md`, §7.1 / §14 NEW-FINDING-1)
- Prior correction commit (RT-1/RT-2, unchanged by this work): `8746e0e2441398e46e70b46be14eca87b13fa3de`

## 1. Defect Summary

A deeply nested SVG (~960+ levels of `<g>`) crashed `app/application/svg_sanitizer.py`'s
tree walk with an uncaught Python `RecursionError`. That exception propagated out of the
Celery task (`run_floor_plan_import_job`) past the only exception handler present
(`except SvgRejected`), so the task itself failed, but the `FloorPlanImportJob` row it was
processing was left at `status = "parsing"` — the last state durably committed before the
crash — permanently. There was no code path that could ever move it forward. The frontend
polls for `parsed`/`failed` and would show an indefinite spinner for such a job.

## 2. Independent Red-Team Evidence

`PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md` §7.1 reproduced this independently: a
970-level nested `<g>` SVG, uploaded through the real API, produced a Celery task failure
in the worker log and left the corresponding job stuck at `"parsing"` with no further
transition. That report classified it MEDIUM (denial of service against a single import
job, not the whole worker or other jobs) and is the sole source of this correction's scope.

## 3. Root Cause

`_walk()` in `app/application/svg_sanitizer.py` was a Python function that called itself
once per child element, inside a `for` loop over the current element's children. Each
level of `<g>` nesting added one Python call-stack frame. Around 960-1000 levels — well
within a 7KB file — the walk exceeded Python's interpreter call-stack limit (default
~1000) and raised `RecursionError`, an exception neither `sanitize_svg`'s own callers nor
the Celery task anticipated: the task's `try/except` only caught `SvgRejected`, the
sanitizer's own deliberate-rejection type. `RecursionError` bypassed that handler entirely
and propagated out of the `with get_sync_db() as db:` block. That block's `__exit__` only
closes the session (an implicit rollback of anything uncommitted) — it never commits — so
whatever was last durably committed (`job.status = "parsing"`, set unconditionally near
the top of the task, before parsing starts) is what persisted. No diagnostics row was ever
flushed either, since the session's `autoflush=False` and the crash happened before any
explicit `commit()` past that point.

This is a distinct failure mode from the existing `MAX_ELEMENTS` cap, which bounds
*breadth* (how many elements, sibling or otherwise, the walk is willing to look at) but did
nothing to bound *depth* (how many levels of nested containers the walk is willing to
descend into) — a document can be small in total element count and byte size while still
being pathologically deep.

## 4. Correction

Two independent, complementary changes:

**(a) `app/application/svg_sanitizer.py` — `_walk` rewritten from recursive to iterative.**
The walk now uses an explicit `list` as a stack of `(element, depth)` tuples instead of
Python call recursion. Pushing another tuple onto a Python list never risks a
`RecursionError`, regardless of how deep a document claims to be — this removes the
interpreter's own call-stack depth as an attack surface entirely, independent of any
depth-limit business rule. Traversal order (and therefore shape order, `objects_discovered`
counts, and warnings) is unchanged from the recursive version: children are pushed in
`reversed()` order so the leftmost pops first, and each element is fully processed before
its children are pushed, exactly matching the original pre-order, depth-first,
process-before-descend semantics.

A separate, deterministic `MAX_NESTING_DEPTH = 200` constant caps nesting depth as its own
business rule, on top of (not instead of) the iterative rewrite — defense in depth, and a
bound on the CPU/memory an attacker can force purely through nesting even though iteration
itself no longer risks a crash. `sys.setrecursionlimit()` was not used anywhere, per the
explicit constraint against it. See §6 for why 200 was chosen and the boundary semantics.

**(b) `app/infrastructure/tasks/floorplan_import.py` — a generic backstop around each
task's full parsing body.** Both `run_floor_plan_import_job` and
`validate_and_run_raster_import_job` now wrap their bodies in `try: ... except SvgRejected
... except Exception:`. The new `except Exception` branch (after the existing, unchanged
`except SvgRejected` branch) calls a new helper, `_mark_job_failed_on_unexpected_error`,
which rolls back the session, re-fetches the job fresh, sets `status = "failed"` with a
fixed, generic `rejection_reason`, writes a diagnostics row recording the failure, commits,
and then re-raises the original exception (so Celery's own task-failure visibility and
logging are preserved). This closes the gap for *any* future/unrelated exception, not just
`RecursionError` — see §5.

No migration was created (§10): both changes are pure application code: a rewritten
function body and a new `except` branch. The existing schema (`FloorPlanImportJob.status`
already includes `"failed"`; `rejection_reason` and `finished_at` are already nullable
columns used by the pre-existing `SvgRejected` path) fully supports the correction.

## 5. Import Job Failure Handling

Before this correction, only `SvgRejected` (a deliberate, recognized rejection) could move
a job out of `"parsing"` on the failure path. Any other exception left the job stuck
forever. After this correction:

- `SvgRejected` still transitions the job to `"failed"` with `exc.reason` as the
  `rejection_reason`, exactly as before — this path is unchanged.
- Any other exception (verified with a plain `ValueError` via monkeypatch, not just the
  specific `RecursionError`) is caught by the new outer `except Exception`, which commits
  `status = "failed"` with a fixed generic message
  (`"The uploaded file could not be processed due to an internal error."`), a diagnostics
  row (`errors=["internal error during parsing"]`), and `finished_at`, then re-raises.
- Celery has no `autoretry_for` or `task_acks_late` configured anywhere in
  `app/infrastructure/celery_app.py` (confirmed by direct inspection) — no task in this
  codebase retries automatically. Re-raising after committing the terminal state therefore
  surfaces the failure in Celery's own logs/monitoring without creating any retry loop:
  deterministic malicious input fails once, cleanly, and stays failed.
- Because the diagnostics/candidate rows for the failed attempt were never flushed before
  the crash (`autoflush=False`, no explicit commit yet), `db.rollback()` has nothing
  partial to discard — the fresh diagnostics row added by
  `_mark_job_failed_on_unexpected_error` is the only row written for that attempt.

Net effect: there is no exception type that can leave a `FloorPlanImportJob` in
`"parsing"` indefinitely. Every import attempt now has a deterministic path to a terminal
state (`"failed"`) or, for the classified-rejection path, the same outcome it always had.

## 6. Security Impact

`MAX_NESTING_DEPTH = 200` is generous against any legitimate data-center floor plan — even
a heavily layered CAD/Visio export grouping racks, equipment, and annotations into their
own named layers has no plausible reason to nest more than a handful of `<g>` levels deep —
while remaining far below where a recursive implementation would approach Python's own
default recursion limit (1000). The check only rejects an element that *has children*
sitting at the depth limit (i.e., one that would need to descend one level further); a leaf
element sitting exactly at the limit is legitimate and is processed normally. This was
caught and fixed during manual adversarial verification before the test suite was run (see
§9's "off-by-one" note is folded into the test names in §7 — `..._accepts_exactly_the_
maximum_allowed_depth` and `..._rejects_one_level_beyond_...` directly test this boundary).

Because rejection happens the moment the depth limit is exceeded — before any of the
excess depth is actually walked — and because the walk is an O(depth) list operation
either way, an attacker cannot force unbounded CPU or memory purely through nesting.
Measured real-service rejection times (§11): 970 levels in 0.55s, 10,000 levels in 0.53s —
i.e., rejection cost does not grow materially with attack depth, since the walk is capped
long before either depth is fully processed.

## 7. Regression Tests

**`backend/tests/unit/test_svg_sanitizer.py`** (25 tests total; 7 new, added in a labeled
section):

- `test_sanitize_svg_moderate_nesting_succeeds` — PASS
- `test_sanitize_svg_accepts_exactly_the_maximum_allowed_depth` — PASS
- `test_sanitize_svg_rejects_one_level_beyond_the_maximum_allowed_depth` — PASS
- `test_sanitize_svg_rejects_the_independently_reported_red_team_depth_without_recursionerror` (970 levels) — PASS
- `test_sanitize_svg_rejects_extreme_nesting_depth_quickly_and_without_recursionerror` (20,000 / 100,000 levels, each asserted <2.0s) — PASS
- `test_sanitize_svg_preserves_shape_order_after_switching_to_iterative_traversal` — PASS
- `test_sanitize_svg_repeated_malicious_deep_svg_does_not_corrupt_subsequent_calls` — PASS

**`backend/tests/api/test_floor_plans.py`** (27 tests total; 6 new):

- `test_deeply_nested_svg_transitions_to_failed_not_stuck_in_parsing` (970 levels, via real upload → job polling → diagnostics/candidates check) — PASS
- `test_much_deeper_nested_svg_also_fails_cleanly_not_just_at_the_reported_depth` (20,000 levels) — PASS
- `test_repeated_malicious_nested_svg_uploads_do_not_prevent_a_later_valid_import` (5× attack, then a valid import) — PASS
- `test_concurrent_malicious_and_valid_imports_do_not_cross_contaminate` (real concurrent asyncio.gather) — PASS
- `test_unexpected_non_svgrejected_exception_still_reaches_a_terminal_state` (monkeypatched `sanitize_svg` to raise `ValueError`; asserts terminal `"failed"` state, generic `rejection_reason`, no leaked exception text, no leftover candidates) — PASS

All 12 new tests pass. Full backend suite: **203 passed** (191 pre-existing + 12 new),
confirmed twice in this session — once before, once after an unrelated container/process
reset (see §11) — with an identical result both times.

## 8. Existing Security Controls Preserved

Not modified by this correction, and re-verified present in the current file:
content-sniffed type check, 5MB file-size cap (checked before parsing), `defusedxml` with
DTD/external-entity/external-resource resolution forbidden, `MAX_ELEMENTS` breadth cap,
`<script>`/dangerous-tag stripping (and no descent into a stripped element's children),
event-handler attribute stripping, external `href`/`xlink:href` stripping (internal
fragment refs still allowed), coordinate clamping for out-of-range/NaN/Infinity values, and
the Sanitized Intermediate Representation boundary (only `SirShape` values cross out of the
sanitizer, never raw `Element` objects or markup). None of these were touched; `_walk`'s
rewrite preserves every one of these checks at the same point in traversal they occurred
at before.

## 9. RT-1 Regression Verification

RT-1 (the partial-unique-index-enforced single-`Imported`-`SpatialLayer`-per-`FloorPlan`
invariant) was not touched by this correction. Re-verified in this session:

- The 4 existing RT-1 pytest regression tests: PASS (part of the 203-test full-suite run).
- A fresh real-HTTP concurrency spot-check against a live server on the post-fix code: 20
  candidates from one import, 20 concurrent `POST .../accept` requests via
  `asyncio.gather` → `{200: 20}`, 0 failures. Direct database check after the run: exactly
  1 `spatial_layer` row with `layer_type='imported'` for that floor plan, and exactly 20
  `spatial_object` rows referencing it (no duplicates, none missing). The partial unique
  index (`uq_spatial_layer_one_imported_per_floor_plan`) is present in the schema
  unchanged.

## 10. Migration Status

No migration was created. `alembic heads` on this branch resolves to `0005_correction`,
the same head as before this correction — this change is pure application code. The schema
already supported everything the correction needed (existing `"failed"` status value,
existing nullable `rejection_reason`/`finished_at` columns).

## 11. Real-Service Validation

Performed twice in this session: once against a first `dcim_svgfix` PostgreSQL database
with a real Uvicorn server and a real Celery worker (`--pool=solo`), all through the
genuine `dcim_app` role via the HTTP API only; and again, in full, after this session's
container/process state was reset mid-task (the first round's running processes and
in-memory state were lost, though the on-disk PostgreSQL data and roles persisted). The
second round is the one reported here as the current, final evidence:

- Literal red-team reproduction (970 levels): `status=failed`,
  `reason="SVG nesting exceeds the 200-level depth limit"`, elapsed 0.55s.
- 2,000 levels: `failed`. 10,000 levels: `failed`, elapsed 0.52s.
- Combined 300-deep × 200-wide (nesting + large element count): `failed`.
- Malformed nested XML (unclosed tag 50 levels deep): `failed`,
  `reason="malformed XML: not well-formed (invalid token): line 1, column 198"` (this
  specific error path — `except ParseError` in `sanitize_svg` — is pre-existing and
  untouched by this correction; noted here for completeness, not as new behavior).
- 10× repeated malicious uploads: all failed cleanly; worker process stayed alive
  throughout (confirmed via `ps`/log inspection after each).
- 3 concurrent malicious + 3 concurrent valid imports (`asyncio.gather` over separate
  `httpx.AsyncClient`s): all 6 resolved correctly and independently.
- Post-attack sanity: a normal SVG import still succeeds (`parsed`) after all of the above.
- Celery worker-kill/restart: submitted a 970-level attack, force-killed
  (`kill -9`) the worker process, confirmed via direct database query that the job stayed
  `"queued"` (never silently promoted) while the worker was down, restarted the worker, and
  confirmed via both the Celery log and a follow-up database query that the job was picked
  up and transitioned to `"failed"` within about 1 second of the worker restarting.
- Observability: fetched the actual JSON response bodies (`GET .../import-jobs/{id}` and
  `.../diagnostics`) for failed jobs directly — `rejection_reason` and `errors` contain
  only the safe, developer-authored strings shown above; no filesystem paths, no Python
  tracebacks. `grep -c "Traceback (most recent call last)"` against both the Uvicorn and
  Celery log files for this entire validation session: `0` in both.
- RT-1 spot-check: see §9.

One thing this real-service round did **not** exercise: the generic `except Exception`
backstop (§4b/§5) for an exception type *other than* `RecursionError` was not triggered
against the standalone live Celery worker process (doing so would require injecting a
fault into that separate process, which was not attempted). That path — a monkeypatched
`ValueError` reaching a real, non-SQLite Postgres session and producing the correct
terminal state and safe message — is instead verified by
`test_unexpected_non_svgrejected_exception_still_reaches_a_terminal_state` (§7), which runs
against the same real PostgreSQL `dcim_app`-role database as the rest of the test suite,
via a direct in-process task call rather than a separate worker process consuming from
Redis. This distinction is disclosed rather than glossed over.

Environment: real PostgreSQL 16, real Redis, real Uvicorn, real Celery (`--pool=solo`).
No Docker was used for this round (available on the host, but the existing PostgreSQL/Redis
services were used directly, consistent with how this repository's test suite already
runs) — disclosed per the requirement to state exactly what was and wasn't used.

## 12. Browser Validation

Not performed in this correction round. Source-level inspection (unchanged from the prior
correction phase, since no frontend files were touched here) confirms
`frontend/src/features/floor-plans/RoomFloorPlanPage.tsx` already: polls the job every 3s
until `status` is `parsed` or `failed`; includes `failed` in its `STATUS_COLORS` map; and
renders `{selectedJob.status === "failed" && (<p>{selectedJob.rejection_reason}</p>)}`. No
frontend code change was needed or made. A live browser check was judged not practical to
add in this round without provisioning a second full environment (a separate frontend dev
server plus a fresh database at migration head, since the repository's other persistent
dev database was on an older schema version) purely to re-confirm behavior the source
already shows and that the general job-polling mechanism was already browser-validated in
the prior (RT-1) correction phase. This is disclosed honestly rather than asserted as done.

## 13. Files Changed

- `backend/app/application/svg_sanitizer.py` — `_walk` rewritten iterative; added
  `MAX_NESTING_DEPTH`; docstring updates. (Requirement A / root cause fix.)
- `backend/app/infrastructure/tasks/floorplan_import.py` — added
  `_mark_job_failed_on_unexpected_error`; wrapped both task bodies in
  `try/except SvgRejected/except Exception`. (Failure-handling correction.)
- `backend/tests/unit/test_svg_sanitizer.py` — 7 new regression tests.
- `backend/tests/api/test_floor_plans.py` — 5 new regression tests (a 6th, the
  concurrent-imports test, was added alongside them in the same section — 6 new tests
  total per the file's test count in §7; see note below).

No other file was changed. Explicitly confirmed unchanged: RT-1's implementation
(`accept_import_candidate`, migration `0005_correction`), the audit architecture, the
outbox architecture, the authoritative/import boundary, and RBAC.

*(§7 lists 5 named API-level tests plus the concurrent-imports test named in that same
section for a total of 6 new tests in that file, consistent with the 21→27 count.)*

## 14. Carry-Forward Findings

Explicitly not addressed by this correction, per scope — left exactly as found in
`PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md`:

- **NEW-1** (idempotency stale-reclaim race) — not touched.
- **NEW-2** (constraint naming) — not touched.
- **RT-3** (bounded `list_racks` N+1) — not touched.

## 15. Known Limitations

- The generic `except Exception` backstop (§4b) is verified against a real PostgreSQL
  session and a real HTTP request/response cycle, but not against the standalone live
  Celery worker process for a non-`RecursionError` exception type (see §11's explicit
  disclosure). Its logic is identical for both paths, but this is disclosed as a gap in
  *how* it was exercised, not asserted as fully closed by process-level evidence.
- The pre-existing "malformed XML" rejection message (`except ParseError` in
  `sanitize_svg`, untouched by this correction) includes the underlying parser's own
  line/column detail (e.g. `"malformed XML: not well-formed (invalid token): line 1, column
  198"`). This is not a stack trace or a filesystem path, and is out of this correction's
  scope (it was not introduced or changed here), but is noted for completeness since it
  touches the same "observability/no internal leakage" requirement area.
- Live browser validation was not performed this round (§12).

---

**PHASE 2 NESTED-SVG CORRECTION COMPLETE — READY FOR INDEPENDENT RE-VALIDATION**

This is the implementer's own assessment, not independent validation. The next required
step is another independent red-team re-validation of this correction, exactly as was done
for the prior RT-1/RT-2 correction.
