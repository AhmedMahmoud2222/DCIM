# ARCHITECTURE_TARGETED_REVISION_REPORT.md

**Revision:** v1.1 → v1.2 (targeted)
**Date:** 2026-09-16

---

## 1. Executive Summary

This is a targeted revision, not a redesign. It resolves exactly nine findings from `ARCHITECTURE_RED_TEAM_REPORT.md` — the five mandatory CRITICALs (C1–C5), two strongly-recommended HIGHs (H1, H6), one decision-status escalation (H7), and one naming reconciliation (M1) — and touches no other part of the architecture. `ManagedAsset`, `PowerNode`, `EquipmentPlacement`/`RackPlacement`, the Protocol→Driver→VendorProfile→DeviceProfile integration layering, and the Outbox pattern are unchanged; every fix in this revision is an addition or correction *within* the sections that already defined those concepts, not a replacement of them.

Each fix followed the same discipline: locate the exact defect, validate whether the red-team's characterization actually holds (it did, in every case examined — none of C1–C5/H1/H6/H7/M1 turned out to be a false positive), correct only what's needed, define the invariant's enforcement across all five layers (DB/domain/API/audit/acceptance), and check that no correction introduced a second source of truth. In two places (C3, C4) the fix adopted was **not** the red-team report's literal suggestion — a more robust mechanism was substituted after validation, and the reasoning for preferring it is recorded in both `ARCHITECTURE_REVIEW.md` and here.

---

## 2. v1.1 Baseline

`ARCHITECTURE_REVIEW.md` v1.1 is the baseline this revision modifies in place. Its core decisions — modular monolith, PostgreSQL system of record, `ManagedAsset` shared-PK identity, `PowerNode` real-FK topology, temporal placement records, adapter-based integrations, quality-tagged telemetry, the Outbox pattern — are all preserved without exception, per the explicit "do not create architectural churn" instruction.

---

## 3. Red-Team Findings Addressed

C1, C2, C3, C4, C5 (mandatory blocking), H1, H6, H7 (strongly recommended), M1 (reconciliation). Full before/after detail for each is in `ARCHITECTURE_REVIEW.md`'s §4a/§4b/§7a/§7b/§7c/§10a/§13a/§30a/§32a and summarized in `ARCHITECTURE_CHANGE_MATRIX.md`. This report does not repeat the full technical detail — it summarizes disposition and reasoning.

---

## 4. C1 Resolution — Asset Replacement

**Located:** §4 (v1.1) had no defined procedure for physical-asset replacement.
**Validated:** Confirmed. The gap was real and foundational — every cross-cutting domain joins on `ManagedAsset.id`, so an undefined replacement procedure risked silent, permanent historical misattribution the first time a rack or piece of equipment was physically swapped.
**Corrected:** A new `ReplaceAsset` domain operation (§4a) explicitly distinguishes "moved" (same `ManagedAsset.id`, new placement row) from "replaced" (new `ManagedAsset.id`, `replaces_asset_id` linking it to the retired one). The operation is transactional: retire the old asset, create the new one, transfer placement through the *ordinary* placement-open workflow (never by editing the old placement row), auto-clear any open alarms against the retired asset, and leave all prior telemetry/audit/external-reference history untouched and correctly attributed to the old identity forever.
**Enforcement defined:** `UNIQUE(replaces_asset_id) WHERE NOT NULL` (DB) → lifecycle-state validation + centralized operation (domain) → dedicated endpoint, single transaction (API) → `AssetReplaced` event + required-`reason` audit entry (audit/event) → "every reference to one id describes one physical object for its whole history" (acceptance).
**Boundary check:** no second source of truth — `replaces_asset_id` is succession metadata; the reverse lookup is a query, not a duplicated column.

---

## 5. C2 Resolution — U-Range Exclusion Semantics

**Located:** §7 (v1.1)'s single `EXCLUDE ... side WITH <>` constraint.
**Validated:** Confirmed, and demonstrated mechanically: a GiST exclusion constraint rejects only when *all* operators evaluate true, so `side WITH <>` rejected exactly the front/rear coexistence case it should have allowed, and permitted exactly the same-side overlap it should have blocked.
**Corrected:** The semantic truth table was treated as the specification, not a suggestion. Two generated boolean columns (`occupies_front`, `occupies_rear`, derived from `side`) plus two partial GiST exclusion constraints — one per occupied plane — were derived independently and checked row-by-row against every entry in the truth table before being adopted. The red-team's literal SQL suggestion was not copied; this mechanism was verified to satisfy all nine rows of the required table, including the "no overlap → always allowed" and "front vs. rear → allowed" cases specifically.
**Enforcement defined:** two partial GiST exclusion constraints (DB) → pre-validation before insert (domain) → `409` with conflict detail (API) → not audited (rejected attempts changed nothing) → truth table passes, pending PostgreSQL integration test (acceptance — explicitly not claimed as tested, per the instruction against inventing evidence).
**Boundary check:** no second source of truth — the two boolean columns are `GENERATED ... STORED` projections of `side`, never independently writable.

