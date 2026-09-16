# PHASE1_BASELINE.md

**Assessed:** 2026-09-16
**Repository:** `AhmedMahmoud2222/DCIM`, branch `claude/new-session-1vutvy`

---

## Repository State

**Empty of code.** Six commits exist, all documentation: `ARCHITECTURE_REVIEW.md` (v1.3) and its five companion reports (revision, red-team, change matrix, targeted-revision, final-validation). No `backend/`, `frontend/`, `docker/`, `.github/`, or dependency manifests exist. No prior scaffolding to preserve or conflict with — this is a from-scratch Phase 1, not a merge with existing code.

**Runtime environment available in this session** (checked directly, not assumed): Python 3.11.15, Node 22.22.2/npm 10.9.7, Docker CLI 29.3.1 (daemon not running — no `dockerd` socket), PostgreSQL 16.13 server and client installed but not started, Redis 7.0.15 server installed but not started. PostgreSQL and Redis were started for this session (`service postgresql start`, `redis-server --daemonize yes`) so integration tests can run against real instances rather than mocks. Two databases created: `dcim` (dev) and `dcim_test` (test), owned by a new `dcim_app` role; `uuid-ossp`, `pgcrypto`, and `btree_gist` extensions confirmed installable (`CREATE EXTENSION` succeeded for all three) — this is the first concrete evidence resolving one of the architecture's open decisions (§49: "can the target Postgres hosting install extensions") for *this* environment; production hosting still needs its own confirmation. Docker's daemon being unavailable means `docker compose up`/image builds cannot be executed or verified in this session — noted as a limitation throughout, not glossed over.

---

## Documentation Gap Found During Baseline Review

Before writing code, `ARCHITECTURE_REVIEW.md` was checked for the exact schemas Phase 1 needs (location hierarchy, `User`/`Role`/`Permission`). **Two table groups present in v1.0 (`git show 4a39e1b`) are missing from the current v1.3 text**, dropped when v1.1 rewrote the document around `ManagedAsset`/`PowerNode` — not deliberately removed, not contradicted by anything later, just not restated:

- §6.1 (v1.0): `User`, `Role`, `Permission`, `RolePermission`, `UserRole` — v1.1 restated only `RoleAssignment` (`UserRole`'s replacement, adding `scope_type`/`scope_id`) while discussing RBAC narratively (§32); the base identity/permission tables themselves were never re-published.
- §6.2 (v1.0): `Organization`, `Country`, `City`, `Site`, `Building`, `Floor`, `Room`, `LocationType` — never re-published anywhere in v1.1/v1.2/v1.3, even though AD1 (§46, present in every version since v1.1) explicitly reaffirms discrete relational location tables plus a `LocationType` registry as a preserved decision.

This is the same class of gap H1 found and fixed for `ManagedAsset` subtypes in v1.2 (a real architectural intent that was never re-published as a complete schema) — not a contradiction, since nothing later invalidates either table group; AD1 is direct, unambiguous confirmation the location design is still intended. Per this prompt's own rule (§2: *"If you discover a genuine contradiction... stop... document... do not silently invent a solution"*) — **this is judged not to be a contradiction requiring a stop**, because there is no conflicting statement to reconcile, only a missing restatement of something still affirmed elsewhere. Treating it as a stop-the-line architectural blocker would be inventing a conflict where the record shows continuity of intent instead.

**Resolution taken:** implement both table groups exactly as v1.0 specified them (retrieved via `git show`), since that is the last, and only, authoritative schema for either, and nothing supersedes it. This is recorded as a documentation debt, not a deviation from approved architecture — see `PHASE1_DEVIATIONS.md` item D1, which recommends folding a restatement of both schemas into `ARCHITECTURE_REVIEW.md` in the next routine documentation pass (parallel to how H1 was resolved), owned by the architecture document, not by this implementation.

---

## Existing Components

None. Nothing to preserve, nothing to reconcile with new work.

---

## Gaps This Baseline Must Close

Everything: repository structure, backend package, frontend package, database schema/migrations, Docker Compose, CI, and the five Phase 1 governance documents this prompt requires (`PHASE1_IMPLEMENTATION.md`, `PHASE1_TRACEABILITY_MATRIX.md`, `PHASE1_DEVIATIONS.md`, `PHASE1_IMPLEMENTATION_REPORT.md`, plus this baseline).

## Assumptions

1. "Location hierarchy" and "User/Role/Permission" schemas are taken from v1.0 as retrieved from Git history, per the resolution above.
2. H7's Option B (global authorization, §32a) is implemented literally: `RoleAssignment.scope_type` defaults to `'global'`, the column exists and is populated, but no query anywhere filters by site. This is a deliberate, documented absence, not an oversight.
3. `AuditLog` partitioning and the privileged retention role (H6/§30a) are in-scope for Phase 1 per the architecture's own §47a phase-impact table ("AuditLog's partitioned schema and the privileged retention role are part of Phase 1's database/audit foundation"). Actual retention/archival *logic* is not implemented (retention duration remains an open, non-blocking decision, §49) — only the partitioned schema, the role, and a partition-maintenance job are built.
4. No `Rack`/`Equipment`/`PDU`/etc. subtype tables are created — `ManagedAsset` is implemented as a bare identity/lifecycle anchor per §9 of this prompt, exactly as the architecture requires for Phase 1.
5. Optimistic concurrency (`version`/`If-Match`) is demonstrated on `Room` (a real Phase 1 entity), not a placeholder table, per §17's instruction not to build future domain models merely to demonstrate a mechanism.
6. Idempotency-key handling is demonstrated on the `ManagedAsset` creation endpoint, the one Phase 1 write operation most analogous to the future import/creation flows the mechanism exists for.

## Files to Be Added

Everything under `backend/`, `frontend/`, `docker-compose.yml`, `.github/workflows/`, plus the governance docs above. Full list in `PHASE1_IMPLEMENTATION_REPORT.md`.

## Files to Be Changed

None outside the new additions — no existing code exists to change.

## Files Intentionally Left Untouched

The six architecture documents already in the repository. This implementation reads them; it does not edit them, except where `PHASE1_DEVIATIONS.md` explicitly recommends a future documentation pass (not performed here, per this prompt's own scope rule against silently modifying architectural decisions).
