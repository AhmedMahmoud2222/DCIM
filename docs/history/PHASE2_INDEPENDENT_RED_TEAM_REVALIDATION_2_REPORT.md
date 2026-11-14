# Phase 2 Independent Red-Team Revalidation #2

Post nested-SVG correction. Independent adversarial revalidation engineer — not the
implementer. This report does not modify any application source, test, migration,
frontend, or configuration file; the only repository change from this session is this
report itself.

## 1. Executive Summary

The nested-SVG defect (uncaught `RecursionError` from deep `<g>` nesting, leaving
`FloorPlanImportJob` permanently stuck in `parsing`) is **independently confirmed closed**.
I reproduced the original attack pattern, tested the depth boundary (199/200/201) for
off-by-one errors (none found), tried four distinct bypass strategies against the
iterative traversal and depth cap (none succeeded), drove the attack through the real
HTTP → Celery → PostgreSQL pipeline including a worker kill/restart, and confirmed the
job always reaches a terminal `failed` state with a safe, generic message. I also
independently verified the generic `except Exception` backstop against five different
injected exception types (not just `RecursionError`), confirmed RT-1's concurrency
invariant at 50 concurrent accepts, ran a fresh clean-install + downgrade + re-upgrade
migration cycle, re-ran an independent SVG/XML security regression matrix, verified the
Sanitized Intermediate Representation and authoritative-state boundaries, and drove the
real browser UI through both a malicious and a subsequent valid import.

One new, independently discovered finding, unrelated to the nesting-depth mechanism
itself and **not introduced by this correction** (verified present in the pre-correction
code too): `MAX_ELEMENTS` is a soft cap that still requires walking (and, via `_walk`'s
`list(element)`/`itertext()` calls, sometimes fully materializing or concatenating) a
single element's entire immediate-children collection or text subtree before the
element-count check has a chance to stop further work — allowing a 5MB file with ~655,000
raw elements under one `<text>` node to be accepted as `"parsed"` in about 2 seconds,
with diagnostics reporting only 5,000 objects discovered. This is bounded by the
pre-existing file-size cap, does not crash anything, does not leave a stuck job, and does
not touch the depth mechanism under revalidation — classified LOW/Observation, does not
block Phase 2 (see §22).

No Critical, High, or blocking Medium findings. Carry-forward findings NEW-1 (Medium),
NEW-2 (Low), RT-3 (Low/Observation) are unchanged — the correction's diff does not touch
any of the code paths those findings concern, confirmed by `git diff`.

**Final verdict: PHASE 2 INDEPENDENT RED-TEAM REVALIDATION PASSED — PROCEED TO PHASE 3**

## 2. Tested Commit

`0afe8dcb8236f990ec8c964aad76683e3fee542a` (confirmed via `git rev-parse HEAD` before and
after all testing; working tree clean before, during, and after — `git status --short`
and `git diff --stat` both empty at the end of this session).

Previous baseline: `d6ee5a325dffb03217173fa3f04639f8846201f2` (prior independent
red-team revalidation, where NEW-FINDING-1 — the nested-SVG defect — was proven).

## 3. Environment

- PostgreSQL 16 (`16/main`, real service via `service postgresql start`, not Docker).
- Redis 7.0.15 (real `redis-server`, not Docker).
- Python 3.11.15, backend `.venv` (FastAPI 0.141.1, Celery 5.6.3, SQLAlchemy 2.0.54).
- Real Uvicorn (`uvicorn app.main:app`), real Celery worker (`--pool=solo`).
- Real `dcim_app` database role for all application-path testing (verified: no
  superuser/`SET ROLE` used for anything other than initial database/extension/bootstrap
  setup, exactly as required).
- **Docker was unavailable** in this environment (`docker info` reports
  `failed to connect to the docker API at unix:///var/run/docker.sock` — no daemon
  running). Directly-installed PostgreSQL/Redis were used instead, matching how this
  repository's own test suite already runs. Disclosed rather than worked around silently.
