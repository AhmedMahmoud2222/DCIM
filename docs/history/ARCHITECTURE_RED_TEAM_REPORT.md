# DCIM Architecture v1.1 — Red-Team Validation Report

**Reviewed:** `ARCHITECTURE_REVIEW.md` v1.1, `ARCHITECTURE_REVISION_REPORT.md`
**Method:** line-by-line verification against the file as written, not against the revision report's claims. Every finding below cites the section it was found in.
**Date:** 2026-09-16

---

## 1. Executive Summary

v1.1's structural decisions hold up: `ManagedAsset` as shared-PK identity, `PowerNode` as a real FK target, `EquipmentPlacement`/`RackPlacement` as unified temporal placement records, the Protocol→Driver→VendorProfile→DeviceProfile integration layering, and the Outbox pattern are all sound architecture and none needed to be undone by this pass.

What breaks under adversarial reading is not the shape of the architecture but **five specific mechanisms the document asserts as guarantees that, as literally specified, do not deliver those guarantees**: an exclusion constraint whose predicate is inverted, a "current placement" invariant with no database constraint actually enforcing uniqueness, concurrency control that was scoped to the wrong tables, an asset-identity model with no defined behavior for physical replacement, and a floor-plan import pipeline that accepts untrusted files with no stated parsing safety boundary. Each is narrow and fixable without touching the overall design. None require abandoning `ManagedAsset`, `PowerNode`, or the placement model.