---

## 6. C3 Resolution — Exactly-One-Current-Placement

**Located:** §7/§8 (v1.1), backed only by a non-enforcing index comment.
**Validated:** Confirmed — nothing prevented two simultaneously-"current" placement rows for one asset, directly undermining §12 (Single Source of Truth).
**Corrected:** The red-team's own suggestion (`UNIQUE(asset_id) WHERE effective_to IS NULL`) was validated as correct but superseded by a strictly stronger alternative: a range-exclusion constraint over `(asset_id, tstzrange(effective_from, effective_to))`, which subsumes "at most one current" as a special case (an open interval is unbounded-upper, so two attempts to open a second current row necessarily range-overlap) while also catching the broader case of any two historical intervals for the same asset overlapping. The simpler partial-unique form is documented as an equally valid fallback if the range-based form is judged too implicit for the implementation team. The distinct, weaker invariant "an *active* asset must have exactly one current placement" was recognized as a cross-table existence question Postgres cannot cleanly enforce declaratively, and was routed to domain-layer enforcement (activation blocked without a current placement) plus a scheduled data-quality check reusing the existing Alarm/Event machinery — stated explicitly rather than left unaddressed.
**Enforcement defined:** range-exclusion constraint (DB) → close-then-open workflow (domain) → §7c's concurrency mechanism (API) → placement-change event (audit) → "two concurrently-current rows are impossible," pending PostgreSQL integration test (acceptance).
**Boundary check:** no second source of truth — one constraint, applied to the existing temporal columns, no duplicated "current" flag introduced.

---

## 7. C4 Resolution — Concurrency Control

**Located:** §36 (v1.1) scoped `version`/`If-Match` to `Rack`, `Equipment`, `AlarmRule` — none of which hold placement/topology data after v1.1's own restructuring.
**Validated:** Confirmed — the tables operators actually contend on (`RackPlacement`, `EquipmentPlacement`, `PowerConnection`) had no stated mechanism.
**Corrected:** Rather than uniformly bolting `version`/`If-Match` onto every table (which does not handle a multi-row close-then-open "move" correctly), the mechanism was matched to the shape of each operation. Placement moves use `SELECT ... FOR UPDATE` on the current-placement row, relying on standard, well-documented Postgres `EvalPlanQual` re-check behavior under READ COMMITTED to surface a clean `409` to a transaction that loses a race — this was reasoned through explicitly (not asserted) to confirm it produces the correct observable behavior (zero rows returned post-unblock, not a stale row). `PowerConnection`, a single-row edit target, uses straightforward `version`/`If-Match`, with `FOR UPDATE` layered on specifically for the disconnect-vs-modify race. The interaction with Idempotency-Key handling (§21) was made explicit: idempotency check first, concurrency check second, so a safe retry of an already-succeeded request never produces a false conflict.
**Enforcement defined:** row locks / `version` columns (DB) → locked transaction pattern per operation type (domain) → `409` + fresh state, or idempotent no-op for same-intent retirement (API) → only the winning transaction is audited/evented (audit) → confirmed correct by construction against documented Postgres semantics, pending integration test under real concurrent load (acceptance).
**Boundary check:** no second source of truth — the lock targets the same rows the C2/C3 constraints protect; nothing cached or duplicated.

---

## 8. C5 Resolution — Floor-Plan Import Security Boundary