- **Multi-worker Uvicorn was not tested** (single Uvicorn process only) — disclosed as a
  limitation (§25); this does not affect the specific defect under revalidation, which is
  a single-job Celery-task failure mode, not a multi-process race (that is RT-1's
  concern, separately re-tested at real-HTTP concurrency, not multi-Uvicorn-process
  concurrency, in §16).
- A dedicated, independently-created scratch database (`dcim_rt2`) was used for this
  revalidation — not the implementer's `dcim_svgfix` database (already dropped by the
  implementer before this session started), and not reused from any prior session.
- A second, separate scratch database (`dcim_migtest`) was created and dropped solely for
  the migration downgrade/upgrade cycle (§20), to avoid disturbing the evidence collected
  in `dcim_rt2`.
- Both scratch databases were dropped and all Uvicorn/Celery/Vite processes stopped at
  the end of this session (verified via `ps aux` and `psql \l`).

## 4. Independence Statement

I did not trust the implementer's `PHASE2_NESTED_SVG_CORRECTION_REPORT.md` claims at
face value. Every attack generator used in this session (`/tmp/rt2_validation/rt2_lib.py`
and its test scripts) was written fresh for this revalidation, not copied from the
implementer's `real_service_attack.py` or from `tests/api/test_floor_plans.py` /
`tests/unit/test_svg_sanitizer.py`. Where I did run the implementer's own 12 new tests
(§24), I treat that as corroboration only, not as the independent evidence itself — the
independent evidence is the separately-written reproductions in §5–§19. I inspected the
actual diff (`git diff d6ee5a3 0afe8dc`) rather than trusting the commit message or
report prose, and used that diff to identify precisely what changed (§8) before deciding
what needed independent re-testing versus what could be carried forward unchanged (NEW-1,
NEW-2, RT-3 — §21).

## 5. Original Nested-SVG Reproduction

Reproduced the exact original attack pattern (970-level `<g>` nesting around one
`<rect>`) via a freshly-written generator (`rt2_lib.gsvg(970)`), through the real HTTP
upload → Celery task → PostgreSQL pipeline:

```
depth= 970 -> status=failed reason=SVG nesting exceeds the 200-level depth limit
```

No `RecursionError`, no worker crash, terminal `failed` state reached, `rejection_reason`
populated, `finished_at` set (confirmed via direct database query in §9).

## 6. Nested-SVG Correction Validation

Confirmed the two-part correction actually present in the code (via `git diff`, §8, not
just the commit message):

- `_walk` in `app/application/svg_sanitizer.py` is genuinely iterative: an explicit
  `stack: list[tuple[Element, int]]`, a `while stack:` loop, `stack.pop()`/`stack.extend()`
  — no call to itself anywhere in the function body. Independently confirmed via AST
  analysis across the entire `backend/app` tree (§8) that zero functions in the codebase
  call themselves by name (excluding `__init__`/`get_logger` false positives from a naive
  attribute-call grep) — i.e., there is no other Python-recursive tree walk anywhere that
  SVG input could reach.
- A separate `MAX_NESTING_DEPTH = 200` constant enforces a deterministic depth cap,
  independent of Python's own call-stack limit.

## 7. Depth Boundary Results

Independently tested depths 0, 1, 199, 200, 201 through real HTTP:

```
depth=   0 -> status=parsed reason=None
depth=   1 -> status=parsed reason=None
depth= 199 -> status=parsed reason=None
depth= 200 -> status=failed reason=SVG nesting exceeds the 200-level depth limit
depth= 201 -> status=failed reason=SVG nesting exceeds the 200-level depth limit
```

**No off-by-one error.** The boundary is exactly where the correction report claims: a
container element (one with children) sitting at depth 200 is rejected; the same
container at depth 199 (i.e., 199 `<g>` wrappers around the leaf `<rect>`) succeeds. A
leaf element sitting at the limit is never itself rejected — only a *container* that
would need to descend one level further is.

