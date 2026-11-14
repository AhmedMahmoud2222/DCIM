# PHASE2_IMPLEMENTATION_RED_TEAM_SCOPE.md

Scope document for an **independent** Phase 2 red-team — written by the implementer to
orient that review, not a self-assessment. This implementation's own self-adversarial
pass (see `PHASE2_IMPLEMENTATION_REPORT.md` §25) found and fixed four genuine defects
(D1–D4); this document is what remains for someone else to try to break.

---

## 1. Critical invariants to attempt to violate

For each, the claim is that PostgreSQL itself enforces it, not just application code.
Try to reach the database in a way that bypasses the API and violates it directly (e.g.
via `tests/integration/test_phase2_placement_constraints.py`'s style of direct-ORM
inserts, or raw `psql`), and separately via the API with adversarial request bodies.

1. **No two rack-mounted items may occupy the same side of the same rack in an
   overlapping U-range.** (`no_front_overlap`/`no_rear_overlap` partial GiST exclusion
   constraints on `equipment_placement`.) Try: `side='both'` against an existing
   `side='front'` at an overlapping range (should conflict); adjacent-but-not-overlapping
   ranges on the same side (should succeed); overlapping ranges on `front` vs. `rear`
   (should succeed — this is the whole point of the front/rear split).
2. **A `rack_mounted` placement can never have `side IS NULL`, `rack_id IS NULL`, or
   `u_range IS NULL`.** (CHECK `rack_mounted_requires_rack_u_range_and_side`, backed by
   the COALESCE-based generated columns.) Try inserting via raw SQL with the CHECK
   constraint's own column values individually NULL'd, not just via the ORM/API (the ORM
   validator in `EquipmentMoveIn` is a *convenience* 422, not the actual guarantee — the
   guarantee is the DB constraint; confirm the DB rejects it even if the API-level
   validator were somehow bypassed).
3. **At most one current placement per rack/equipment.** (Range-exclusion constraints
   `rack_placement_one_timeline_per_asset`/`equipment_placement_one_timeline_per_asset`.)
   Try racing two genuinely concurrent move requests for the *same* asset (see §3 below)
   and inspect the final DB state, not just the HTTP response codes.
4. **A placement's `room_id` must match the room its linked `SpatialObject` (via that
   object's `FloorPlan`) actually belongs to.** (Trigger
   `check_placement_spatial_object_room_match()`.) Try linking a `RackPlacement` in room
   A to a `SpatialObject` that belongs to a `FloorPlan` for room B.
5. **At most one active `FloorPlan` per room.** (Partial unique index
   `uq_floor_plan_one_active_per_room`.) Try activating two different revisions for the
   same room in rapid succession / concurrently, not just sequentially (D2 in the
   implementation report was found this way — confirm the fix actually holds under real
   concurrency, not just the sequential reproduction that found it).
6. **Imported geometry never becomes an authoritative `SpatialObject` without a human
   `accept` action.** Try to find *any* code path — including the raster/calibration-only
   pipeline, or a malformed candidate accept request — that creates a `source='imported'`
   or `source='authoritative'` `SpatialObject` without going through
   `POST .../candidates/{id}/accept`.

## 2. Security boundaries

- **RBAC**: every new permission (`rack:read/manage/place`, `equipment:read/manage/
  place`, `floor_plan:read/import/manage`, `spatial:read`) should be independently
  tested for every role, not just the pairs this implementation's own tests covered
  (Viewer-cannot-write, Engineer-cannot-accept-candidates). In particular: can an
  Operator (has `rack:place`/`equipment:place` but not `manage`) call `move`/`retire` but
  genuinely not `create`? Can Engineer upload (`floor_plan:import`) but genuinely not
  `activate` or `accept`/`reject` (`floor_plan:manage`)?
- **Untrusted file pipeline** (`app/application/svg_sanitizer.py`): this implementation's
  own adversarial matrix is in the implementation report §14/traceability matrix. Things
  *not* yet tried: a zip bomb disguised with SVG magic bytes at the start (content-
  sniffing only checks the first ~4KB); an SVG with thousands of shallow-nested `<g>`
  wrapper elements each containing one shape (tests whichever recursion-depth limit, if
  any, actually exists — there is currently no explicit recursion-depth cap, only an
  element-*count* cap); Unicode/encoding tricks in `<text>` content reaching the
  `suggested_label`/candidate review UI unescaped.
- **In-memory-only file handling**: confirmed by design there is no on-disk quarantine
  directory a path-traversal filename could target, but confirm no *temp file* is
  created anywhere in the actual request path (e.g. by FastAPI's `UploadFile` spooling
  large uploads to disk — check `python-multipart`'s behavior above its default spool
  threshold, since the Phase 2 upload endpoint's own blanket cap is 20MB, which is above
  the typical default `SpooledTemporaryFile` in-memory threshold).
- **Idempotency-Key reuse**: confirm a client cannot use the same key across the two
  *different* Phase 2 creation endpoints (`POST /racks` vs `POST /equipment`) to somehow
  cross-contaminate results — the claim is scoped by `endpoint` string
  (`app.application.idempotency`), but this was not explicitly tested in Phase 2's own
  suite (only within a single endpoint).

## 3. Attack surfaces requiring genuine concurrency (not sequential simulation)