Beyond those five, the review found a genuine internal inconsistency between §4 and §16/§25 (a column name used in one section that doesn't exist in the section that actually defines the table), several v1.1-introduced tables that were never fully re-specified with columns (making the document not self-contained for Phase 2 implementation), and a set of real-but-narrower operational gaps (audit-log partitioning, power-capacity rollup formula, asset-correlation algorithm, telemetry ingestion rate at target scale).

---

## 2. Verdict

**ARCHITECTURE REQUIRES TARGETED REVISION**

Five CRITICAL findings block unconditional approval. All five are corrections within already-defined sections (a constraint predicate, a missing index, a concurrency-control scope, a missing workflow definition, a missing security control statement) — none requires discarding or fundamentally re-shaping `ManagedAsset`, `PowerNode`, `EquipmentPlacement`, the Outbox pattern, or any ADR in §46. See §15 for the exact, minimal set of changes required before Phase 1 begins.

---

## 3. Critical Findings

**C1 — Asset replacement / physical-swap workflow is undefined.**
*Where:* §4 (Digital Object Identity), interacts with §7, §8, §46 AD7.
*Defect:* `ManagedAsset.id` is asserted as "the same value in every table that references it" for the physical asset's entire life. But the document never defines what happens when a physical rack is removed and a *different* physical rack is installed in the same slot — does this create a new `ManagedAsset` (correct, preserves history) or does an implementer reuse/mutate the existing row (which would silently make years of prior telemetry/alarm/audit history describe two different physical objects under one ID)? Nothing in the schema or narrative forecloses the wrong choice.
*Why CRITICAL:* This is exactly "loss of authoritative state" / "inability to reconstruct history" — and because `ManagedAsset` is the join target for every cross-cutting domain (§4's own diagram), getting this wrong is discovered only after real replacement events have already occurred, at which point it is a data-migration problem, not a code fix.
*Fix required:* Define an explicit "Asset Replacement" operation: decommission the old `ManagedAsset` (`lifecycle_status='removed'`), create a new one, add nullable `replaces_asset_id`/`replaced_by_asset_id` self-referencing columns on `ManagedAsset` for continuity, and transfer `RackPlacement`/`EquipmentPlacement` to the new asset via the normal move operation. **Category: DOMAIN DESIGN CHANGE + SCHEMA CHANGE.**

**C2 — U-range exclusion constraint predicate is logically inverted.**
*Where:* §7 (Equipment Placement Model): `EXCLUDE USING gist (rack_id WITH =, u_range WITH &&, side WITH <>) WHERE (...)`.
*Defect:* A GiST exclusion constraint rejects a new row only when *all* listed operators evaluate true against an existing row. `side WITH <>` means the constraint rejects **only when the two rows' sides differ** — i.e., it blocks the legitimate front+rear half-depth coexistence the section explicitly says it supports, and it does **not** block two rows with the *same* side (or `'both'`) from overlapping, which is the actual case that must be prevented.
*Why CRITICAL:* This is the specific mechanism §7 cites as making rack-slot overlap "impossible to violate even under concurrent writes" (echoing v1.0's identical claim). As written, two full-depth equipment items can occupy the same U range in the same rack and the database will not reject it — a physically impossible state represented as valid data, silently.
*Fix required:* Replace with a predicate that actually encodes the intended semantics — e.g., two exclusion constraints (one keyed on rows where `side IN ('front','both')`, one on `side IN ('rear','both')`, each using `u_range WITH &&` and `rack_id WITH =` only), or a computed "occupancy bitmask" column compared with a custom operator. **Category: SCHEMA CHANGE (implementation-detail in nature, but blocking because the document currently asserts a guarantee that does not hold).**

**C3 — No database constraint enforces "exactly one current placement per asset."**
*Where:* §7, §8: `RackPlacement`/`EquipmentPlacement` are documented as having "exactly one row per rack/equipment has effective_to IS NULL," backed only by an index comment (`IDX(rack_id, effective_to)`), not a uniqueness constraint.
*Defect:* An `IDX` is not a `UNIQUE` constraint. Nothing in the schema as written prevents two `RackPlacement` rows for the same `rack_id` both having `effective_to IS NULL` simultaneously (e.g., a retried "move" request, a bug in the close-then-open sequence, or a bulk-import race). If that happens, "where is this rack" has two different authoritative answers at once.
*Why CRITICAL:* This directly undermines §12 (Single Source of Truth), the principle the document states most prominently and repeats as the resolution to v1.0's original defect. The fix the document credits itself for making "structurally impossible" (§8, AD9) is not actually enforced by anything in the given schema.
*Fix required:* Add `UNIQUE (rack_id) WHERE effective_to IS NULL` and `UNIQUE (equipment_id) WHERE effective_to IS NULL` as partial unique indexes on `RackPlacement`/`EquipmentPlacement`, respectively. **Category: SCHEMA CHANGE.**

**C4 — Optimistic concurrency is scoped to the wrong tables.**
*Where:* §36 (API Architecture): "`version INT`... on frequently-contended entities (`Rack`, `Equipment`, `AlarmRule`)."
*Defect:* Per §7/§8, `Rack` and `Equipment` no longer hold placement/location data at all — that moved to `RackPlacement`/`EquipmentPlacement`. Per §13, power topology lives in `PowerConnection`. These are exactly the tables two concurrent operators actually race on (Test 28's scenario: Engineer A moves equipment, Engineer B changes power) — and none of them has a stated concurrency-control mechanism. A "move" is a read-then-close-then-open sequence against a temporal table; without `version`/`If-Match` or an explicit `SELECT ... FOR UPDATE` on these specific tables, two concurrent moves of the same asset can race into an inconsistent result (compounding C3 — if the unique-current-placement index from C3 is added, a race here would surface as a constraint-violation error rather than silent corruption, which is better but still an unhandled 500 rather than a clean 409).
*Why CRITICAL:* The specific concurrency scenario the red-team prompt asks to verify (Test 28) is not actually protected by the mechanism the document names for that purpose.
*Fix required:* Extend `version`/`If-Match` (or a stated `SELECT ... FOR UPDATE` discipline layered on top of C3's unique index) explicitly to `RackPlacement`, `EquipmentPlacement`, and `PowerConnection`. **Category: SCHEMA CHANGE + IMPLEMENTATION DETAIL (the mechanism exists in the document; its scope is wrong).**

**C5 — Floor-plan import has no stated file-parsing safety boundary.**
*Where:* §10 (Floor Plan Importer Abstraction), §36, §31.
*Defect:* SVG, DXF, and VSDX are XML/ZIP-based formats with well-known parser attack surfaces (XXE, zip/decompression bombs, path traversal via embedded references, malformed-input crashes); PDF parsers have their own history of memory-exhaustion and RCE-class bugs. §10 describes format detection, geometry extraction, and confidence scoring in detail but never mentions parser isolation, resource limits, external-entity handling, or file-size/decompression-ratio caps anywhere the document discusses security (§31 covers auth/transport/secrets but not file ingestion).
*Why CRITICAL:* This is a genuine, unaddressed trust boundary — user-uploaded files reach backend parsing code that (per §35) runs in the same worker pool that also holds database credentials and, per Collector integration, potentially network reach to devices. An unsandboxed XML parser processing an attacker-crafted SVG/VSDX is a real security-boundary failure, exactly the CRITICAL category the review criteria name explicitly.
*Fix required:* State the parsing safety boundary explicitly — disable external entity resolution in every XML-based parser, enforce file-size and decompression-ratio limits before parsing, run each import's parse step in a resource-constrained (memory/CPU/time-limited) worker or subprocess, and reject rather than best-effort-parse malformed input. **Category: ARCHITECTURE CHANGE (a stated control is currently entirely absent, not merely under-specified).**

---

## 4. High Findings

**H1 — v1.1 never re-publishes complete column lists for the concrete `ManagedAsset` subtype tables.** (§4, §7, §8, §13) `Rack`, `Equipment`, `PDU`, `UPS`, `Generator`, `PowerPanel`, `Sensor` are discussed narratively (what changed: room_id removed from Rack, rack_id/u_position removed from Equipment) but no section in v1.1 lists e.g. `Rack`'s full column set under the new shared-PK-with-`ManagedAsset` scheme. An implementer must mentally merge v1.0's now-partially-superseded §6.3/§6.4/§6.5 with v1.1's changes to get a buildable schema — exactly the ambiguity §65's Phase-1 gate asks to rule out. **DOCUMENTATION ONLY**, but blocking for a clean Phase 2 start.

**H2 — Asset-correlation algorithm across discovery sources is unspecified.** (§26, Test 26) `DiscoveredDevice.matched_asset_id` exists; the matching logic (serial-number exact match? MAC/IP heuristic? confidence-scored like floor-plan import? manual-only?) is never defined. Without it, "two integrations identify the same physical asset" has no stated resolution path. **DOMAIN DESIGN CHANGE.**

**H3 — Import repeatability for non-identical re-imports relies on asset-tag text-label matching, which many floor plans won't have.** (§10, §21, Test 9) Checksum-based dedup only catches byte-identical re-uploads. A re-exported (visually identical, differently-encoded) file re-triggers full candidate detection; suppressing re-proposal of already-confirmed racks depends on matching a visible asset-tag label in the drawing — common floor plans label racks only by position, not by DB asset tag. No position-based reconciliation against existing confirmed `SpatialObject`s is described. Mitigated by the human-review gate (nothing auto-creates), so not CRITICAL, but a real duplication risk under normal, expected use. **DOMAIN DESIGN CHANGE.**

**H4 — Power graph has no self-loop/cycle guard, and delete/cascade behavior for `PowerNode`/`PDU`/`PDUOutlet` is undefined.** (§13, Test 12) No `CHECK (source_node_id <> target_node_id)`; no stated cycle-detection guard on the recursive CTE that traces power paths (§13) — a corrupt graph with a cycle causes an unbounded/aborted recursive query, not a clean error. Deleting or decommissioning a `PDU`/`PowerNode` has no stated effect on its `PowerConnection` rows (orphan vs. cascade-close vs. reject). **SCHEMA CHANGE + DOMAIN DESIGN CHANGE.**

**H5 — Power capacity has no rollup/aggregation formula, and A/B redundant load double-counting is unaddressed.** (§14, Test 13) `PowerCapacity` exists per-node with rated/configured/measured/threshold fields, but nothing defines how a parent node's `measured_load_kw` (e.g., a Panel) is computed from its children (sum of circuits? sum of PDU outlets?), nor how a dual-corded server's load — visible on both its A-feed and B-feed `PowerNode`s — is prevented from being counted twice when rolling up to a shared upstream node. Two implementers could compute different, both-wrong capacity numbers from the same topology. **DOMAIN DESIGN CHANGE.**

**H6 — AuditLog is excluded from the partitioning strategy, and its append-only DB-grant conflicts with any future retention/archival need.** (§30, §38, §49, Test 47) §38 names only `TelemetryReading` and `Event` as partitioned by month; `AuditLog`, which by design captures "every important mutation" across every domain at 10,000+ rack / 100,000+ equipment scale, has an identical unbounded-growth profile and is not included. Separately, §30's correct protection (the app DB role has no `UPDATE`/`DELETE` on `audit_log`) means a future approved retention policy cannot be executed through the normal application path at all — this needs a stated separate privileged maintenance path, which the document doesn't mention. **SCHEMA CHANGE + DOCUMENTATION ONLY.**

**H7 — RBAC site-scoping is fully unenforced at Phase 1, and this is a live security question, not only a future one.** (§32, Test 30) The document is honest that enforcement is deferred to "a stated trigger condition" — this is not a hidden defect, but the review must state plainly: as designed, **any** authenticated user holding a global role can read and write every site's data, with no IDOR-style protection at all, on day one. If the actual Phase 1 stakeholder set includes even one engineer who should not see another site's racks/floor plans/alarms, this is blocking, not deferrable. **MANAGEMENT DECISION** (confirm whether Phase 1 truly has zero isolation requirement) that could become a **DOMAIN DESIGN CHANGE** depending on the answer.

**H8 — Telemetry ingestion rate at the stated target scale is asserted as viable with no batching mechanism specified.** (§21 Scalability, Test 17) 100,000 equipment × 50 metrics × 1 reading/minute ≈ 83,000 readings/second sustained. Partitioning and named Celery queues are described, but nothing states a batched/bulk-insert ingestion path (vs. one Celery task or one `INSERT` per reading, which would not approach this throughput). Per the instruction not to invent evidence: **benchmark required**; what the architecture is missing is not proof it works, but a stated mechanism (batched collector submission, bulk `COPY`-style writes) that could plausibly reach this rate at all. **DOMAIN DESIGN CHANGE + TESTING ONLY (benchmark).**

**H9 — Concurrent alarm-lifecycle history (re-trigger after clear) is not explicitly modeled.** (§23, Test 27) Whether each trigger→clear cycle produces a new `Alarm` row (clean history) or reuses one row (losing the prior cycle's exact acknowledgement/clear timestamps) is not stated, which weakens "what was the alarm state at time T" reconstruction for an asset with a noisy, repeatedly-clearing metric. **DOMAIN DESIGN CHANGE.**

---

## 5. Medium Findings

**M1 — §4 and §16/§25 name the same concept inconsistently.** §4's diagram states `Alarm.managed_asset_id, Event.managed_asset_id → FK ManagedAsset.id`; §16 and §25 define the actual columns as `object_class`/`object_id` with trigger-based validation, not a column literally named `managed_asset_id`. A reader following §4 alone would expect a real FK column that §16/§25 shows does not exist under that name. **DOCUMENTATION ONLY** — reconcile to one naming convention.

**M2 — `dedup_key` uniqueness on a monthly-partitioned `TelemetryReading` needs its constraint notation corrected.** (§16, §38) Postgres requires a unique constraint on a partitioned table to include the partition key. Since `dedup_key` is a hash that already embeds `occurred_at` (the partition key), the *intended* behavior is achievable, but the ERD's literal `UNIQUE (dedup_key) per partition` phrasing should read `UNIQUE (occurred_at, dedup_key)` to reflect what Postgres will actually accept. **SCHEMA CHANGE (notation only).**

**M3 — Outbox dispatch order ("commit order") and cross-handler ordering guarantees are asserted without a stated mechanism.** (§22, Test 20/21) Transaction commit order is not the same as row-insertion-id order under concurrency (a lower-id row can become visible after a higher-id one). No dispatch-delay window or LSN-based read strategy is named to avoid reading past not-yet-visible rows. Separately, if multiple dispatcher workers process events concurrently, handler execution order isn't guaranteed even if read order is — this should be stated as a design requirement ("handlers must tolerate out-of-order delivery"), not left implicit in the general idempotency principle. **IMPLEMENTATION DETAIL**, worth naming explicitly.

**M4 — No stated egress/network restriction on `Integration` endpoint configuration.** (§20, §31, Test 31) `IntegrationEndpoint.host/port` is admin/DCIM-Manager configured, not end-user input, so this is a lower-severity SSRF surface than a public form — but nothing blocks an Integration from being pointed at internal-only ranges (loopback, link-local, cloud metadata IPs) if a lower-trust role ever holds `integration:configure`. **ARCHITECTURE CHANGE** (add a stated egress allow/deny policy).

**M5 — No alarm-rule type for "metric has gone stale," only value-threshold rules.** (§17, §23, Test 16) `AlarmRule` conditions operate on `value`; there's no described condition type for "alert when quality has been STALE for duration X," so a device that stops reporting (rather than reporting a bad value) can't be alerted on through the described mechanism alone. **DOMAIN DESIGN CHANGE.**

**M6 — v1.1's soft-delete list was not extended to new v1.1 tables.** (§46 AD9/AD10, v1.0 §6.11 preserved) The protected-from-hard-delete list (`Rack, Equipment, PDU, Site, Building, Floor, Room`) predates `PowerNode`, `EquipmentInterface`, `FloorPlan`, `Collector` — several of which are exactly as history-critical (a deleted `PowerNode` orphans `PowerConnection` history; per H4). **DOCUMENTATION ONLY** (extend the existing list; no new mechanism needed).

**M7 — Bonded/aggregated network interfaces (LACP) have no modeling path.** (§15, Test 14) `EquipmentInterface` models individual physical interfaces; there's no self-referential "member of an aggregated logical interface" concept. Reasonable to leave for the already-flagged network-scope open decision (§49), not a blocking gap today. **DOMAIN DESIGN CHANGE, deferrable.**

**M8 — Floor-plan replacement's "migrate SpatialObjects to the new plan's coordinates" step is asserted but not mechanically defined.** (§8, Test 7) Described as "an explicit migration step, reviewed like an import" without stating whether it reuses the `FloorPlanImportCandidate` pipeline or is a separate, undefined workflow. **DOMAIN DESIGN CHANGE.**

**M9 — Universal TIMESTAMPTZ convention is demonstrated per-table, not stated as a platform-wide rule.** (§9, §16, §25, §29, Test 46) Every table shown explicitly uses `TIMESTAMPTZ`, which is correct, but nothing states this as a binding convention for every future timestamp column, nor explicitly ties `Site.timezone` (preserved from v1.0) to a stated display-layer conversion rule. **DOCUMENTATION ONLY.**

**M10 — Discovery-absence ("device disappears from SNMP") has no stated effect on the matched asset's lifecycle, which is safe but should be an explicit decision, not an inferred one.** (§26, Test 25) The document's design correctly implies no auto-decommissioning happens, but doesn't say so directly. **DOCUMENTATION ONLY.**

---

## 6. Low Findings

**L1.** `ManagedAsset.id` immutability is implied (as a PK, never reassigned) but never stated as an explicit invariant alongside the other identity guarantees in §4. *DOCUMENTATION ONLY.*
**L2.** Barcode/QR workflows (inherited from the original master prompt's §44) should encode the immutable `ManagedAsset.id`, not the mutable `asset_tag` — §4 allows `asset_tag` to change (audited) but doesn't flag the operational consequence for already-printed labels. *DOCUMENTATION ONLY.*
**L3.** No orphan-prevention or duplicate-subtype-row trigger for the `ManagedAsset` ↔ concrete-subtype relationship (Postgres has no native "exactly one subtype row" constraint for class-table inheritance). *SCHEMA CHANGE, low priority given service-layer creation is centralized.*
**L4.** `NetworkSegment`/VLAN modeling scope for v1 is already an open decision (§49); no new finding, just confirming the deferral is consistent. *NONE — carried forward correctly.*

---

## 7. Observations

**O1.** Full-text/fuzzy asset search at 100,000+ equipment may eventually want `pg_trgm` trigram indexing in addition to exact/prefix lookups on `asset_tag`/`ip_address`/`mac_address` — not a defect, a forward-looking note for §48's search architecture.
**O2.** The Data Ownership Matrix (§42) and Domain Boundaries diagram (§43) are unusually clean for a document this size — no circular dependency was found under adversarial tracing (Test 42 passes cleanly).
**O3.** The two-dimensional telemetry quality model (§17) is one of the strongest mechanisms in the document — it survives every stale/multi-source/late-arrival scenario tested (Tests 15, 16) without a workaround needed.
**O4.** The Outbox pattern's core guarantee (durability of the record independent of Redis, §22/§44) correctly answers Test 35's "is Redis treated as a system of record" — it is not, and this is verified, not just asserted.

---

## 8. Scenario Test Results (All 50)

| # | Test | Verdict | Key Reason |
|---|---|---|---|
| 1 | Master Identity Integrity | **FAIL** | Asset replacement undefined (C1); correlation algorithm undefined (H2) |
| 2 | Asset Lifecycle | **FAIL** | Same root cause as #1 — "Rack A removed / Rack B installed" is the replacement scenario |
| 3 | Rack Model Versioning | PASS | Revisioning/pinning preserved from v1.0, no defect found |
| 4 | Rack U-Position Integrity | **FAIL** | Exclusion constraint predicate inverted (C2) |
| 5 | Equipment Placement | **FAIL** | No uniqueness constraint on "current" placement (C3) |
| 6 | Rack Spatial Consistency | PASS WITH CONDITION | Room-consistency trigger sound; depends on C3's fix for "current" to be well-defined |
| 7 | Floor Plan Versioning | PASS WITH CONDITION | Superseded/archived lifecycle sound; migration mechanics underspecified (M8) |
| 8 | Import Red-Team (format edge cases) | PASS | Confidence-scored human review is a reasonable generic catch-all at architecture level |
| 9 | Import Repeatability | **FAIL** | Position-based reconciliation missing for non-identical re-imports (H3) |
| 10 | Human Review | PASS | Confidence tiers, audited decisions, no-silent-authoritative-creation all verified |
| 11 | Power Graph | PASS WITH CONDITION | FKs sound; rollup formula undefined (H5) |
| 12 | Power Graph Corruption | **FAIL** | No self-loop/cycle guard, no delete-cascade definition (H4) |
| 13 | Power Capacity | PASS WITH CONDITION | Fields present; aggregation/double-counting formula undefined (H5) |
| 14 | Network Topology | PASS WITH CONDITION | Physical/logical split sound; bonded interfaces unmodeled (M7, deferrable) |
| 15 | Telemetry Identity | PASS WITH CONDITION | Multi-source/precedence sound; partition-key notation needs correction (M2) |
| 16 | Telemetry Failure | PASS WITH CONDITION | Staleness/recovery sound; no stale-triggered alarm type (M5) |
| 17 | Telemetry Scale | **FAIL** | ~83k writes/sec implied at target scale, no batching mechanism specified (H8); benchmark required |
| 18 | Edge Collector / WAN Outage | PASS WITH CONDITION | Buffer/backlog/dedup sound; max buffer duration is an open decision (§49), not a defect |
| 19 | Integration Failure | PASS | Per-device circuit breaker, isolation preserved from v1.0 |
| 20 | Domain Events / Outbox | PASS WITH CONDITION | Durability guarantee verified; ordering mechanism underspecified (M3) |
| 21 | Event Ordering | PASS WITH CONDITION | Same as #20 |
| 22 | Alarm State Machine (hysteresis) | PASS | Independent trigger/clear durations verified to prevent the specific flapping scenario |
| 23 | Alarm/Telemetry Race Conditions | PASS WITH CONDITION | Partial unique index prevents duplicate active alarms; out-of-order clear/trigger sequencing not fully addressed |
| 24 | Notification Failure | PASS | Dedup + retry policy sound |
| 25 | Discovery vs. Authoritative Inventory | PASS WITH CONDITION | Reconciliation layer prevents blind overwrite; discovery-absence behavior should be stated explicitly (M10) |
| 26 | Asset Correlation | **FAIL** | Matching algorithm (deterministic/probabilistic/manual) undefined (H2) |
| 27 | Temporal Reconstruction | PASS WITH CONDITION | Most domains reconstructable; alarm re-trigger history ambiguous (H9) |
| 28 | Concurrent Operators | **FAIL** | Optimistic concurrency scoped to wrong tables (C4) |
| 29 | Delete / Retirement Safety | PASS WITH CONDITION | Core principle sound; protected-entity list not extended to v1.1 tables (M6) |
| 30 | RBAC / Site Isolation | **FAIL** | Enforcement explicitly deferred; blocking if Phase 1 needs isolation (H7) |
| 31 | API Security | PASS WITH CONDITION | Strong baseline; integration egress restriction unstated (M4) |
| 32 | Floor Plan Security | **FAIL** | No parser sandboxing / resource-limit / XXE defense stated (C5) |
| 33 | Async Queue Isolation | PASS | Named queues with independent pools directly addresses the scenario |
| 34 | Database Failure | PASS | §44 failure table directly covers this |
| 35 | Redis Failure | PASS | Outbox durability verified — Redis correctly not a system of record |
| 36 | Observability | PASS | Correlation chain fully specified end-to-end |
| 37 | Backup / Restore | PASS WITH CONDITION | Mechanisms described; RPO/RTO and restore testing correctly stated as not yet done, not claimed |
| 38 | Scale / Hotspot Analysis | **FAIL** | AuditLog excluded from partitioning strategy at unbounded-growth scale (H6) |
| 39 | 3D Scale | PASS | Instancing/LOD/lazy-loading directly addresses 10k-rack scale |
| 40 | AI Readiness | PASS | Answerable through APIs without UI scraping; not necessarily in one call, which is not the stated requirement |
| 41 | Thermal / CFD Readiness | PASS | All required inputs present via ThermalProfile/ThermalZone without touching core inventory |
| 42 | Domain Boundary Test | PASS | No circular dependency found under adversarial tracing |
| 43 | Source of Truth Test | PASS | Data Ownership Matrix resolves ownership for every field tested |
| 44 | External System Boundary | PASS | Discovery/reconciliation layer keeps DCIM authoritative independent of BMS/EMS availability |
| 45 | Migration / Evolution | PASS | Adapter/profile layering makes new vendors/protocols/formats additive |
| 46 | Multi-Site Operations | PASS WITH CONDITION | TIMESTAMPTZ used where shown; not stated as a universal, binding convention (M9) |
| 47 | Data Retention | PASS WITH CONDITION | Telemetry retention config-driven; audit retention vs. append-only-grant conflict unresolved (H6) |
| 48 | Search Architecture | PASS | Identity consolidation makes cross-domain search plausible without a second search store |
| 49 | Reporting | PASS | Example report answerable via CapacitySnapshot/Alarm/RackPlacement without raw-telemetry joins |
| 50 | Failure of a Single Component | PASS WITH CONDITION | Mostly covered by soft-delete principle; several v1.1 tables lack explicit delete-cascade definition (H4, M6) |

**9 FAIL, 17 PASS WITH CONDITION, 24 PASS, 0 NOT APPLICABLE.**

---

## 9. Database Integrity Matrix

| Relationship | DB FK | DB Constraint | Trigger | Domain Rule | Validation Only | Gap |
|---|---|---|---|---|---|---|
| ManagedAsset → domain entity (Rack/Equipment/...) | Shared PK (yes) | — | — | — | — | No constraint enforces exactly one subtype row exists (L3) |
| Equipment → EquipmentPlacement | Yes (equipment_id FK) | Exclusion constraint (incorrect predicate, C2) | Room-consistency trigger (yes) | Close-then-open on move | — | No uniqueness on "current" row (C3) |
| Rack → RackPlacement | Yes (rack_id FK) | — | Room-consistency trigger (yes) | Close-then-open on move | — | No uniqueness on "current" row (C3) |
| SpatialPlacement → Room/FloorPlan | — | — | Yes (room-match trigger) | — | — | None found |
| PowerConnection → PowerNode | Yes (both ends) | — | — | — | — | No self-loop CHECK, no cycle guard (H4) |
| PowerNode → underlying asset | FK (managed_asset_id or owning_asset_id) | CHECK (at least one set) | — | Creation discipline (service layer) | — | No DB-level guarantee every asset-with-power gets a node (relies on service code, §46 risk 1) |
| Network port relationships | Yes (EquipmentInterface FKs) | — | — | — | — | Bonded/aggregated interfaces unmodeled (M7) |
| Telemetry → metric | Yes (metric_id FK) | — | — | — | — | None found |
| Telemetry → asset | — | Partial: `UNIQUE(dedup_key)` (needs partition-key fix, M2) | Yes (object_class validation) | — | Yes, by design (§4 caveat) | Documented, accepted exception |
| Alarm → asset | — | Partial unique index (active-alarm dedup, verified sound) | Implied, not explicitly restated for Alarm (M1) | — | Yes, by design | Same pattern as telemetry, naming inconsistency (M1) |
| Event → asset | — | UNIQUE(dedup_key) | Yes (object_class validation) | — | Yes, by design | Documented, accepted exception |
| Outbox → aggregate | — | UNIQUE(event_id) | — | Dispatcher processes in "commit order" | — | Ordering mechanism unstated (M3) |
| Audit → entity | FK (entity_id, where entity is a ManagedAsset) | — | Append-only via DB grant (REVOKE UPDATE/DELETE) | — | — | Not partitioned; conflicts with future retention need (H6) |

**Reading this matrix:** the architecture relies on application/domain code correctness in exactly three places it shouldn't need to — `ManagedAsset` subtype uniqueness (L3, low risk, centralized creation), `PowerNode` creation discipline (already self-flagged as a risk in §46), and the "current placement" invariant (C3, should be a DB constraint and currently is not).

---

## 10. Data Lifecycle Matrix

| Entity | Created | Updated | Retired | Deleted | Historical | Archived |
|---|---|---|---|---|---|---|
| ManagedAsset | Yes | `lifecycle_status`, `external_ids` | `lifecycle_status='decommissioned'/'removed'` | Never (soft-delete principle, inherited from v1.0) | Via placement/telemetry/event history | Not explicit — row persists indefinitely |
| RackPlacement / EquipmentPlacement | Yes (on install/move) | Never in place — new row per move | Implicit (`effective_to` set on move) | Never | Yes, by design (this table *is* the history) | Not explicit |
| PowerNode / PowerConnection | Yes | `effective_to` on PowerConnection | Undefined for PowerNode itself | **Undefined** (H4) | Partial (PowerConnection yes, PowerNode lifecycle no) | Not explicit |
| FloorPlan | Yes | New version | `status=superseded` | Undefined (implied never, but not stated — M6) | Yes (superseded plans retained) | Not explicit |
| TelemetryReading | Yes (append-only) | Never | N/A | Via RetentionPolicy (raw/hourly/daily tiers) | Yes, is the history | Aggregates serve this role |
| Alarm | Yes (on trigger) | State transitions | `state=cleared` | Never (implied) | **Ambiguous for re-trigger cycles** (H9) | Not explicit |
| Event / AuditLog | Yes (append-only) | Never (DB-grant enforced for Audit) | N/A | **Undefined path for Audit given append-only grant** (H6) | Yes, is the history | Not explicit, and mechanically blocked for Audit without a separate privileged path |
| Collector | Yes | `status`, heartbeat | Undefined | Undefined | Via CollectorHeartbeat | Not explicit |

**Entities with undefined deletion semantics:** `PowerNode`, `FloorPlan`, `Collector`, `EquipmentInterface` (M6). None of these is individually severe, but as a set they show the soft-delete principle from v1.0 was not systematically re-applied to every table v1.1 introduced.

---

## 11. Failure Matrix

| Component | Failure | Immediate Effect | Data Loss Risk | Recovery Mechanism | Duplicate Risk | Operator Visibility |
|---|---|---|---|---|---|---|
| PostgreSQL | Unavailable 5 min | API 503 on writes, reads may serve stale cache | None (nothing committed is lost) | Automatic on DB return; Celery retries with backoff | Low — retried tasks are idempotent (§21) | Yes — DB Health surface (§37) |
| Redis | Outage | Async side-effects delayed | **None** — Outbox row already committed (§22/§44, verified) | Automatic once Redis/dispatcher return | Low — event_id-keyed tasks | Yes — Queue Health surface |
| API (FastAPI) | Process crash | In-flight requests fail; stateless, restarts cleanly | None for committed data | Load balancer / orchestrator restart | None | Yes — standard process monitoring (not detailed in doc) |
| Celery worker | Crash mid-task | Task requeued (ack-late semantics) | None if handler is idempotent | Automatic requeue | **Requires idempotent handlers — verified as a stated principle (§2, §21)** | Yes — Queue Health |
| Scheduler (Celery Beat) | Down | New scheduled polls/aggregations stop firing | Growing telemetry gap while down | Restart; missed polls are simply missed (not backfilled unless collector buffers) | None | **Not explicitly covered** — no "scheduler health" surface named in §37's list |
| Central Collector | Down | Assigned Integrations stop polling | Telemetry gap for that window | Platform's own alarm engine fires on "integration stale" (self-monitoring, verified §44) | None | Yes |
| Integration / Device | Unreachable | Per-device circuit breaker; other devices unaffected | Telemetry gap for that device only | Backoff + auto-retry | None | Yes — PollLog, per-device |
| BMS/EMS | Unavailable | Treated as any device outage | None for DCIM's own data (§26 reconciliation layer keeps inventory independent) | Same as Integration | None | Yes |
| WAN (site↔central) | Outage | Edge collector buffers locally | Bounded by buffer size/duration (**open decision**, §49) | Backlog forwarded with true `occurred_at` on reconnect | **Requires dedup_key on forward — stated and verified (§19)** | Yes — CollectorHeartbeat |
| Notification provider | Timeout/failure | Retry per policy | None — recorded `failed` after max attempts, never silently dropped | Retry with backoff | Deduped via (alarm_id, channel, transition) — verified | Yes — NotificationAttempt |
| Floor-plan parser | Malformed/malicious file | **Undefined — no stated resource limit or isolation (C5)** | Potentially high (unbounded parse, crash, or worse) | Undefined | N/A | Undefined |
| Object storage (Attachments) | Unavailable | Import uploads / attachment reads fail | None for already-persisted DB rows; new uploads blocked | Depends on storage provider's own SLA — not architecturally specified beyond "S3-compatible recommended" (§49, open) | None | Not explicitly covered |

---

## 12. Scalability Matrix

| Domain | Main Workload | Growth Driver | Likely Bottleneck | Scaling Mechanism | Evidence Needed |
|---|---|---|---|---|---|
| Telemetry ingestion | Writes | Equipment count × metrics × poll frequency | Single-row insert overhead at ~83k/sec target rate (H8) | Partitioning (stated) + **batched writes (not stated — gap)** | **Benchmark required** for actual sustained write rate under realistic index overhead |
| Telemetry query (charts/reports) | Reads | Retention window × object count | Long-range raw scans if aggregates aren't hit correctly | Pre-computed hourly/daily aggregates (stated) | Benchmark required for aggregate-selection query planner behavior at scale |
| Outbox dispatch | Read+write | Mutation rate across every domain | Poll-based dispatcher contention on `status='pending'` index | Named queues, retry/backoff (stated) | Benchmark required; no discussion of dispatcher horizontal scaling |
| AuditLog | Writes | Every mutation, every domain | Unbounded single-table growth (H6) | **Not stated** — partitioning omitted | Needs the same partitioning treatment as Event/Telemetry |
| Alarm evaluation | Read (telemetry) + write (alarm state) | Metric count × rule count | Evaluation triggered per insert batch — could thrash under high-frequency metrics with many rules | Named `alarms` queue, isolated from `imports`/`telemetry` (stated, sound) | Benchmark required for rule-evaluation throughput per batch |
| WebSocket fan-out | Push | Concurrent connected clients × subscribed topics | Not discussed at all — no stated connection-count target or fan-out strategy (pub/sub via Redis vs. per-process) | **Not specified** | Needs an explicit design, not just "topics" |
| Floor-plan spatial queries (2D) | Read | Objects per room/floor plan | Renderer abstraction (§40) addresses client-side; server-side query cost at 10k+ SpatialObjects per large room not discussed | SpatialRenderer client-side strategy (stated); server query indexing implied but not detailed | Low risk — `SpatialLayer`/`FloorPlan` scoping bounds query size naturally |
| Rack elevation queries | Read | Equipment per rack (bounded, small — max ~52U) | None — inherently small per-query | Direct indexed lookup | None needed — low risk |
| Power path traversal | Read (recursive CTE) | Depth of power graph | Cycle in the graph causes runaway recursion (H4) | Recursive CTE (stated); **cycle guard missing** | Needs the H4 fix, then benchmark for typical topology depth |
| Capacity calculations | Read (from CapacitySnapshot, cached) | Object count × capacity domains | Refresh job cost at 10,000+ racks × multiple domains each | Periodic refresh, explicitly labeled derived/cached (sound design) | Benchmark required for refresh-job duration at target scale |

---

## 13. Security Trust-Boundary Matrix

| Boundary | Input | Threat | Control (as stated) | Enforcement Layer | Gap |
|---|---|---|---|---|---|
| Browser → API | JSON over HTTPS, JWT | Credential theft, injection, CSRF | Argon2id, JWT in memory, HttpOnly+Secure+SameSite=Strict refresh cookie, Pydantic validation, parameterized queries | Backend (stated, sound) | None found at this boundary specifically |
| Browser → WebSocket | Topic subscription after auth handshake | Unauthorized topic access (cross-site data leak) | Auth handshake required | Backend | **Site-scoping enforcement gap (H7) applies identically here** — any authenticated user can subscribe to any site's topic |
| API → Database | SQL via SQLAlchemy ORM | SQL injection, privilege escalation | Parameterized queries only, least-privilege DB role (no DDL/superuser) | Backend/DB grants | AuditLog append-only grant correctly enforced; **no separate elevated role defined for legitimate retention/archival** (H6) |
| API → Redis | Task payloads, cache keys | Cache poisoning, task injection | Private network trust | Infra | Acceptable given stated deployment (private Docker network); no additional control needed at current scale |
| Worker → Device (Integration) | SNMP/SSH/REST/etc. calls to configured endpoints | SSRF-via-endpoint-config, credential leakage in logs | Admin-configured endpoints, encrypted credentials, redacted logging | Backend/worker | **No stated egress allowlist/denylist** (M4) — relevant only if a non-admin role can configure integrations |
| Collector → Central DCIM | Outbound-only telemetry/backlog forwarding | Spoofed collector, replay | mTLS/signed callback (stated) | Network + app | Adequate as described; not yet implemented (correctly deferred, §19) |
| File upload → Parser | SVG/DXF/PDF/VSDX/image | XXE, zip bomb, decompression bomb, malformed-input crash, RCE | **None stated** | **None** | **CRITICAL gap (C5)** |
| DCIM → BMS/EMS | Poll requests via Integration adapters | Malicious/compromised BMS response | Adapter-level parsing of responses; normalization before DB write | Backend | Reasonable — response parsing isn't discussed with the same rigor as file-upload parsing, but responses are structured protocol data (SNMP/REST), a narrower surface than arbitrary file formats; **low priority, not flagged as a separate finding** |
| DCIM → ITSM (ServiceNow, future) | Outbound API calls | Credential leakage, SSRF | Framework-only (§27); no live implementation | N/A | Not applicable yet — correctly stated as framework-only, `NOT IMPLEMENTED` |
| DCIM → Notification provider | Outbound webhook/email/SMS/etc. | Credential leakage, message injection | Encrypted channel config (same mechanism as IntegrationCredential) | Backend | Adequate as described |

---

## 14. Architectural Contradictions

A direct search for the specific contrast pairs the review requested, verified against file content:

- **"real FK" vs. "polymorphic reference":** No contradiction — the document explicitly names its two accepted exceptions (telemetry/event/alarm object references, §4/§16/§38) rather than silently mixing patterns. This is handled correctly.
- **"single source of truth" vs. "duplicated":** No contradiction found in principle — every derived/cached structure (`CapacitySnapshot`, `telemetry_current_value`, `PowerCapacity.measured_load_kw`) is explicitly labeled non-authoritative. **However**, C3 means the *practice* can produce two simultaneously-"current" placement rows even though the *principle* forbids it — a contradiction between stated principle and actual enforced mechanism, not between two stated principles.
- **"at least once" vs. "exactly once":** No contradiction — §22 explicitly commits to at-least-once delivery with idempotent handlers and never claims exactly-once anywhere else in the document. Consistent.
- **"single-tenant" vs. "multi-tenant":** No contradiction — §32 recommends single-tenant and flags it as needing sign-off; nothing elsewhere in the document assumes multi-tenant.
- **"immutable" vs. "editable":** No contradiction for `RackModelRevision`/`EquipmentModelRevision` (`is_locked` correctly gates this). **A related, real inconsistency was found instead (M1):** §4's naming (`managed_asset_id` as a column) vs. §16/§25's actual column definition (`object_class`/`object_id`) for the same conceptual reference on `Alarm`/`Event`.
- **"historical" vs. "current-state-only":** No contradiction in principle (§29's temporal model is applied consistently to every table that has one) — but H9 identifies one table (`Alarm`) where it's unclear whether the temporal model was actually applied to repeated trigger/clear cycles, which is a completeness gap rather than a stated contradiction.

**Net result: one genuine documentation-level contradiction (M1), and one principle-vs-enforcement gap (C3) that reads as a contradiction between what §12/§8 claim and what the schema in §7/§8 actually enforces.** No contradiction was found between the tenancy, delivery-guarantee, or immutability statements.

---

## 15. Required Changes Before Phase 1

These map 1:1 to the CRITICAL findings — all are additions/corrections within existing sections, not new architecture:

1. Define the Asset Replacement operation and add `replaces_asset_id`/`replaced_by_asset_id` to `ManagedAsset` (C1). **DOMAIN DESIGN CHANGE + SCHEMA CHANGE.**
2. Correct the `EquipmentPlacement` exclusion constraint predicate for `side` (C2). **SCHEMA CHANGE.**
3. Add partial unique indexes enforcing exactly one current row per asset on `RackPlacement`/`EquipmentPlacement` (C3). **SCHEMA CHANGE.**
4. Extend optimistic concurrency (or an explicit locking discipline) to `RackPlacement`, `EquipmentPlacement`, `PowerConnection` (C4). **SCHEMA CHANGE + IMPLEMENTATION DETAIL.**
5. State the floor-plan import parsing safety boundary (XXE/zip-bomb/resource limits/isolation) (C5). **ARCHITECTURE CHANGE.**

Strongly recommended to fold in at the same time, given how directly they interact with the above (low cost, same review cycle):
6. Reconcile §4/§16/§25 naming for object references (M1). **DOCUMENTATION ONLY.**
7. Add `AuditLog` to the partitioning strategy and name a privileged retention/archival path distinct from the app's DB role (H6). **SCHEMA CHANGE + DOCUMENTATION ONLY.**
8. Publish complete column lists for `Rack`/`Equipment`/`PDU`/`UPS`/`Generator`/`PowerPanel`/`Sensor` under the v1.1 scheme so Phase 2 has a buildable spec (H1). **DOCUMENTATION ONLY.**
9. Get an explicit management answer on RBAC site-scoping timing (H7) — this doesn't have to mean building enforcement now, but Phase 1 cannot proceed on an *unconfirmed* assumption about it.

---

## 16. Changes That Can Safely Be Deferred

- H2 (asset-correlation algorithm), H3 (import-repeatability position matching), H4's cycle-guard (add when real topology-authoring UI exists), H5 (capacity rollup formula), H9 (alarm re-trigger row semantics), M2–M10, and all LOW findings/Observations. None of these block Phase 1's foundation work (repo, Docker, DB skeleton, auth, RBAC scaffolding, audit scaffolding) — they matter starting at the phase that actually builds the affected domain (Phase 2 for asset correlation, Phase 7 for power rollups, Phase 10 for alarm re-trigger semantics, Phase 6 for import repeatability), and should be resolved before that phase's exit gate, not before Phase 1's.
- H8 (telemetry batching/benchmark) matters starting at Phase 9, not Phase 1 — flag it now so Phase 9 planning budgets time for the benchmark, but it is not a Phase 1 blocker.

---

## 17. Open Organizational Decisions

Unchanged from `ARCHITECTURE_REVIEW.md` §49 (16 items) — this review adds one: **H7's RBAC-timing question needs an explicit answer, not just a reserved column, before Phase 1 begins**, since Phase 1 builds the RBAC foundation itself (§65) and "defer enforcement" is a decision that should be made knowingly, not by default silence.

---

## 18. Final Verdict

**ARCHITECTURE REQUIRES TARGETED REVISION**

No fundamental redesign is required — `ManagedAsset`, `PowerNode`, `EquipmentPlacement`/`RackPlacement`, the Outbox pattern, and the integration-layering model all survived adversarial testing intact. Five CRITICAL findings (C1–C5) and the RBAC-timing confirmation (H7) must be resolved — each is a narrow, specific correction to an already-written section, not a new architectural direction. Per §15 above, do not begin Phase 1 implementation until those corrections are made in `ARCHITECTURE_REVIEW.md` and the concrete subtype table definitions (H1) are published in full.

This session made no code changes, no migrations, no schema files, and did not modify `ARCHITECTURE_REVIEW.md`. Findings are reported for explicit human review and revision, per the stop condition.