## 8. Recursion/Traversal Analysis

- Confirmed via `git diff d6ee5a3 0afe8dc` that exactly two production files changed:
  `app/application/svg_sanitizer.py` and `app/infrastructure/tasks/floorplan_import.py`
  (plus two test files and the correction report) — no migration, no frontend, no RBAC,
  no audit, no outbox file touched.
- **Bypass attempts against the depth cap** (all independently written, not from the
  implementer's fixtures), all through real HTTP:

  | Attempt | Result |
  |---|---|
  | 970-level nested `<svg>` (instead of `<g>`) | `failed`, depth-limit reason, 0.27s |
  | 970-level alternating `<g>`/`<svg>`/`<defs>` | `failed`, depth-limit reason, 0.27s |
  | 970-level nested `<a>` (unrecognized container-like tag) | `failed`, depth-limit reason, 0.27s |
  | 970-level nested `<use>` (a `DANGEROUS_TAGS` entry) | `parsed` — **not a bypass**: `<use>` is stripped at the outermost level and `_walk` never descends into a stripped element's children at all (`continue` before computing `children`), so the entire malicious subtree is discarded after inspecting one element; nothing pathological is ever walked |

  No container tag or combination of tags lets nesting depth escape the 200-level cap.
  The one "success" case (`<use>`) is safe by construction, not a gap.
- One genuinely new observation, unrelated to depth/recursion (§10, §15, §22).

## 9. Import Job State Machine Validation

Independently injected five different exception types directly into
`run_floor_plan_import_job` (via runtime monkeypatch of `sanitize_svg`, using a **real**
PostgreSQL session via `get_sync_db()`, not a mock):

```
ValueError           task_raised=ValueError           final_status='failed'  reason='The uploaded file could not be processed due to an internal error.' finished_at_set=True
RuntimeError         task_raised=RuntimeError         final_status='failed'  reason='The uploaded file could not be processed due to an internal error.' finished_at_set=True
KeyError             task_raised=KeyError             final_status='failed'  reason='The uploaded file could not be processed due to an internal error.' finished_at_set=True
RecursionError       task_raised=RecursionError       final_status='failed'  reason='The uploaded file could not be processed due to an internal error.' finished_at_set=True
TypeError            task_raised=TypeError            final_status='failed'  reason='The uploaded file could not be processed due to an internal error.' finished_at_set=True
```

Every case: task re-raises (Celery visibility preserved), job reaches `failed`,
`finished_at` set, `rejection_reason` is the fixed generic string with **no trace of the
injected exception's own message** (one case deliberately embedded a fake secret path,
`"secret_path=/etc/shadow"`, in the exception message to test for leakage — confirmed
absent from the stored `rejection_reason`).

Also confirmed via direct SQL query that `FloorPlanImportDiagnostics` rows are written
for these failures (not skipped), and that `_mark_job_failed_on_unexpected_error`'s
`if job is None: return` path does not swallow the exception — the caller's `raise`
still fires regardless.

## 10. Celery Failure/Recovery Validation

- **Deterministic malicious input, no retry storm**: across all uploads in this session
  (11+ malicious/valid/malformed uploads through the real worker in one continuous log),
  `grep -c "received$"` and `grep -c "succeeded in"` on the Celery log both returned
  identical counts (11/11) — 1:1, no duplicate task execution, `grep -ci retry` returned
  0. Confirmed independently that `app/infrastructure/celery_app.py` configures no
  `autoretry_for`/`task_acks_late` for any task.
- **Worker kill/restart**: submitted a 970-level attack, `kill -9`'d the actual Celery
  worker process (verified via `ps aux` before/after, not a wrong PID), confirmed via
  direct SQL query the job stayed `queued` (never silently promoted) while the worker
  was down, restarted the worker, confirmed via SQL query the job transitioned to
  `failed` with the correct reason within ~1 second of restart. No data loss, no stuck
  state, worker fully alive afterward.