This implementation's own test
(`tests/api/test_racks.py::test_rack_move_concurrent_movers_only_one_wins_the_other_gets_409`)
demonstrates the pattern needed: independent `AsyncClient`/`AsyncSession` instances per
request (see that test and `tests/integration/test_idempotency_concurrency.py`'s
`per_request_client` fixture for the exact plumbing), never the shared `db_session`
fixture the rest of the suite uses (which cannot safely be used from two coroutines at
once and would not exercise real contention).

Worth extending beyond what this implementation tried:
- Concurrent move + retire on the same rack (one caller moving, one retiring, truly
  simultaneously) — this implementation only tested move-vs-move.
- Concurrent `accept` on two *different* candidates from the *same* import job, racing
  the "auto-create the Imported layer if missing" logic in
  `app/api/v1/floor_plans.py::accept_import_candidate` — the layer lookup-then-create is
  not itself locked; two simultaneous first-accepts for the same job could each decide
  the layer is missing and both attempt to create it. Check whether `SpatialLayer` has any
  uniqueness constraint that would catch this (it does not currently have one scoped to
  `(floor_plan_id, layer_type)` — this is a plausible real gap, not yet proven either way).
- Concurrent `activate` calls for two different revisions of the same room's floor plan.

## 4. Data integrity / NULL semantics to re-verify independently

Do not trust this implementation's own claim that COALESCE fixed the NULL-bypass bug —
re-derive it: what does `NULL IN ('front', 'both')` actually evaluate to in PostgreSQL,
and does the *current* migration DDL (not just the SQLAlchemy model file) actually say
`COALESCE(...)`? (`migrations/versions/0004_phase2_physical_spatial_model.py`, search for
`occupies_front`/`occupies_rear`.) Confirm the migration and the model file agree — they
were edited together via `sed` during development (see conversation history); confirm no
drift was introduced.

## 5. Import job / diagnostics polling race (frontend, lower severity)

The frontend polls `import-jobs`, `import-diagnostics`, and `import-candidates`
independently on fixed intervals rather than any push/websocket mechanism. Confirm there
is no window where a job transitions to `failed` with a `rejection_reason` but the
diagnostics panel silently shows stale or partial data (this implementation's own manual
smoke test only observed the `parsed` success path in detail, not a `failed` mid-poll
transition in the browser — the `failed` path was only verified via direct API assertion
in `test_xxe_attempt_is_rejected_outright`, not visually in the UI).

## 6. Exact repro commands / environment requirements

**Database:** PostgreSQL 16 with `btree_gist`, `uuid-ossp`, `pgcrypto` extensions (same
as Phase 1). **Redis:** any reachable instance for Celery broker/backend and the app's
own `REDIS_URL`. **Python:** 3.11, `backend/.venv` already has all dependencies installed
(`pip install -e .[dev]` from `backend/` if rebuilding).

```bash
# Backend test suite (187 tests, ~65s against a real Postgres/Redis)
cd backend
export DATABASE_URL="postgresql+asyncpg://dcim_app:dcim_dev_password@localhost:5432/dcim_test"
export TEST_ADMIN_DATABASE_URL="postgresql+asyncpg://postgres:postgres_test_admin_password@localhost:5432/dcim_test"
export REDIS_URL="redis://localhost:6379/1"
export JWT_SECRET_KEY="test-only-secret-key-not-for-production-use-32ch"
.venv/bin/python -m pytest -q

# Migration validation (clean install / upgrade / downgrade) — see
# PHASE2_IMPLEMENTATION_REPORT.md §21 for the exact three-pass sequence used; repeat
# against a fresh scratch database, not dcim_test, to avoid disrupting the test suite's
# own fixtures.

# Frontend
cd frontend
npm install
npm run typecheck && npm run lint && npm run build

# End-to-end (manual/Playwright): start uvicorn on :8000 (matching
# frontend/vite.config.ts's dev-server proxy target), a Celery worker with
# `-Q default,maintenance,imports`, and `npm run dev` — see this report's own smoke-test
# scripts' structure (not committed; described in PHASE2_IMPLEMENTATION_REPORT.md §20)
# for the login → locations → racks → equipment → floor-plan-upload → accept-candidate →
# activate → 2D-view sequence that was manually verified.
```

**Docker:** unavailable in the implementer's sandbox (no daemon) — this scope was written
without any containerized runtime validation. If Docker is available to the red-team,
validate `docker-compose up` end-to-end, which was not possible here.

## 7. What was NOT tried (gaps in this implementation's own testing, worth prioritizing)

- Load/scale testing at the stated 10,000+ rack / 100,000+ equipment target — indexing
  was *designed* for this scale but never measured.
- Fuzzing the SVG parser beyond the specific attack shapes listed in §2 above.
- Any test of the `EquipmentModelRevision`/`RackModelRevision` catalog under concurrent
  creation (two clients creating a `RackModel` with the same manufacturer/model_name
  simultaneously — is there a uniqueness constraint? Check
  `app/domain/catalog/models.py`; this implementation added a `UniqueConstraint` but did
  not write a dedicated concurrency test for it).
- Cross-tenant/cross-organization isolation — Phase 2 has no organization-scoping concept
  of its own (rooms belong to the single global location hierarchy Phase 1 established),
  so this is out of scope by inheritance, not overlooked, but worth the red-team
  confirming that assumption still holds.