**Located:** §10 (v1.1) had no stated parsing safety boundary at all.
**Validated:** Confirmed — a real, previously entirely unaddressed trust boundary (untrusted user-uploaded SVG/DXF/PDF/VSDX reaching a parser with no stated isolation, in a runtime that also holds database credentials elsewhere).
**Corrected:** The existing v1.1 pipeline already had a natural seam ("Geometry Normalization"); v1.2 inserts the trust boundary one step earlier, enclosing the parser itself: Upload → Quarantine → Validation (content-sniffed type check, size cap, decompression-ratio guard) → Isolated Parse (credential-less, network-restricted, resource-bounded worker; XXE/DTD disabled; external references stripped; no script execution) → Sanitized Intermediate Representation → the rest of v1.1's pipeline, unchanged, operating only on the SIR. A malware-scan hook, audit trail, cleanup policy, and site/RBAC-inherited authorization were specified at the minimum level the instructions asked for, explicitly avoiding over-building this into a full malware-defense platform.
**Enforcement defined:** execution-boundary controls, not a DB constraint (DB — N/A by design) → the pipeline stages themselves (domain) → async job with diagnostics (API) → import diagnostics + audit trail (audit) → "a malformed/malicious file produces a failed job, never a partial write, never uncontrolled parser execution" — **architecturally defined; requires security testing to validate control effectiveness** (acceptance, stated per the instruction against claiming untested security controls work).
**Boundary check:** the SIR is not a new source of truth — it is the same intermediate artifact v1.1 already treated as review-gated, with the boundary moved one step earlier.

---

## 9. H1 Resolution — Complete ManagedAsset Subtype Definitions