## 11. Concurrent Import Validation

Independently written concurrency test (`asyncio.gather`, separate `httpx.AsyncClient`
per request, real HTTP): 4 concurrent 970-level malicious uploads + 4 concurrent valid
uploads + 2 concurrent malformed-XML uploads, all against the same floor plan:

```
attack statuses (want all failed):    ['failed', 'failed', 'failed', 'failed']
valid statuses (want all parsed):     ['parsed', 'parsed', 'parsed', 'parsed']
malformed statuses (want all failed): ['failed', 'failed']
```

Cross-contamination check: all 10 job IDs were distinct (no ID collisions); for each job,
its own candidates were queried and matched its own outcome (0 candidates for every
failed job, ≥1 candidate for every parsed job) — no diagnostics or candidates assigned to
the wrong job, no shared mutable sanitizer state observed.

## 12. Resource Exhaustion Validation

- 970/2,000/10,000-level depth attacks (breadth-1, pure depth): rejected in ~0.27–0.55s
  each, no growth with depth beyond the 200-level cap (rejection happens at depth 200
  regardless of how much deeper the document claims to go).
- Deep-nesting-inside-`<text>` (349,512 levels of `<tspan>`, sized to the 5MB file cap):
  rejected in 0.41s — `_walk`'s own depth cap still applies to `<text>`'s descendants
  once it starts iterating into them; no bypass, no crash, no `RecursionError` even at
  this extreme depth (independently confirmed via raw `xml.etree.ElementTree.itertext()`
  at 50,000 levels in isolation too: 0.0009s, no error).
- Wide-under-`<text>` (655,335 sibling `<tspan/>` elements under one `<text>`, sized to
  the 5MB file cap): **accepted as `"parsed"`** in ~2 seconds, `objects_discovered=5000`
  (`MAX_ELEMENTS` reached and silently truncated) despite the actual document containing
  ~130× more raw elements. This is the new finding (§10 of the correction diff was not
  touched here — this soft-cap-then-return behavior, `if result.objects_discovered >=
  MAX_ELEMENTS: ...; return`, is byte-for-byte present in both the pre-correction
  recursive `_walk` and the post-correction iterative `_walk`; confirmed via `git diff`
  that this specific logic is unchanged). Bounded by the pre-existing 5MB file-size cap
  (an attacker cannot exceed ~655K such elements without exceeding that cap first); does
  not hang, crash, or leave a stuck job. See §22 for full write-up and severity.
- Combined 300-deep × 200-wide (breadth and depth together): rejected cleanly, same as
  pure depth (§ implementer's own report, re-confirmed not independently re-timed this
  round since it is a strict subset of the deeper-nesting cases already independently
  reproduced above).

## 13. SVG/XML Security Regression

