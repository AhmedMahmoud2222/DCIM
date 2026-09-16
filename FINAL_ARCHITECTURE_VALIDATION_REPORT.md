# FINAL_ARCHITECTURE_VALIDATION_REPORT.md

**Gate:** Final Architecture Validation, pre-Phase-1
**Baseline reviewed:** `ARCHITECTURE_REVIEW.md` v1.2
**Method:** independent re-derivation of every claimed fix against Postgres semantics and against the architecture's own stated rules — not a re-read of the revision reports' conclusions.
**Date:** 2026-09-16

---

## 1. Executive Summary

`ARCHITECTURE_REVIEW.md` v1.2 is close to Phase-1-ready but is not there yet, for two reasons, neither of which is fundamental:

**First, a confirmed, undecided blocker:** H7 (site-scoped RBAC timing) remains exactly what v1.2 itself says it is — `MANAGEMENT DECISION REQUIRED` (verified directly in the repository, §32a). Per this gate's own rule, that alone is sufficient to withhold `APPROVED FOR PHASE 1`.

**Second, independent re-derivation of C2's fix (the U-range exclusion constraint) found a real residual gap v1.2 did not catch:** `EquipmentPlacement.side` is nullable, and the schema has no `CHECK` requiring it to be set for rack-mounted placements. A row with `side = NULL` produces `NULL` (not `false`) for both generated `occupies_front`/`occupies_rear` columns, and a `NULL` value in a partial exclusion constraint's `WHERE` clause excludes that row from enforcement entirely — meaning an equipment item placed with an unset `side` is invisible to *both* exclusion constraints and can silently overlap another item in the same U range. This was verified directly against the current schema (line 330 vs. line 337 of `ARCHITECTURE_REVIEW.md`), not assumed. C2 is therefore **not fully resolved** — the truth-table logic for a properly-populated `side` value is sound (re-verified row-by-row below), but the schema doesn't guarantee `side` is always populated where it matters.

Beyond these two, this gate found three further real (if narrower) gaps that survived three rounds of documentation without being caught: a domain-boundary rule violated by the architecture's own new Asset Replacement operation, an undocumented but load-bearing dependency on Postgres's transaction-constant `now()`, and a scalability question connecting the still-open H7 decision to telemetry's target scale that neither H7 nor H8's write-ups mention. None of these require redesign. All five together are exactly what "targeted revision" exists for.

Everything else re-derived independently in this pass — C1, C3, C4, C5, H1, H6, M1, the domain ownership matrix, the temporal model's remaining structure, the power/network topology, the integration layering, and the deferred-findings list — held up. No fundamental architectural decision was found unresolved.

---

## 2. Repository Commit History Reviewed

| Commit | Content | Architectural Significance |
|---|---|---|
| `4a39e1b` | Phase 0 architecture (v1.0) | Initial baseline: modular monolith, PostgreSQL, versioned rack/equipment catalogs, adapter-based integrations |
| `070f3e1` | Architecture v1.1 | Introduced `ManagedAsset`, `PowerNode`, `EquipmentPlacement`/`RackPlacement`, Outbox pattern, collector architecture, network topology split |
| `3505af6` | Red-team validation report | Found C1–C5 (CRITICAL), H1–H9 (HIGH), M1–M10 (MEDIUM), several LOW/OBSERVATION items against v1.1 |
| `8ea77a8` | Architecture v1.2 targeted revision | Claims to resolve C1–C5, H1, H6, M1; escalates H7 to an explicit, undecided management decision; explicitly defers H2–H5, H8–H9, M2–M10 |

`git log --stat` confirms each commit's file-level footprint matches its stated scope — v1.2's commit touches exactly `ARCHITECTURE_REVIEW.md` plus the two new companion documents, with no unrelated file changes and no silent modification of `ARCHITECTURE_RED_TEAM_REPORT.md` or `ARCHITECTURE_REVISION_REPORT.md` (both remain as originally committed — checked, not assumed). No commit was found that silently weakened a prior decision; each revision's diff is additive/corrective in exactly the areas its own commit message claims.

---

## 3. Documents Reviewed

`ARCHITECTURE_REVIEW.md` (v1.2, canonical), `ARCHITECTURE_REVISION_REPORT.md` (v1.0→v1.1), `ARCHITECTURE_RED_TEAM_REPORT.md` (v1.1 adversarial findings), `ARCHITECTURE_CHANGE_MATRIX.md` (v1.1→v1.2 before/after), `ARCHITECTURE_TARGETED_REVISION_REPORT.md` (v1.2 summary). No ADRs exist as separate files — all ADRs (AD1–AD25) are inline in `ARCHITECTURE_REVIEW.md` §46, confirmed by direct inspection.

---

## 4. Previous Red-Team Findings — Re-Verified

