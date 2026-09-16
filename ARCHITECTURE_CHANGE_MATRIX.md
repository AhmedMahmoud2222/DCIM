# ARCHITECTURE_CHANGE_MATRIX.md

**Revision:** v1.1 → v1.2 (targeted)
**Source of findings:** `ARCHITECTURE_RED_TEAM_REPORT.md`
**Date:** 2026-09-16

---

## Resolved / Addressed in v1.2

| Finding | Severity | Status | Original Section (v1.1) | Revised Section (v1.2) | Architectural Change | Schema Impact | Domain Impact | Security Impact | Phase Impact | Decision Required |
|---|---|---|---|---|---|---|---|---|---|---|
| **C1** — Asset replacement / physical swap undefined | CRITICAL | **RESOLVED** | §4 | §4a | New `ReplaceAsset` domain operation; explicit distinction between "moved" (same identity) and "replaced" (new identity) | `ManagedAsset.replaces_asset_id` (nullable FK, partial-unique) | Rack/Equipment/PDU/UPS/Generator/PowerPanel modules gain a replacement workflow | None (no new attack surface) | Phase 2 (see §47a) | None |
| **C2** — U-range exclusion predicate inverted | CRITICAL | **RESOLVED** | §7 | §7a | Two GiST partial exclusion constraints on generated `occupies_front`/`occupies_rear` columns, replacing the single `side WITH <>` constraint | `EquipmentPlacement` gains two generated columns, two constraints replace one | Rack elevation / equipment placement service must pre-validate before insert | None | Phase 5 (see §47a) | None |
| **C3** — No DB enforcement of exactly-one-current-placement | CRITICAL | **RESOLVED** | §7, §8 | §7b, §8 | Range-exclusion constraint over `(asset_id, tstzrange(effective_from, effective_to))` on both placement tables | New constraint on `RackPlacement`, `EquipmentPlacement` (no new columns) | Placement service's close-then-open sequencing becomes constraint-guaranteed, not just convention | None | Phase 2/4 (see §47a) | None |
| **C4** — Concurrency control scoped to wrong tables | CRITICAL | **RESOLVED** | §36 | §7c, §13a, §36 | `SELECT ... FOR UPDATE` + `EvalPlanQual`-based conflict detection for placement; `version`/`If-Match` for `PowerConnection` | `version` column added to `EquipmentPlacement`, `RackPlacement`, `PowerConnection` | Move/edit workflows in `rack`, `equipment`, `power` modules must implement the locked transaction pattern | None (concurrency, not a new trust boundary) | Phase 1 (API convention), Phase 7 (`PowerConnection.version`) | None |
| **C5** — No floor-plan parser security boundary | CRITICAL | **RESOLVED** | §10 | §10a | Quarantine → Validation → Isolated Parse (credential-less, network-restricted, resource-bounded) → Sanitized Intermediate Representation → Review → Commit | No schema change; adds `FloorPlanImportDiagnostics` fields for rejection reason (size/ratio/timeout/malformed) | `spatial`/import module gains an isolated execution boundary at implementation time | Directly closes a real trust-boundary gap (untrusted-file parsing) | Phase 6 (see §47a) | None |
| **H1** — Incomplete `ManagedAsset` subtype definitions | HIGH | **RESOLVED** | §4 | §4b | Full column lists for Rack/Equipment/PDU/PDUOutlet/UPS/Generator/PowerPanel/Sensor; explicit placement-mechanism choice (temporal vs. plain FK) per subtype; extensibility pattern for future classes (CRAC/CRAH/Chiller/ATS/Transformer/Busway/RackPDU/SmartCabinet) | New `PDUOutlet` table (keyed by `PowerNode`, not `ManagedAsset` — explicitly justified); no changes to existing tables beyond what C1/H1 jointly require | Removes ambiguity Phase 2/3/4/7 would otherwise have had to resolve independently (and possibly inconsistently) | None | Phase 2/3/4/7 — clarifies existing scope, adds no new work | None |
| **H6** — AuditLog excluded from partitioning; retention vs. append-only-grant conflict | HIGH | **RESOLVED** | §30, §38 | §30a, §38 | Monthly partitioning added for `AuditLog`; a separate privileged DB role (e.g. `dcim_retention_admin`) defined for retention/archival, distinct from the application role | `AuditLog` becomes a partitioned table; new DB role (infra, not schema) | Audit module's growth becomes operationally bounded; application code's privileges are unchanged (still no UPDATE/DELETE) | Strengthens the existing tamper-evidence guarantee — application code still cannot purge history | Phase 1 (mechanism) | **Retention duration** (the actual "keep for N years" figure) — compliance/legal decision, not invented here |
| **H7** — RBAC site-scoping timing left as silent default | HIGH | **RESOLVED (v1.3)** | §32, §49 | §32a | Decision recorded: Option B. Architecture unchanged — the finding was that a decision needed to be made explicitly, and it now has been (§32a) | None | None | Determines that every authenticated user can read/write every site's data from Phase 1 onward — now an **accepted, documented** condition, not an open question | Phase 1 proceeds as originally scoped; enforcement is a defined later addition | None remaining — decision made by the architecture owner |
| **F1** — `EquipmentPlacement` U-range exclusion has a residual gap: `side` is nullable, so a NULL-side rack-mounted row escapes both partial exclusion constraints | HIGH (found at final validation gate, not in the original red-team report) | **RESOLVED (v1.3)** | §7 (v1.2's `EXCLUDE`/`CHECK` block) | §7 | `CHECK` on `EquipmentPlacement` extended to also require `side IS NOT NULL` for rack-mounted placements | One clause added to an existing `CHECK` constraint — no new column, no new constraint | None | None | Phase 2/5 — closes the gap before either phase's exit gate | None |
| **M1** — `Alarm`/`Event` naming inconsistent with `object_class`/`object_id` pattern | MEDIUM | **RESOLVED** | §4 | §4, §16, §25 (cross-reference only) | §4's diagram corrected to use `object_class`/`object_id` (matching §16/§25) instead of a non-existent `managed_asset_id` column name | None (documentation-only — no column named `managed_asset_id` ever existed on `Alarm`/`Event`; the fix is in the description, not the schema) | None | None | None | None |

**On H7's disposition (updated, v1.3):** H7 is no longer "explicitly blocked" — the architecture owner recorded Option B in response to the final validation gate's question, closing it the same way C1–C5 close: with a concrete, verifiable change (§32a now states a decision rather than requesting one).

**On F1:** found independently at the final architecture validation gate (`FINAL_ARCHITECTURE_VALIDATION_REPORT.md`), not in the original `ARCHITECTURE_RED_TEAM_REPORT.md` — recorded here because it is, in substance, a residual piece of C2 that survived the v1.2 pass. F2–F6 (the final gate's other findings) remain open and are tracked in that report's addendum, not repeated here since none of them block Phase 1.

---

## Out-of-Scope / Future Revision (discovered during this pass, not fixed now)

Per the strict-scope instruction, these were noticed while resolving H1/C4 but are **not** addressed in v1.2:

| Item | Where noticed | Why deferred |
|---|---|---|
| `EquipmentPlacement.equipment_id` / `EquipmentInterface.equipment_id` column naming | §4b/§4c, while generalizing placement scope to PDU/Sensor | Cosmetic — fixes no confirmed defect on its own; renaming a working column for clarity is unscoped churn. Tracked for a future non-targeted cleanup pass. |
| Power-graph self-loop/cycle prevention (`source_node_id <> target_node_id`, recursive-CTE cycle guard) | §13a, while adding `PowerConnection.version` | This is red-team finding **H4**, explicitly outside this revision's mandatory scope (C1–C5, H1, H6, H7, M1). Not touched. |

---

## Deferred Findings (from `ARCHITECTURE_RED_TEAM_REPORT.md`, unchanged in this revision)

| Finding | Why Deferred | Affected Future Phase | Gate That Must Resolve It | Blocks Phase 1? | Changes Current Architecture? |
|---|---|---|---|---|---|
| H2 — Asset-correlation algorithm (deterministic/probabilistic/manual) undefined | Out of this revision's mandatory scope; matters when `discovery` module is actually built | Phase 8 (Integrations) | Phase 8 exit gate | No | No — `DiscoveredDevice.matched_asset_id` schema already accommodates any algorithm choice |
| H3 — Import repeatability: no position-based reconciliation for non-identical re-imports | Out of scope; mitigated today by the human-review gate (no auto-creation) | Phase 6 (Floor Plan Import) | Phase 6 exit gate | No | No — additive to the existing candidate-review pipeline |
| H4 — Power graph: no self-loop/cycle guard, no delete-cascade definition | Explicitly named out-of-scope for this revision (§13a note) | Phase 7 (Power) | Phase 7 exit gate | No | No — additive constraints/rules on top of existing `PowerNode`/`PowerConnection` |
| H5 — Power capacity rollup/double-counting formula undefined | Out of scope; a calculation-correctness question in the derived `CapacitySnapshot` layer, not the authoritative graph | Phase 7 (Power) / Phase 12 (Capacity) | Phase 7 or 12 exit gate | No | No — `PowerCapacity` schema already holds the needed fields; only the formula is missing |
| H8 — Telemetry ingestion rate at target scale unproven, no batching mechanism specified | Out of scope; matters starting at Phase 9 | Phase 9 (Telemetry) | Phase 9 exit gate — requires a load-test benchmark | No | Possibly — may require a stated batching mechanism, to be evaluated at Phase 9 planning |
| H9 — Alarm re-trigger (clear then re-raise) history semantics unspecified | Out of scope; a completeness question in the temporal model as applied to `Alarm` specifically | Phase 10 (Alarm) | Phase 10 exit gate | No | No — additive clarification to `Alarm`'s row-per-cycle-vs-reuse behavior |
| M2 — `dedup_key` partition-key notation needs correction | Documentation-only; the underlying mechanism (hash embeds `occurred_at`) already works | Phase 9 (Telemetry) | Phase 9 exit gate | No | No — notation fix only |
| M3 — Outbox commit-order / concurrent-dispatcher ordering unspecified | Out of scope; an implementation-detail refinement of an already-correct durability guarantee | Any phase using Outbox (all, from Phase 2 on) | First phase where multiple dispatcher workers are actually run | No | No — additive statement of a design requirement ("handlers tolerate out-of-order delivery") |
| M4 — No stated egress restriction on Integration endpoint configuration | Out of scope; lower-severity SSRF surface (admin-configured, not end-user input) | Phase 8 (Integrations) | Phase 8 exit gate | No | Possibly — may add a stated egress allow/deny policy |
| M5 — No alarm-rule type for "metric gone stale" | Out of scope; a rule-type completeness gap, not a structural defect | Phase 10 (Alarm) | Phase 10 exit gate | No | Possibly — may add a rule `condition_type` beyond value-threshold |
| M6 — Soft-delete protected-entity list not extended to v1.1/v1.2 tables (`PowerNode`, `FloorPlan`, `Collector`, `EquipmentInterface`) | Out of scope; documentation completeness | Phases 5/7/8 (Spatial/Power/Collector) | Each phase's own exit gate | No | No — extends an existing list, no new mechanism |
| M7 — Bonded/aggregated (LACP) network interfaces unmodeled | Already an open decision (network scope, §49) | Phase 7+ (Network, if built) | Whichever phase builds network topology | No | Possibly — deferred pending the network-scope decision itself |
| M8 — Floor-plan replacement migration mechanics underspecified | Out of scope; a workflow-detail gap | Phase 6 (Floor Plan Import) | Phase 6 exit gate | No | Possibly — may reuse the `FloorPlanImportCandidate` pipeline or need a distinct one |
| M9 — Universal TIMESTAMPTZ convention demonstrated per-table, not stated as a binding rule | Documentation-only | Phase 1 (foundation convention) | Phase 1 exit gate (as a documentation completeness item) | No | No |
| M10 — Discovery-absence effect on matched asset lifecycle not explicitly stated (design is correct, just undocumented) | Documentation-only | Phase 8 (Integrations) | Phase 8 exit gate | No | No |

None of the deferred findings block Phase 1. All are tracked, none are silently absorbed.

---

## Red-Team Finding Disposition (C1–C5, plus H1/H6/H7/M1, plus F1 found at the final validation gate)

| Finding | Original Severity | Confirmed? | Resolved? | Deferred? | Blocking Phase 1? | Reason |
|---|---|---|---|---|---|---|
| C1 | CRITICAL | Confirmed | **RESOLVED** | No | No | `ReplaceAsset` operation + `replaces_asset_id` close the gap |
| C2 | CRITICAL | Confirmed | **RESOLVED (v1.3)** | No | No | Dual partial exclusion constraints verified against the full truth table for populated `side`; the NULL-`side` residual gap (F1, found at the final validation gate) is now closed by v1.3's `CHECK` extension |
| C3 | CRITICAL | Confirmed | **RESOLVED** | No | No | Range-exclusion constraint enforces at-most-one-current at the DB level |
| C4 | CRITICAL | Confirmed | **RESOLVED** | No | No | `FOR UPDATE`/`EvalPlanQual` for placement; `version`/`If-Match` for `PowerConnection` |
| C5 | CRITICAL | Confirmed | **RESOLVED** | No | No | Quarantine/isolated-parse/SIR boundary defined; implementation-time security testing still required |
| H1 | HIGH | Confirmed | **RESOLVED** | No | No | Complete subtype matrix published (§4b) |
| H6 | HIGH | Confirmed | **RESOLVED** | No | No | Partitioning + privileged retention role defined; retention duration remains an open (non-blocking) decision |
| H7 | HIGH | Confirmed | **RESOLVED (v1.3)** | No | **No — was blocking, now closed** | Architecture owner recorded Option B (§32a) |
| M1 | MEDIUM | Confirmed | **RESOLVED** | No | No | Naming reconciled between §4 and §16/§25 |
| F1 | HIGH (new, found at final validation gate) | Confirmed | **RESOLVED (v1.3)** | No | No | `CHECK` extended to require `side IS NOT NULL` for rack-mounted placements |

All five mandatory CRITICAL findings (C1–C5) are RESOLVED, not merely discussed — each has a concrete database mechanism, domain workflow, and enforcement-matrix row (§47b of `ARCHITECTURE_REVIEW.md`) rather than a paragraph asserting the problem is handled.