Independently written matrix (not copied from either party's existing tests), all
through real HTTP:

| Case | Expected | Got | Result |
|---|---|---|---|
| XXE file read (`SYSTEM "file:///etc/passwd"`) | failed | failed | OK |
| XXE billion-laughs entity expansion | failed | failed | OK |
| External DTD reference | failed | failed | OK |
| `<script>` tag | parsed (stripped) | parsed | OK |
| Event-handler attribute (`onload`) | parsed (stripped) | parsed | OK |
| External `xlink:href` | parsed (stripped) | parsed | OK |
| Malformed XML | failed | failed | OK |
| Non-SVG/non-XML content | failed | 422 at upload time | OK (fails even earlier/faster than expected — synchronous content-sniff rejection, not a defect) |
| Oversized (>5MB) | failed | failed | OK |
| NaN/Infinity coordinates | parsed (clamped) | parsed | OK |
| Invalid/garbled encoding | failed | failed | OK |
| `<use>` (dangerous tag) | parsed (stripped) | parsed | OK |
| `<foreignObject>` with embedded HTML/script | parsed (stripped) | parsed | OK |
| Path-traversal filename (`../../etc/passwd.png`) | accepted, no traversal | accepted, no traversal (temp storage uses randomized names, not attacker filename — carried over from RT-2, unchanged) | OK |
| Unusual Unicode filename | accepted | accepted | OK |
| Excessively long filename (300 chars) | accepted | accepted | OK |
| MIME/extension spoofing (real PNG bytes, `.svg` filename, `image/svg+xml` Content-Type) | content-sniffed correctly | correctly content-sniffed as PNG (`source_format: "png"`, `parser_name: "raster_calibration_only"`), routed to the raster path, accepted as a genuinely valid PNG | OK — confirms filename/Content-Type are not trusted anywhere in the routing decision |

All pre-existing security controls remain intact. No regression found.

## 14. Sanitized IR Boundary

Queried `floor_plan_import_candidate.raw_geometry` directly in PostgreSQL across all
candidates created in this session: every row is a clean, structured JSON object
(`{"x":..., "y":..., "text":..., "width":..., "height":..., "radius":..., "shape_type":...}`)
— no raw XML, no markup, no `Element` objects. A targeted search
(`raw_geometry::text ilike '%script%' or '%onload%' or '%evil%'`) across every stored
candidate returned **zero matches**, including for the `<script>`/event-handler/malicious-href
test cases in §13 whose dangerous content was in the same uploaded documents. Confirmed
the boundary is not bypassed.

## 15. Authoritative-State Boundary

Queried floor plans where every associated import job had `status='failed'`: confirmed
**zero** `SpatialObject` rows exist for any of them. No automatic promotion of rejected
or partially-processed imports to authoritative state. Candidate review workflow (pending
→ accept/reject) remains the only path to authoritative `SpatialObject` creation,
confirmed both at the database level and live in the browser (§19).

## 16. RT-1 Regression

Independently re-tested at **50 concurrent accepts** (more than the implementer's own
20), against a fresh import of 50 rack-like rectangles, real HTTP, `asyncio.gather`:

```
accept status codes: Counter({200: 50})
```

Zero 500s, zero `MultipleResultsFound` or `500 Internal` entries in the Uvicorn log
(`grep -c` returned 0). Direct database query after the run: **exactly 1**
`spatial_layer` row with `layer_type='imported'` for that floor plan, and **exactly 50**
`spatial_object` rows referencing it — no duplicates, none missing. The partial unique
index (`uq_spatial_layer_one_imported_per_floor_plan`) is present in the schema, unchanged.
RT-1 remains fixed.

## 17. Database Invariants

Confirmed via direct schema inspection (`\d spatial_layer`) that the partial unique
index and its `WHERE layer_type::text = 'imported'::text` predicate are present exactly
as RT-1's correction established. Did not re-run the full Phase 2 invariant suite
exhaustively (placement U-range exclusion constraints, front/rear semantics, etc.) since
this correction's diff does not touch `RackPlacement`/`EquipmentPlacement` code at all —
consistent with the instruction to concentrate on regression-sensitive invariants rather
than re-running everything for volume. The full backend test suite (203 tests, §24)
includes this codebase's own placement-constraint regression tests and passed cleanly,
which is corroborating (not independent) evidence those invariants still hold.

## 18. Audit/Outbox

- **Audit log**: queried the real `audit_log` partitioned table directly (note: the
  correct partition is the current month, `audit_log_2026_09` — querying only
  `audit_log_default` undercounts). Confirmed audit entries exist for every upload
  (`floor_plan.import_upload`) and every RT-1 accept (`floor_plan.candidate_accept`)
  performed in this session, with correct counts (50 uploads → 50 audit rows in one
  count check, 50 accepts → 50 audit rows). No missing or duplicate entries observed.
- **Outbox**: confirmed via `grep` that `write_outbox_event` is called from
  `app/api/v1/floor_plans.py` only for the `floor_plan.activate` endpoint
  (`"FloorPlanRevisionCreated"`), never from the import-job pipeline. Zero outbox events
  is therefore the *correct* expected count for this session's testing (no floor plan was
  activated) — not a defect, and not something this correction's diff touches (confirmed
  via `git diff`, the outbox call sites are unchanged). No false "import succeeded"
  outbox event is possible because the import pipeline never writes one in the first
  place, regardless of success or failure.