| Finding | Previous Status | Current Status | Evidence | Remaining Risk |
|---|---|---|---|---|
| C1 (Asset Replacement) | CRITICAL, confirmed | **RESOLVED** | §4a: `ReplaceAsset` transaction, `replaces_asset_id` with partial-unique constraint, verified logically sound; **but see §17 below — a new domain-boundary tension was found in the same section's step 5 (alarm-clearing)** | MEDIUM — see finding F2 |
| C2 (U-range exclusion) | CRITICAL, confirmed | **RESOLVED, WITH A RESIDUAL GAP** | §7a's dual-constraint logic re-verified row-by-row against both truth-table formulations (this gate's and v1.2's) and confirmed correct *for populated `side` values*; **`side` is nullable with no enforcing `CHECK`, confirmed at line 330/337 — a NULL-side rack-mounted row bypasses both constraints** | HIGH — see finding F1 |
| C3 (Exactly-one-current-placement) | CRITICAL, confirmed | **RESOLVED** | §7b/§8: `tstzrange`-based exclusion constraint, re-verified: `tstzrange(from, NULL)` produces a correctly unbounded-upper range in Postgres, half-open `[)` bounds correctly treat back-to-back close/reopen as non-overlapping | LOW — depends on `now()` being transaction-constant, see finding F3 |
| C4 (Concurrency) | CRITICAL, confirmed | **RESOLVED** | §7c/§13a: `SELECT ... FOR UPDATE` + `EvalPlanQual` re-check under READ COMMITTED re-derived and confirmed to produce zero-rows-on-lost-race (standard, documented Postgres behavior, not exotic); `version` columns confirmed present on `EquipmentPlacement` (line 335), `RackPlacement` (line 474), `PowerConnection` (line 657) | LOW — requires PostgreSQL integration test, correctly stated as such, not claimed as tested |
| C5 (Import security boundary) | CRITICAL, confirmed | **RESOLVED (architecturally)** | §10a: quarantine → isolated-parse → SIR boundary is a genuine execution-boundary redesign, not a paragraph of intent — credential-less, network-restricted, resource-bounded worker, correctly stated as requiring security testing, not claimed as validated | MEDIUM — architecturally sound; real effectiveness is untested by definition until implemented |
| H1 (Subtype matrix) | HIGH, confirmed | **RESOLVED** | §4b: complete column lists for every subtype; `PDUOutlet`'s non-`ManagedAsset` status explicitly justified; placement-mechanism choice (temporal vs. plain FK) explicit and consistent | LOW |
| H6 (AuditLog retention) | HIGH, confirmed | **RESOLVED** | §30a/§38: partitioning added, privileged role distinct from application role, correctly separates mechanism (specified) from duration (left open, correctly not invented) | LOW |
| H7 (RBAC decision) | HIGH, confirmed | **NOT RESOLVED — CONFIRMED STILL OPEN** | §32a, verified directly: `"Decision status as of this revision: MANAGEMENT DECISION REQUIRED. No such decision exists elsewhere in this repository."` No commit since has recorded a decision. | **This gate's primary blocker, per §6 of the validation instructions** |
| M1 (Naming) | MEDIUM, confirmed | **RESOLVED** | §4's diagram now reads `object_id (when object_class='managed_asset')` matching §16/§25 exactly | None |

---

## 5. Final Architecture Integrity Assessment

The load-bearing decisions — `ManagedAsset` shared-PK identity, `PowerNode` real-FK topology, temporal placement with range-exclusion constraints, the Outbox pattern, and the Protocol→Driver→VendorProfile→DeviceProfile integration layering — were re-examined for internal consistency, not just re-read, and hold. The gaps found in this pass (F1–F5 below) are all narrow: one missing `CHECK` constraint, one domain-boundary clarification, one documentation note about a Postgres semantics dependency, one cross-table invariant statement, and one conditional (on H7) scalability note. None requires touching `ManagedAsset`, `PowerNode`, the placement model, or the Outbox pattern.

**New findings from this gate (not previously recorded anywhere):**