**Located:** §4 (v1.1) discussed the shared-PK pattern narratively without republishing subtype schemas.
**Validated:** Confirmed — an implementer would have needed to reconstruct `Rack`/`Equipment`/`PDU`/`UPS`/`Generator`/`PowerPanel`/`Sensor` by merging v1.0 and v1.1, and PDU/Sensor placement was not addressed by v1.1's `EquipmentPlacement` fix at all.
**Corrected:** §4b publishes complete column lists for every subtype, states the rule once ("`ManagedAsset` = identity/lifecycle; subtype = domain-specific state, never duplicated"), answers the explicit "is `PDUOutlet` a `ManagedAsset`?" question (no — it has no independent lifecycle, identified via its owning PDU's id plus an outlet number), and makes an explicit, justified choice per subtype between temporal placement (`EquipmentPlacement`, for Rack/Equipment/PDU/Sensor — things that move with some regularity) and a plain FK (for UPS/Generator/PowerPanel — fixed installations relocated only as major projects). The extensibility pattern (add an enum value + a thin subtype table + a placement-mechanism choice + optional `PowerNode`/`EquipmentInterface` rows) was verified against every future class named in the request (CRAC/CRAH/Chiller/ATS/Transformer/Busway/RackPDU/SmartCabinet) — none requires touching `ManagedAsset` itself.

---

## 10. H6 Resolution — AuditLog Retention/Partitioning

**Located:** §38 (v1.1) partitioned only `TelemetryReading`/`Event`; §30's correct append-only DB grant had no reconciliation with future retention needs.
**Validated:** Confirmed — an identical unbounded-growth profile to `Event`, omitted from the partitioning strategy; and a genuine mechanical conflict (the role that can't `DELETE` also can't purge, with no stated alternative path).
**Corrected:** `AuditLog` added to the monthly partitioning strategy. A separate, explicitly privileged database role, used only by a scheduled maintenance job (never the application, never a human directly), is defined as the sole path for detaching/archiving/dropping old partitions. Default posture is archive-don't-delete given audit data's evidentiary value. The actual retention *duration* is correctly left as an open compliance/legal decision — not invented here — while the *mechanism* is fully specified regardless of what duration is eventually approved.

---

## 11. H7 Decision Status

**Located:** §32/§49 (v1.1) — already disclosed as deferred, not a hidden defect.
**Validated:** Confirmed as needing explicit escalation — Phase 1 builds the RBAC foundation itself, so a decision reached by default silence is a different (worse) thing than one made knowingly.
**Disposition:** Per instruction, this document does **not** select an option. §32a records Options A/B/C exactly as they must be presented to the architecture owner, notes Option B as the lower-cost default absent a conflicting need, and marks the decision status as **MANAGEMENT DECISION REQUIRED**. This is now an explicit, blocking Phase 1 entry criterion (§47a) rather than an assumption Phase 1 could silently proceed under.

---

## 12. M1 Resolution — Identity-Reference Naming

**Located:** §4's diagram named `Alarm.managed_asset_id`/`Event.managed_asset_id`; §16/§25's actual table definitions used `object_class`/`object_id`.
**Validated:** Confirmed as a genuine documentation-level inconsistency (not a functional defect — the intent was inferable, but the two sections literally disagreed on a column name).
**Corrected:** §4's diagram now reads `object_id (when object_class='managed_asset')` for `Alarm`/`Event`/`TelemetryReading`/`ExternalReference`, explicitly stating this is the one pattern used everywhere this kind of reference is needed, with `ManagedAsset` as the identity for physical assets and `object_class`/`object_id` as the necessarily-polymorphic pattern for the handful of non-asset domain objects (Room/Site/Building/Floor) that also need to be referenced. No duplicate identity mechanism was introduced — this was a naming fix, not a schema change.

---

## 13. Invariant & Enforcement Matrix

Published in full as new §47b of `ARCHITECTURE_REVIEW.md`. Not repeated here in full; every row traces one invariant through Source of Truth → DB Enforcement → Domain Enforcement → API/Concurrency → Audit/Event → Acceptance Criterion.

---

## 14. Before/After Traceability

Provided inline, per finding, in §4a/§4b/§7a/§7b/§7c/§10a/§13a/§30a/§32a of `ARCHITECTURE_REVIEW.md` — each carries its own Before/Problem/After/Enforcement/Impact/Non-impact structure via the "Step A–F" method required by this revision's instructions. Not duplicated here to avoid two documents drifting out of sync; `ARCHITECTURE_REVIEW.md` is the authoritative copy.

---

## 15. Phase Impact

Full table in `ARCHITECTURE_REVIEW.md` §47a. Summary: no phase was reordered. C1/H1 add a prerequisite to Phase 2 (asset replacement + complete subtype schemas exist before Phase 2's exit gate). C2/C3 add a prerequisite to Phase 5 (and confirm work already implicitly needed in Phase 2/4). C4 adds a prerequisite to Phase 1 (the API concurrency convention itself) and Phase 7 (`PowerConnection.version`). C5 adds explicit in-scope work to Phase 6 (the import security boundary is now part of that phase's exit gate, not an afterthought). H6 adds a prerequisite to Phase 1 (partitioned `AuditLog` + privileged role). H7 is a **blocking Phase 1 entry criterion** — the one place this revision found an actual gate condition, not just a clarification. M1 changes no phase.

---

## 16. Deferred Findings

Full table in `ARCHITECTURE_CHANGE_MATRIX.md`. H2–H9 and M2–M10 remain exactly as the red-team report found them — none silently absorbed, none fixed under cover of this revision's broader scope. Each has a stated reason for deferral, an affected future phase, and an explicit "blocks Phase 1: No."

---

## 17. Consistency Check

Cross-document review performed across the dimensions requested:

- **Identity:** `ManagedAsset` (physical assets) vs. subtype ids (shared PK, not separate) vs. `asset_tag` (mutable, human-facing) — consistent throughout §4/§4a/§4b, no contradiction found.
- **Placement:** `RackPlacement`/`EquipmentPlacement` (temporal, authoritative) vs. `SpatialObject`/`FloorPlan` (geometry, linked 1:1 optionally) — consistent; §7/§8's room-consistency trigger (v1.1, unchanged) and the new range-exclusion constraints (v1.2) operate on the same tables without conflict.
- **Spatial:** Room ownership of exactly one active `FloorPlan`, coordinate system (§9, unchanged), placement history (§7b/§8) — consistent.
- **Power:** `PowerNode` vs. `PowerConnection` vs. `PDUOutlet` (now fully specified, §4b) — consistent; `PowerConnection.version` (v1.2) does not conflict with the FK structure (v1.1, unchanged).
- **Network:** `EquipmentInterface` vs. `PortConnection` vs. `NetworkSegment` — untouched by this revision, no new inconsistency introduced.
- **Telemetry:** `ManagedAsset` vs. metric identity vs. collector/source identity — untouched, consistent with the corrected §4 diagram.
- **Alarm/Event:** `ManagedAsset` references and polymorphic targets — reconciled by M1; no remaining naming contradiction found on re-check.
- **Import:** `FloorPlanImporter` vs. candidate detection vs. authoritative inventory — the new isolated-parse boundary (§10a) sits *before* the unchanged candidate-detection/review pipeline; no conflict.
- **Security:** RBAC (§32a, decision pending) vs. importer trust boundary (§10a, inherits whatever RBAC scoping exists) vs. integration egress (unchanged, still an open finding M4) — consistent; §10a explicitly notes it inherits RBAC scoping rather than defining its own.
- **Audit:** `AuditLog` (now partitioned, §30a) vs. domain events vs. Outbox — consistent; the distinction between synchronous audit writes and asynchronous Outbox-delivered side effects (v1.1, unchanged) still holds.
- **Temporal:** `effective_from`/`effective_to` vs. `occurred_at`/`recorded_at` vs. `created_at`/`updated_at` — the new range-exclusion constraints use `effective_from`/`effective_to` exactly as the v1.1 temporal model (§29, unchanged) defines them; no new temporal concept was introduced.
- **Concurrency:** `version`/`If-Match` (now correctly scoped, §36) vs. database constraints (§7a/§7b) vs. transaction locking (§7c/§13a) — the three are explicitly reconciled as complementary, not competing, mechanisms (locking produces a clean `409`; constraints are the backstop for any path that bypasses the locked service transaction).
- **Lifecycle:** active → maintenance → retired/removed → replacement — the replacement operation (§4a) is now an explicit, defined extension of the lifecycle state machine, not a gap.

No contradiction was found between any two sections after these edits. The one contradiction the red-team review identified (M1) is resolved.

---

## 18. Remaining Open Decisions

Unchanged list from `ARCHITECTURE_REVIEW.md` §49, plus:
- **RBAC site-scoping timing (§32a)** — escalated from "open" to "open, blocking Phase 1 entry" in this revision.
- **AuditLog retention duration (§30a)** — new in this revision; mechanism is specified, the number is not.

Neither of these is a defect in the architecture — both are organizational decisions the architecture correctly refuses to make on its own authority.

---

## 19. Remaining Risks

Carried forward from `ARCHITECTURE_REVIEW.md` §50 (renumbered §-risks list), plus two new risks added directly by this revision (items 8–9 in that list): the corrected exclusion constraints and concurrency mechanisms are validated by construction and against documented Postgres semantics, but **not yet validated against a running PostgreSQL instance** — every one of C2/C3/C4's mechanisms requires a PostgreSQL integration test before the phase that depends on it exits; and the C5 import security boundary requires security testing to validate actual control effectiveness, not just architectural presence. Both risks are stated using the required language ("requires implementation validation," "requires PostgreSQL integration test," "requires security testing") rather than asserted as proven.

---

## 20. Final Architecture Gate Status

**ARCHITECTURE v1.2 TARGETED REVISION COMPLETE**

Files changed:
- `ARCHITECTURE_REVIEW.md` — updated in place to v1.2 (§4/§4a/§4b/§4c, §7/§7a/§7b/§7c, §8, §10/§10a, §13/§13a, §30/§30a, §32/§32a, §36, §38, §46 (AD21–AD25), §47/§47a/§47b, §49, closing status)

Files created:
- `ARCHITECTURE_CHANGE_MATRIX.md`
- `ARCHITECTURE_TARGETED_REVISION_REPORT.md` (this file)

Findings resolved: C1, C2, C3, C4, C5, H1, H6, M1.
Findings explicitly blocked with documented reason (not silently deferred, not silently decided): H7.
Findings deferred, unchanged, tracked: H2, H3, H4, H5, H8, H9, M2, M3, M4, M5, M6, M7, M8, M9, M10.

Decisions still required: RBAC site-scoping timing (Option A/B/C, §32a — **blocks Phase 1 entry**); AuditLog retention duration (mechanism ready, number pending, does not block Phase 1); every open decision already listed in v1.1 §49 (single-vs-multi-tenant confirmation, Postgres extension availability, priority SNMP vendors, VSDX support level, etc.) remains open and unchanged.

Phase 1 implications: Phase 1 cannot begin until the RBAC decision is recorded (§32a). Once recorded, Phase 1's scope gains two concrete, bounded additions versus v1.1: the `FOR UPDATE`/optimistic-concurrency API convention (§7c/§13a) as part of its API-conventions foundation, and `AuditLog`'s partitioned schema plus the privileged retention role (§30a) as part of its database/audit foundation. Neither is a large addition; both were arguably always implied by v1.1's own stated principles and are now made explicit.

Remaining architectural risks: two new (PostgreSQL integration testing required for the corrected constraints/concurrency mechanisms; security testing required for the import boundary), plus the seven carried forward unchanged from v1.1 (§50, `ARCHITECTURE_REVIEW.md`).

**STOP — WAITING FOR FINAL ARCHITECTURE VALIDATION GATE**

No application code, migrations, endpoints, components, or dependencies were created or modified in this session. This revision is documentation only, per its stated scope.