## 19. Frontend Validation

Live browser test (Playwright + the pre-installed Chromium, real login, real client-side
navigation — note: the app's auth token is held in-memory only, so a hard page
navigation via `page.goto` to a protected route loses the session and bounces to
`/login`; the test navigates via the app's own `<Link>`/`<NavLink>` elements instead, as a
real user's browser would).

1. Uploaded a fresh 970-level malicious SVG through the actual file input on
   `RoomFloorPlanPage`. The UI showed a red **`failed`** badge next to the filename with
   the exact text **"SVG nesting exceeds the 200-level depth limit"** displayed to the
   user, diagnostics showing `Objects discovered: 0`. No infinite spinner, no blank
   state, no false success. Screenshot captured
   (`/tmp/rt2_validation/after_malicious_upload.png`, not committed — ephemeral test
   artifact).
2. Immediately after, uploaded a valid SVG through the same UI. It showed a green
   **`parsed`** badge, `Objects discovered: 2`, `Racks detected: 1`, and two pending
   candidates with working Accept/Reject buttons — confirming the workflow is not broken
   by a prior malicious upload and the golden path (upload → diagnostics → candidate
   review) remains fully intact.

## 20. Migration Validation

- Confirmed no new migration file exists in this correction's diff.
- Fresh clean install: created a new database, ran `alembic upgrade head` from scratch —
  succeeded, final version `0005_correction` (the same head as before this correction —
  confirms the correction genuinely required no schema change).
- Downgrade/re-upgrade cycle (separate scratch database, `dcim_migtest`, to avoid
  disturbing other evidence): `alembic downgrade -1` then `alembic upgrade head` —
  both succeeded cleanly, final version `0005_correction`.

## 21. Carry-Forward Findings

- **NEW-1 (Medium)** — idempotency stale-reclaim race. Confirmed via
  `git diff a3a79fe 0afe8dc -- backend/app/application/idempotency.py` that this file has
  **zero changes** between the commit where NEW-1 was proven and the commit under test
  here. Status unchanged: **CARRY-FORWARD, Medium, not re-fixed, not re-tested this
  round** (the master prompt's own instruction not to silently reclassify a carry-forward
  as fixed applies directly here — I did not re-run the original NEW-1 reproduction this
  session, since the relevant code path is byte-for-byte unchanged and re-verified as
  such).
- **NEW-2 (Low)** — constraint naming / migration downgrade issue. Not independently
  re-tested this round; carried forward unchanged (out of scope for this defect's
  revalidation, and the correction's diff does not touch migrations or constraint
  definitions at all).
- **RT-3 (Low/Observation)** — `list_racks` N+1. Not independently re-tested this round;
  carried forward unchanged (the correction's diff does not touch racks/equipment code
  at all).

## 22. New Findings

**FINDING R2-1 (LOW / Observation)** — `MAX_ELEMENTS`'s soft-cap enforcement can be
exceeded by up to ~130× within a single element's immediate-children collection before
the cap takes effect, via a wide (not deep) `<text>` subtree.

- **Component**: `app/application/svg_sanitizer.py`, `_walk` (both the pre-correction
  recursive version and the post-correction iterative version — confirmed identical
  logic via `git diff`; **not introduced by this correction**).
- **Precondition**: authenticated user with `floor_plan:import` permission (same
  precondition as the original nesting defect — not a public/unauthenticated surface).
- **Reproduction**: a single 5MB SVG file (`MAX_SVG_FILE_SIZE_BYTES`) containing one
  `<text>` element with 655,335 self-closing `<tspan/>` children (all siblings, depth 1 —
  no nesting involved at all) is accepted with `status: "parsed"` in ~2 seconds.
  Diagnostics report `objects_discovered: 5000` (`MAX_ELEMENTS`), understating the true
  element count in the document by ~130×.
- **Observed result**: `parsed`, `objects_discovered: 5000`, `duration_ms: ~1586–1970`.
- **Expected result** (per the stated design intent of `MAX_ELEMENTS`, "a hard
  element-count cap enforced *during* the walk... bounding both memory and wall-clock
  time"): either a materially lower processing cost proportional to `MAX_ELEMENTS` itself,
  or an explicit rejection rather than a silent, generously-bounded acceptance.
- **Impact**: bounded, not unbounded — the pre-existing 5MB file-size cap caps the
  achievable element count at roughly 655K for this specific shape of attack (a single
  wide `<text>`), and the measured cost (~2 seconds) is modest, not a worker-starvation
  event on its own. It does not crash the worker, does not leave a stuck job, does not
  bypass the depth cap (this is purely a breadth-side observation), and does not weaken
  any of the security controls under revalidation. Its practical effect is limited to
  diagnostics under-reporting true document complexity and a higher-than-`MAX_ELEMENTS`-
  implies processing cost per malicious upload, which — if an attacker submitted many
  such uploads concurrently — would add up to a proportionally larger but still
  file-size-bounded amount of serialized Celery worker time (e.g. 50 concurrent copies
  ≈ 100 seconds of worker time, comparable in order of magnitude to just uploading many
  legitimately-large files at the existing size cap, not a qualitatively new attack
  surface).
- **Evidence classification**: **PROVEN** — directly reproduced twice (direct function
  call and real HTTP → Celery → PostgreSQL), with exact timing and diagnostics captured
  in §12.
- **Blocks Phase 2?** **No.** It is bounded by an existing, unmodified security control
  (the 5MB file-size cap), does not affect the specific nesting-depth/stuck-job defect
  this revalidation was scoped to, predates this correction (confirmed via diff — this
  correction did not introduce or worsen it), and does not meet the Critical/High/blocking-
  Medium bar the approval criteria set. Recorded here for completeness and honesty, not
  as a blocker, per explicit instruction not to inflate severity without evidence and not
  to silently fix things out of scope.

No other new findings. All bypass attempts against the depth cap itself (§8) failed to
find a genuine gap.

## 23. Evidence Matrix

| Claim | Classification | Evidence |
|---|---|---|
| RecursionError no longer occurs at any tested depth (970 to 349,512) | PROVEN | §5, §7, §12 |
| No off-by-one at the 200-level boundary | PROVEN | §7 |
| No bypass via alternate container tags (`<svg>`, mixed, `<a>`) | PROVEN | §8 |
| `<use>`-nested depth is safe (never descended into) | PROVEN | §8 |
| Generic backstop handles 5 distinct exception types, not just RecursionError | PROVEN | §9 |
| No leaked exception internals in `rejection_reason` | PROVEN | §9 |
| No infinite retry / task duplication | PROVEN | §10 |
| Worker kill/restart recovers correctly | PROVEN | §10 |
| Concurrent malicious+valid+malformed imports don't cross-contaminate | PROVEN | §11 |
| Existing SVG/XML security controls intact | PROVEN | §13 |
| SIR boundary intact (no raw markup escapes) | PROVEN | §14 |
| Authoritative-state boundary intact (no promotion of failed imports) | PROVEN | §15 |
| RT-1 concurrency invariant intact at n=50 | PROVEN | §16 |
| Audit logging intact and correct | PROVEN | §18 |
| Outbox correctly uninvolved in import path (not a regression) | PROVEN | §18 |
| Frontend shows terminal `failed` state, not a spinner | PROVEN | §19 |
| Migration: no new migration; clean install + downgrade/upgrade cycle works | PROVEN | §20 |
| NEW-1/NEW-2/RT-3 code paths genuinely untouched by this diff | PROVEN | §21 |
| `MAX_ELEMENTS` soft-cap can be exceeded ~130× via wide `<text>` (bounded, not blocking) | PROVEN | §12, §22 |
| Multi-Uvicorn-worker-process concurrency | NOT REPRODUCED | §3, §25 (single process only, disclosed) |

## 24. Test Results

- Independent scripts written this session (all in `/tmp/rt2_validation/`, none
  committed): boundary depth test, bypass-attempt test, wide-text-element-count test,
  exception-backstop test (5 exception types), repeated+concurrent-import test, worker
  restart test, RT-1 concurrency test (n=50), security regression matrix, migration
  downgrade/upgrade test, browser Playwright test. All passed / behaved as documented
  above; no assertion failures in any of them.
- Implementer's own test suite, re-run in this session as **corroboration only** (not
  the independent evidence): `backend/tests/` full suite — **203 passed**, 0 failed, via
  `pytest -q` against the project's own `dcim_test` database with the `dcim_app` role
  (unchanged from the implementer's own reported baseline).
- What the implementer's 12 new tests do NOT independently prove, which this session's
  own scripts cover instead: bypass resistance via alternate container tags; the exact
  200-boundary tested via live HTTP rather than direct function call; multiple
  non-`RecursionError` exception types against a real Celery task with a real Postgres
  session (the implementer's own equivalent test uses a monkeypatched `ValueError` only,
  via the FastAPI test client rather than a live worker); RT-1 at n=50 rather than n=20;
  a genuine migration downgrade/upgrade cycle; and live browser confirmation.

## 25. Limitations

- Docker was unavailable; directly-installed PostgreSQL 16 / Redis 7 were used instead
  (disclosed, §3).
- Multi-process Uvicorn (`--workers N`) was not tested; only a single Uvicorn process was
  used. This does not affect the specific defect under revalidation (a single Celery
  task's exception handling), but is disclosed as a gap in this session's concurrency
  coverage generally.
- NEW-2 and RT-3 were not independently re-executed this round — their carry-forward
  status rests on the confirmed fact that this correction's diff does not touch their
  relevant code, not on a fresh reproduction in this session.
- Redis restart during an in-flight import (as distinct from a Celery worker restart, or
  a Docker-level restart) was not separately tested.
- FINDING R2-1 (§22) was measured up to ~655,000 elements (bounded by the 5MB file-size
  cap for a single upload); concurrent-upload amplification of this finding was reasoned
  about analytically (§22) rather than empirically measured under real concurrent load.

## 26. Final Verdict

**PHASE 2 INDEPENDENT RED-TEAM REVALIDATION PASSED — PROCEED TO PHASE 3**

The nested-SVG defect is closed: no `RecursionError` at any tested depth, no bypass of
the depth cap found across multiple independent attempts, the job state machine always
reaches a terminal state (verified for the specific original exception type and four
others), Celery behavior is safe (no retries, correct worker-restart recovery), existing
security controls and architectural boundaries (SIR, authoritative-state, audit, outbox,
RT-1) remain intact, migrations are unaffected, and the real browser UI correctly shows
the terminal failure state rather than an indefinite spinner. One new LOW/Observation
finding (R2-1) was discovered and is fully disclosed; it predates this correction, is
bounded by an existing unmodified control, and does not meet the bar to block Phase 2 per
the stated approval criteria. Carry-forward findings NEW-1 (Medium), NEW-2 (Low), and
RT-3 (Low/Observation) are confirmed unchanged and, per instruction, do not block Phase 2
on their own.

STOP — PHASE 2 INDEPENDENT RED-TEAM REVALIDATION COMPLETE