- **F1 (HIGH):** `EquipmentPlacement.side` is nullable with no `CHECK` enforcing it for `placement_type='rack_mounted'`. A NULL-side row's generated `occupies_front`/`occupies_rear` columns are also NULL, which a partial exclusion constraint's `WHERE` clause treats as "not in the enforced set" — silently reopening exactly the overlap risk C2 was built to close. **Fix:** add `CHECK (placement_type <> 'rack_mounted' OR side IS NOT NULL)`. Category: SCHEMA CHANGE, one line, no redesign.
- **F2 (MEDIUM):** §4a's Asset Replacement operation (step 5) has the `rack`/`equipment` module directly transition `Alarm` rows to `cleared` within the same transaction. This is a direct cross-module write, which §43 (Domain Boundaries, unchanged since v1.1) explicitly prohibits: *"no arrow points backward... where a downstream domain needs to react to an upstream change, it does so via a domain-event handler, never a direct cross-module write."* This is a genuine tension, not a typo — same-transaction consistency (an alarm must never be visibly active against a `removed` asset, even for an instant) pulls toward a synchronous write, while the stated domain-boundary rule pulls toward an async event handler in the `alarm` module. **This gate does not resolve the tension** (per its own validation-only scope) — it is recorded as a required, explicit choice: either (a) document alarm-clearing-on-retirement as a narrow, sanctioned synchronous exception (parallel to how `AuditLog` writes are already a stated synchronous exception), or (b) move it to an `alarm`-module event handler and explicitly accept a brief (sub-second, in practice) window where a removed asset's alarm is still shown active. Category: DOMAIN DESIGN CHANGE (a documentation/rule clarification, not a schema change).
- **F3 (MEDIUM):** The correctness of §7c's close-then-open placement transaction (specifically, that the closed and reopened intervals are adjacent rather than overlapping or gapped) depends on Postgres's `now()`/`CURRENT_TIMESTAMP` returning the same value for both statements within one transaction (transaction-start-constant, not per-statement). This is correct as designed and was independently re-derived and confirmed — but it is a load-bearing, non-obvious dependency that is nowhere stated as an implementation constraint. A future implementer using `clock_timestamp()` (a genuinely common substitution when someone wants "the real current time") would silently reintroduce a race. Category: DOCUMENTATION ONLY, but should be an explicit, flagged implementation constraint, not left to be discovered by accident.
- **F4 (LOW/MEDIUM):** No stated invariant that `u_range`'s width must equal the placed equipment's `EquipmentModelRevision.height_u`. This is correctly a domain-enforced (not DB-enforceable, since it spans tables) rule, but it is never explicitly written down as an invariant with its enforcement layer identified, unlike every other cross-cutting invariant in §47b's matrix. Category: DOCUMENTATION ONLY.
- **F5 (LOW):** Two related, purely conventional gaps, both needed before Phase 4/5 can implement consistently and neither affecting the C2/C3 DB mechanisms' correctness: (a) `u_range`'s inclusive/exclusive bound convention is never stated explicitly (matters for correct range construction at insert time); (b) rack U-numbering direction (U1 = bottom vs. top of the physical rack) is never stated anywhere in the document. Category: DOCUMENTATION ONLY.
- **F6 (MEDIUM, conditional on H7):** No table in the telemetry/alarm/event path (`TelemetryReading`, `Event`, `Alarm`) carries a denormalized `site_id`/`room_id`. Every subtype in `ManagedAsset` has a deterministic path to `Site` (verified: Rack/Equipment/PDU/Sensor via placement→Room→Floor→Building→Site; UPS/PowerPanel via direct `room_id`; Generator via direct `site_id` — no "site-less" object was found). But if H7 is ever resolved as **Option A** (site-scoped RBAC required before/soon after Phase 1), every RBAC-filtered query against `TelemetryReading` at its target scale (100,000+ equipment × 50 metrics × 1/min, per H8's own math) would need a multi-hop join back through `ManagedAsset`/placement/room to filter by site — a real, foreseeable performance problem for exactly the enforcement mechanism Option A would require. This is not urgent under the currently-recommended Option B, but it connects two currently-separate open items (H7 and H8) in a way neither document currently notes. Category: ARCHITECTURE CHANGE, conditional — only actionable once H7 is decided.

---

## 6. Database Integrity Matrix

| Invariant | DB-enforced? | Domain-enforced? | API-enforced? | Audit/event? | Failure behavior | Status |
|---|---|---|---|---|---|---|
| Unique asset identity | Yes (`asset_tag` UNIQUE, `id` PK) | — | — | `AuditLog` | Constraint violation on duplicate tag | OK |
| Immutable `ManagedAsset.id` | Yes (PK, never updated) | `ReplaceAsset` never mutates `id` | Dedicated replace endpoint, not `PATCH` | `AssetReplaced` event | N/A | OK |
| Subtype integrity (exactly one concrete row per `ManagedAsset`) | **No** — Postgres has no native "exactly one subtype" constraint for class-table inheritance | Centralized creation (service layer) | — | — | Undetected if violated by a bug | Unchanged from v1.1 (L3), still a low-priority gap, not newly found |
| One current placement per asset | Yes (`tstzrange` exclusion, §7b/§8) | Close-then-open workflow | `FOR UPDATE`/`409` | Placement event | Constraint violation on race that bypasses the lock | OK, pending integration test |
| Temporal placement overlap (historical) | Yes (same constraint as above — subsumes this) | — | — | — | — | OK |
| U-range overlap | **Partially** — correct for populated `side`; NULL `side` escapes both constraints (F1) | Pre-validation (but doesn't cover the NULL case either, since nothing requires the domain layer to set `side`) | `409` (only reached if the DB constraint or domain check fires) | — | **Silent** if `side` is NULL — no error, no audit, just an undetected overlap | **GAP — F1** |
| Power FK integrity | Yes (`PowerConnection` FKs to `PowerNode`) | `PowerNode` creation discipline (service layer) | `409` on concurrent edit | Topology event | Orphaned edge is impossible; orphaned *node-less* participant is a service-layer risk (unchanged from v1.1) | OK |
| Power self-loop | **No** | **No** | **No** | — | Undetected | Confirmed still H4, correctly deferred, not silently resolved |
| Power cycle | **No** | **No** | **No** | — | Recursive CTE would loop until Postgres's recursion limit | Confirmed still H4, correctly deferred |
| Network connection integrity | Yes (`PortConnection` FKs to `EquipmentInterface`) | — | — | — | — | OK, unchanged from v1.1 |
| Telemetry deduplication | Yes (`dedup_key` unique per partition, embeds `occurred_at`) | — | — | — | Conflicting insert is a no-op/upsert | OK (M2's notation nit remains, not re-litigated here) |
| Alarm state transitions | Partial unique index (no duplicate open alarm) | Hysteresis logic (trigger/clear durations) | — | `Alarm*` events | — | OK; H9 (re-trigger row semantics) remains open, correctly |
| Event identity | Yes (`dedup_key` unique) | — | — | Is itself the audit ledger for events | — | OK |
| Outbox durability | Yes (row committed in the same transaction as the mutation) | Dispatcher + idempotent handlers | — | — | At-least-once, never lost | OK |
| Site authorization | **No enforcement exists** (H7 undecided) | Reserved schema (`RoleAssignment.scope_type`) only | **No** | — | Every global-role user can currently read/write every site | **Confirmed blocking — H7** |

---

## 7. Data Lifecycle Matrix

| Object | Create | Modify | Move | Replace | Retire | Delete | History | Retention |
|---|---|---|---|---|---|---|---|---|
| ManagedAsset | Yes | `lifecycle_status`, `external_ids`, `asset_tag` (audited) | N/A (see Placement) | `ReplaceAsset` (§4a) | `lifecycle_status='removed'` | Never (soft-delete) | Full, via placement/telemetry/event/audit | Indefinite (row persists) |
| Rack/Equipment/PDU/Sensor | Yes, via `ManagedAsset` + subtype row | Subtype fields | Via `EquipmentPlacement`/`RackPlacement` | Via `ReplaceAsset` | Via `ManagedAsset.lifecycle_status` | Never | Via placement history | Indefinite |
| UPS/Generator/PowerPanel | Yes | Subtype fields, `room_id`/`site_id` (rare, audited, not temporal) | Audited FK update, not placement-tracked (§4b, deliberate) | Via `ReplaceAsset` | Via `lifecycle_status` | Never | Only via `AuditLog` for the rare relocation | Indefinite |
| RackPlacement / EquipmentPlacement | On install/move | Never in place | New row (close-then-open) | New row for new asset, per §4a | Row closed, none reopened | Never | Is the history | Indefinite |
| FloorPlan | Yes | New version | N/A | N/A | `status='superseded'` | **Undefined** (M6, still open) | Superseded plans retained | Not specified (M6) |
| PowerConnection | Yes | `version`-guarded edit | N/A (edges, not placed) | N/A | `effective_to` set | Never | Yes | Indefinite |
| TelemetryMetric | Yes (catalog) | Rare, audited | N/A | N/A | Deprecate (implied, not explicit) | Rare | N/A | N/A |
| TelemetryReading | Append-only | Never | N/A | N/A | N/A | Via `RetentionPolicy` tiers | Is the history | Configurable, mechanism specified |
| Alarm | On trigger | State transitions | N/A | Auto-cleared on asset replacement (F2) | `state='cleared'` | Never | **Ambiguous for re-trigger cycles (H9, still open)** | Not specified |
| Event | Append-only | Never | N/A | N/A | N/A | Never (partitioned, retained) | Is the history | Not specified |
| AuditLog | Append-only | Never (DB grant enforced) | N/A | N/A | N/A | Only via privileged retention role (§30a) | Is the history | Mechanism specified (§30a); duration open |
| Collector | Yes | `status`, heartbeat | N/A | Implied replace-in-place at implementation time | **Undefined** (M6) | **Undefined** (M6) | Via `CollectorHeartbeat` | Not specified |
| DiscoveredDevice | Yes (discovery) | `last_seen_at`, `raw_attributes` | N/A | N/A | Implied by absence — **must not auto-decommission the matched asset (M10, correctly a stated design intent, not yet an explicit rule)** | Not specified | Via `ReconciliationDiff` | Not specified |

---

## 8. Failure Matrix

| Component | Failure | Data impact | Recovery | Replay | Duplicate risk | Audit | RTO/RPO dependency |
|---|---|---|---|---|---|---|---|
| PostgreSQL | Unavailable | None committed is lost | Automatic on return | Celery retries | Low (idempotent tasks) | Resumes normally | **Requires management-approved RPO/RTO — not invented here** |
| Redis | Outage | None (Outbox is durable, re-verified) | Automatic | Yes, via Outbox | Low | Unaffected | None — confirmed not a system of record |
| Celery worker | Crash | None if handler idempotent | Requeue (ack-late) | Yes | Requires idempotent handlers (stated, unchanged) | Resumes | None |
| API process | Crash | None for committed data | Restart (stateless) | N/A | None | Resumes | None |
| Collector (central) | Down | Telemetry gap for assigned integrations | Platform's own alarm fires on "integration stale" | N/A | None | Yes | None |
| Collector (edge) / WAN | Outage | Bounded by local buffer size/duration | Backlog forwarded, `occurred_at` preserved | Yes | Low (`dedup_key`) | Yes | **Buffer duration is an open, management-approved value — not invented here** |
| Device/BMS | Unreachable | Telemetry gap, that device only | Backoff + retry | N/A | None | Yes | None |
| Floor-plan parser | Malformed/malicious file | None past quarantine (§10a) | Job marked failed | N/A | N/A | Diagnostics + audit | None |
| Outbox dispatcher | Crash | None (rows remain `pending`) | Resumes on restart | Yes, at-least-once | Requires idempotent handlers | Handler-level | None |
| Notification provider | Timeout | None (recorded `failed`, retried) | Retry w/ backoff | Yes | Deduped (alarm/channel/transition) | `NotificationAttempt` | None |
| Backup/restore | N/A (not yet exercised) | N/A | **Restore test required — not claimed as passed** | N/A | N/A | N/A | **RPO/RTO require management decision** |

No RTO/RPO figures are invented anywhere in this table, consistent with the instruction against fabricating evidence.

---

## 9. Security Trust-Boundary Matrix

| Boundary | Input | Threat | Control | Isolation | Credential access | Audit | Status |
|---|---|---|---|---|---|---|---|
| Browser → API | JSON, JWT | Credential theft, injection, CSRF | Argon2id, in-memory access token, `HttpOnly`/`Secure`/`SameSite=Strict` refresh cookie, parameterized queries | Process-level (stateless API) | Full (app role) | Yes | OK |
| Browser → WebSocket | Topic subscription | Cross-site data exposure | Auth handshake required | Same as API | Same as API | Yes | **Inherits H7's gap — any global-role user can subscribe to any site's topic** |
| API → Database | SQL via ORM | Injection, privilege escalation | Parameterized queries, least-privilege role (no DDL/superuser, no UPDATE/DELETE on `audit_log`) | DB role boundary | App role only | Yes | OK |
| API → Redis | Task/cache payloads | Poisoning | Private network trust | Network-level | App/worker role | N/A | OK at stated scale |
| Worker → Device | Protocol calls | SSRF via endpoint config, credential leakage | Admin-configured endpoints, encrypted credentials, redacted logs | Per-adapter | Scoped to the integration | `PollLog` | OK; M4 (egress allowlist) remains open, correctly, not newly urgent |
| Collector → Central DCIM | Outbound telemetry/backlog | Spoofing, replay | mTLS/signed callback (not yet built, correctly deferred) | Network-level | None (outbound-only) | `CollectorHeartbeat` | OK — future work, not a current gap |
| Uploaded file → Parser | SVG/DXF/PDF/VSDX/image | XXE, zip bomb, RCE, resource exhaustion | Quarantine, content-sniffed validation, size/ratio caps | **Process/worker isolation, no DB/Redis/device credentials (§10a)** | None | Import diagnostics | **Architecturally defined; requires security testing to confirm control effectiveness — not claimed as validated** |
| Parser → Sanitized IR | Normalized geometry only | N/A (trusted from this point) | Schema-validated SIR | N/A | N/A | N/A | OK |
| External integration → DCIM (BMS/EMS/ITSM) | Protocol responses, external events | Malicious/malformed response | Adapter-level parsing, normalization before DB write | Per-adapter | Scoped credentials | `PollLog`/`Event` | OK — narrower surface than file upload, correctly not held to the same isolation bar |
| AI → DCIM APIs | API calls (future) | Bypassing RBAC/audit via "internal" trust | **Same JWT/RBAC path as any client — no special AI bypass exists in the architecture** | Same as any API client | Same as any client | Same as any client | OK structurally; **inherits whatever RBAC gap exists — same H7 dependency as every other client, not a separate one** |

---

## 10. Source-of-Truth Assessment

Re-checked against §12 (Single Source of Truth) and §42 (Data Ownership Matrix), both unchanged since v1.1: every fact this gate traced (asset identity, location, placement, rack elevation, equipment position, power topology, network topology, telemetry, discovered state, alarm state, events, floor-plan geometry, capacity, external references) has exactly one owning table or is explicitly labeled derived/cached (`CapacitySnapshot`, `telemetry_current_value`, `PowerCapacity.measured_load_kw`). Rack elevation is confirmed still computed at request time (§7, unchanged), never stored. 2D/3D are confirmed to both read `SpatialObject` exclusively (§8/§41) — no independent database found for either. Discovery is confirmed structurally incapable of silently overwriting authoritative inventory (§26's `DiscoveredDevice`/`ReconciliationDiff` split, unchanged). No new violation of this principle was found **except** F2 (the alarm-clearing tension), which is a domain-boundary question, not a source-of-truth violation — `Alarm` remains the sole owner of alarm state either way; the question is only *which module's transaction* is allowed to write to it.

---

## 11. Temporal Consistency Assessment

`created_at`/`updated_at` (row bookkeeping) vs. `effective_from`/`effective_to` (business validity) vs. `occurred_at`/`recorded_at`/`received_at` (event/telemetry timing) remain distinct and consistently applied everywhere this gate checked (§29, unchanged since v1.1). The conceptual reconstruction questions posed by this gate were traced end-to-end:
- *"What equipment was installed in Rack A014 at 14:00 on 15 June?"* — answerable: `WHERE rack_id = ... AND effective_from <= T AND (effective_to IS NULL OR effective_to > T)` against `equipment_placement`.
- *"What was the power path at that time?"* — answerable via the same pattern against `PowerConnection`.
- *"What telemetry source was active?"* — answerable via `TelemetryReading.source_id`/`collector_id` at that `occurred_at`.
- *"Which asset physically occupied the position?"* — answerable, and correctly distinguishes a moved asset (same id) from a replaced one (different id via `replaces_asset_id`, §4a) — this is a genuine strength confirmed by this gate, not just asserted.
- *"What alarms were active?"* — answerable for the common case; **weakened for an asset with multiple trigger/clear cycles, per H9, still correctly open.**

**New finding in this section:** F3 (the `now()`/`clock_timestamp()` dependency) is a temporal-consistency risk that hadn't been surfaced before this gate — recorded above in §5.

---

## 12. Scalability Assessment

H8's math (100,000 equipment × 50 metrics × 1/min ≈ 83,000 readings/sec) is re-stated, not re-invented, and remains correctly un-benchmarked — no evidence in the repository claims this was load-tested, and this report does not claim otherwise. Partitioning (`TelemetryReading`, `Event`, and now `AuditLog` per H6) is architecturally sound for the query patterns described. **No new scale finding was made about ingestion itself** — H8 stands exactly as the red-team left it, correctly deferred to Phase 9. The one new scale-adjacent finding is F6 (§5 above): the *interaction* between an eventual Option-A RBAC decision and telemetry's join cost, which is new precisely because it sits at the intersection of two documents (H7 and H8) that were each reviewed correctly on their own terms but not against each other until this gate.

---

## 13. Multi-Site / RBAC Assessment

**H7 is confirmed unresolved.** Every `ManagedAsset` subtype has a deterministic path to `Site` (traced in §5/F6 above — no site-less object was found among `ManagedAsset`, `PowerNode`, `Telemetry`, `Alarm`, `Event`, `Collector`, `FloorPlan`, or network objects; `Collector.site_id` is nullable by design, correctly representing "central," not an oversight). The schema (`RoleAssignment.scope_type/scope_id`) is ready to support enforcement the moment a decision is made. **What does not exist is the decision itself, or the enforcement middleware.** Per this gate's explicit instruction: *"If management has NOT made the decision: FINAL VERDICT MUST NOT BE APPROVED."* This instruction is followed literally — the verdict below reflects it.

---

## 14. Integration / Collector Assessment

Protocol→Driver→VendorProfile→DeviceProfile→MetricMapping layering (§20) re-verified as sufficient to keep vendor-specific logic out of domain code — no OID or vendor conditional exists anywhere near `rack`/`equipment`/`power` module descriptions. Collector/edge-collector architecture (§18/§19) re-verified: `occurred_at`/`received_at` split and `dedup_key` are exactly the mechanisms needed for safe WAN-outage buffering and replay, confirmed consistent (not just asserted) with the general telemetry-identity design in §16. Credential isolation for collectors (outbound-only, no inbound port) is architecturally stated; not yet implemented, correctly not claimed as such.

---

## 15. 2D / 3D / Floor-Plan Assessment

Confirmed: `SpatialObject` remains the single geometry owner for both 2D and 3D (§8/§40/§41, unchanged). The renderer abstraction (`SpatialRenderer`/`SVGRenderer`/`CanvasRenderer`/`WebGLRenderer`) does not touch domain data. Instancing/LOD/room-scoped lazy loading (§41) directly addresses the 10,000-rack scale question without loading the whole enterprise into one scene, re-confirmed as architecturally sound (not benchmarked — correctly not claimed as such). The import security boundary (C5/§10a) was independently re-derived in detail (§5, §9) and found sound in its execution-boundary design. Floor-plan replacement/versioning mechanics (M8) and re-import position-based reconciliation (H3) remain correctly open, exactly as the change matrix states — neither was accidentally resolved nor accidentally contradicted by any later section.

---

## 16. Power / Network Assessment

`PowerNode`/`PowerConnection` FK integrity re-confirmed real (not polymorphic-and-hopeful). Self-loop and cycle prevention (H4) and the capacity rollup/double-counting formula (H5) are **confirmed still absent** from the schema and narrative — checked directly (§13/§13a contain no `CHECK (source_node_id <> target_node_id)` and no aggregation formula for `PowerCapacity`) — both correctly remain tracked as Phase 7 gates, not silently resolved by v1.2's `version` column addition (which addresses concurrency only, a different concern). Network topology's physical/logical split (`EquipmentInterface`/`PortConnection` vs. `NetworkSegment`/`InterfaceSegmentMembership`) is unchanged and sufficient for the near-term modeling target; LACP/bonding (M7) remains explicitly deferred, tied to the already-open network-scope decision.

---

## 17. Telemetry / Alarm / Event Assessment

Telemetry identity (multi-source, `dedup_key`, precedence) and the two-dimensional quality model re-confirmed sound. Alarm hysteresis (independent trigger/clear durations) re-verified to prevent the specific flapping scenario by construction. **New finding in this section:** F2 — the Asset Replacement operation's alarm-clearing step is a direct cross-module write that the architecture's own domain-boundary rule (§43) says shouldn't happen; recorded as a required clarification, not resolved here. Alarm re-trigger row semantics (H9) confirmed still open, not accidentally addressed by anything in v1.2 (the replacement operation's alarm-clearing is a *retirement* trigger, a different event than a *re-trigger* cycle, and does not touch H9's actual question).

---

## 18. AI Readiness Assessment

Re-confirmed structurally sound: nothing in the architecture gives a hypothetical AI client any access path other than the same versioned REST API/WebSocket every other client uses, meaning AI inherits RBAC, audit, and every other control automatically rather than needing a bespoke bypass-prevention mechanism. The corollary, stated plainly: AI also inherits H7's *gap* — until site-scoping is enforced, an AI client (like any client) could read/write across every site. This is not a new risk category; it is the same risk as §13, restated for a future consumer.

---

## 19. Deferred Findings — Verified Intact

Checked individually against the current `ARCHITECTURE_REVIEW.md` and `ARCHITECTURE_CHANGE_MATRIX.md` content (not assumed from memory):

| Finding | Still valid? | Correctly deferred? | Affected phase | Blocker? | Accidentally resolved or contradicted elsewhere? |
|---|---|---|---|---|---|
| H2 (asset correlation algorithm) | Yes | Yes | Phase 8 | No | No |
| H3 (import re-import reconciliation) | Yes | Yes | Phase 6 | No | No |
| H4 (power self-loop/cycle) | Yes | Yes | Phase 7 | No | No — explicitly re-confirmed absent in §16 above |
| H5 (power capacity rollup) | Yes | Yes | Phase 7/12 | No | No |
| H8 (telemetry scale benchmark) | Yes | Yes | Phase 9 | No | No — see F6 for a new *connection* to H7, not a resolution |
| H9 (alarm re-trigger semantics) | Yes | Yes | Phase 10 | No | No — F2 touches a different alarm event (retirement), not this one |
| M2 (dedup_key partition notation) | Yes | Yes | Phase 9 | No | No |
| M3 (outbox ordering) | Yes | Yes | All phases using Outbox | No | No |
| M4 (integration egress/SSRF) | Yes | Yes | Phase 8 | No | No |
| M5 (stale-telemetry alarm type) | Yes | Yes | Phase 10 | No | No |
| M6 (soft-delete list extension) | Yes | Yes | Phases 5/7/8 | No | No |
| M7 (LACP/bonding) | Yes | Yes | Network phase, if built | No | No |
| M8 (floor-plan replacement mechanics) | Yes | Yes | Phase 6 | No | No |
| M9 (universal TIMESTAMPTZ statement) | Yes | Yes | Phase 1 | No | No |
| M10 (discovery-absence lifecycle effect) | Yes | Yes | Phase 8 | No | No |

None were dropped from the change matrix; none were silently resolved; none were silently contradicted by any v1.2 addition.

---

## 20. Final Contradiction Scan

Checked explicitly, not asserted:

- **ManagedAsset identity vs. Asset Replacement:** no contradiction — `replaces_asset_id` is additive metadata, never reassigns `id`.
- **Placement (RackPlacement/EquipmentPlacement) vs. spatial source of truth (SpatialObject):** no contradiction — the room-consistency trigger (v1.1, unchanged) and the new range-exclusion constraints (v1.2) operate on the same rows without conflicting.
- **U-range semantics vs. the schema that implements them:** **contradiction found — F1.** The document asserts the truth table is enforced; the schema, as written, does not enforce it for `side = NULL`. This is the most significant finding of this gate.
- **PowerNode/PowerConnection vs. network topology:** no contradiction — cleanly separated concerns, no shared table, no naming collision.
- **Telemetry identity vs. alarm state:** no contradiction in the schema; **a domain-boundary rule contradiction found — F2** (§43's stated rule vs. §4a's described behavior).
- **Discovered state vs. floor-plan import:** no contradiction — both correctly funnel through a human-confirmation gate before touching authoritative tables, and neither references the other's tables.
- **RBAC (§32a) vs. audit (§30a) vs. retention:** no contradiction — the privileged retention role and RBAC's `RoleAssignment` are independent mechanisms with no overlapping claim.
- **Timestamps:** no contradiction in naming/semantics; **a documentation gap found — F3** (an unstated dependency, not a conflicting statement).
- **Phase sequencing (§47/§47a) vs. finding disposition (`ARCHITECTURE_CHANGE_MATRIX.md`):** no contradiction — every phase-impact claim in §47a was checked against the corresponding finding's actual resolution in §4a/§7a/§7b/§7c/§10a/§30a and found consistent.
- **Technology stack:** no contradiction — v1.2 introduced no new technology; every mechanism (generated columns, GiST exclusion constraints, `FOR UPDATE`) is native PostgreSQL, consistent with the stated "no new technology without necessity" constraint on this whole revision chain.
- **Database constraints vs. domain-layer claims of enforcement:** checked table-by-table in §6 above; the one place a domain-layer claim ("pre-validation") doesn't actually cover the DB gap is F1, already recorded.

**Two contradictions were found: F1 (schema doesn't deliver the asserted guarantee) and F2 (a new domain-boundary rule violation introduced by v1.2's own C1 fix).** Neither was present in earlier documents' own contradiction scans because both are consequences of v1.2's specific fixes, not of anything in v1.1.

---

## 21. Phase Readiness Matrix

| Phase | Inputs stable? | Architecture complete? | Deferred gates identified? | Security gate? | Performance gate? | Ready? |
|---|---|---|---|---|---|---|
| Phase 1 (Foundation) | No — blocked on H7 | Otherwise yes | N/A | H7 is itself a security/authorization gate | N/A | **No — blocked on H7** |
| Phase 2 (Rack/Equipment/Placement) | Yes, once Phase 1 clears | Yes, pending F1's one-line fix | H1 informs, doesn't block | — | — | Conditionally yes |
| Phase 5 (U-position) | Yes | Yes, pending F1 | — | — | Requires PostgreSQL integration test (stated, not done) | Conditionally yes |
| Phase 6 (Floor-plan import) | Yes | Yes | H3, M8 correctly deferred | C5's boundary requires security testing at implementation | — | Conditionally yes |
| Phase 7 (Power/Network) | Yes | Yes | H4, H5 correctly deferred | — | — | Yes |
| Phase 8 (Integration/Discovery/Collector) | Yes | Yes | H2, M4, M10 correctly deferred | M4 (egress) worth resolving before this phase's exit gate | — | Yes |
| Phase 9 (Telemetry scale) | Yes | Yes | H8, M2 correctly deferred | — | **Benchmark required, not done** | Conditionally yes — benchmark is this phase's own gate, not a blocker to starting it |
| Phase 10 (Alarm) | Yes | Yes | H9, M5 correctly deferred | — | — | Yes |
| Phase 11 (3D) | Yes | Yes | — | — | — | Yes |
| Phase 12 (Capacity) | Yes | Yes | H5 (rollup formula) must resolve here | — | — | Conditionally yes |
| Phase 14 (AI) | Yes | Yes | — | Inherits whatever RBAC state exists by then | — | Yes |

---

## 22. Remaining Risks

Carried forward, unchanged: the seven risks in `ARCHITECTURE_REVIEW.md`'s risk list (v1.1) plus the two added in v1.2 (PostgreSQL integration testing required for the corrected constraints; security testing required for the import boundary). New in this gate: F1–F6 above, each already carrying its own severity and required action.

---

## 23. Required Actions Before Phase 1

1. **Record the H7 decision** (Option A/B/C, §32a) — the architecture owner's action, not an engineering task. **Blocking.**
2. **Add `CHECK (placement_type <> 'rack_mounted' OR side IS NOT NULL)` to `EquipmentPlacement`** (F1) — a one-line schema correction closing the residual C2 gap. **Blocking for Phase 2/5's exit gate, trivial to apply.**
3. **Resolve the F2 domain-boundary tension explicitly** — choose and document either the sanctioned-synchronous-exception framing or the event-driven-with-accepted-window framing for alarm-clearing on asset retirement. Not blocking for Phase 1 itself (no alarms exist yet), but must be resolved before Phase 10 (Alarm) or Phase 2's replacement workflow ships, whichever comes first.
4. **State the F3 `now()`/`clock_timestamp()` constraint explicitly** in the architecture as an implementation note. Documentation-only; cheap; should not wait for a dedicated revision pass.
5. **State the F4/F5 conventions** (U-range/height invariant, bound convention, U-numbering direction) explicitly. Documentation-only; needed before Phase 4/5, not Phase 1.

None of items 2–5 require a further red-team-style revision cycle — they are small enough to fold into the next routine documentation pass. Item 1 is the only true gate.

---

## 24. Final Verdict

**ARCHITECTURE REQUIRES TARGETED REVISION**

Not approved, because: (a) H7 remains an explicit, undecided, confirmed-open management decision, and this gate's own rule states the verdict must not be `APPROVED FOR PHASE 1` while that is true; (b) independent re-derivation of C2's fix found a genuine, if narrow, residual gap (F1) that the prior revision did not catch, meaning C2 cannot be marked fully resolved either.

Not "requires revision before Phase 1," because: every finding in this report — F1 through F6, and H7 itself — is narrow, precisely located, and correctable without touching `ManagedAsset`, `PowerNode`, `EquipmentPlacement`/`RackPlacement`, the integration layering, or the Outbox pattern. None forces an engineering team to invent architecture; each has an exact, stated fix or an exact, stated decision-maker.

Required before Phase 1 specifically: item 1 (H7) and item 2 (F1) from §23. Items 3–5 are real but do not block Phase 1's own start.
