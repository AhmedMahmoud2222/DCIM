# ARCHITECTURE_REVISION_REPORT.md

**Project:** In-House DCIM Platform
**Revision:** v1.0 → v1.1
**Date:** 2026-09-16

---

## 1. Executive Summary

v1.0 established a sound overall shape — modular monolith, PostgreSQL as system of record, versioned rack/equipment catalogs, adapter-based integrations, quality-tagged telemetry. That shape is preserved in v1.1 without exception.

What v1.0 got wrong, and what v1.1 fixes, is **referential integrity at the seams between domains**. Every place v1.0 used a generic `(object_type, object_id)` pair — Spatial's link to Rack, Power's connection graph, Telemetry's object reference, Alarm's object reference — was a place where a dangling reference was structurally possible and nothing in the database would catch it. v1.1 replaces those seams with real identity (`ManagedAsset`, `PowerNode`) and real foreign keys everywhere Postgres can express them, and documents the two remaining exceptions (telemetry/event object references) explicitly rather than leaving them as an unstated gap.

The second class of fix is **equipment placement and spatial authority**, which v1.0 modeled too narrowly (rack-only equipment; a `Rack.room_id`/`SpatialObject` split that could diverge). v1.1 replaces both with unified, temporal placement records (`EquipmentPlacement`, `RackPlacement`) that make divergence structurally impossible rather than merely policed.

The third class is **operational completeness for a real production platform**: idempotency across every asynchronous workflow, a transactional outbox for domain events, alarm hysteresis, a collector/edge-collector abstraction, network topology, configuration-vs-discovered-state reconciliation, and a full security/audit hardening pass — all present as architecture in v1.0's phase list only by name, not by design.

---

## 2. Major Architectural Changes

1. Introduced `ManagedAsset` as the single stable identity for every independently-lifecycled physical asset (Rack, Equipment, PDU, UPS, Generator, PowerPanel, Sensor), using shared-primary-key inheritance so there is exactly one ID per asset, referenced by real FK everywhere.
2. Replaced `Equipment.rack_id`/`u_position` with `EquipmentPlacement` — a temporal, placement-type-aware record supporting rack-mounted, floor-standing, wall-mounted, and ceiling-mounted equipment.
3. Replaced the split `Rack.room_id` + independent `SpatialObject` with `RackPlacement` — one temporal record owning both a rack's room and its coordinates, with a database trigger enforcing that a rack's drawn position can never belong to a different room than its authoritative location.
4. Defined an explicit coordinate system (canonical integer millimeters, room-local origin, axis/rotation convention, calibration chain) that every importer and both 2D/3D renderers must conform to.
5. Replaced VSDX-centric floor-plan import thinking with a `FloorPlanImporter` protocol and per-format implementations (SVG/DXF/PDF/VSDX/Image), plus a diagnostics table explaining every import's outcome.
6. Replaced the untyped `PowerConnection.source_type/id` pair with `PowerNode` — a real identity table every power-graph participant (including sub-components like `PDUOutlet`) has a row in, making `PowerConnection` FK-safe.
7. Added `PowerCapacity` at every power-graph level (rated/configured/measured/thresholds/redundancy) — capacity is no longer only a connection graph.
8. Split network topology into physical (`EquipmentInterface`/`PortConnection`) and logical (`NetworkSegment`/`InterfaceSegmentMembership`) tables, retiring the fully-generic `Connection` table for this use.
9. Redesigned telemetry identity: `occurred_at`/`received_at` split, `dedup_key`, multi-source precedence resolution instead of last-write-wins.
10. Split telemetry "quality" into two dimensions — stored provenance/trust (`GOOD/BAD/.../MANUAL`) and computed staleness — replacing a single overloaded enum and a fixed `3×` staleness multiplier with a configurable, per-source model.
11. Introduced explicit `Collector`/`CollectorAssignment`/`CollectorHeartbeat`/`CollectorCapability` identity between `Integration` and `ProtocolDriver`, making edge-collector deployment additive rather than a future redesign.
12. Layered integrations as Protocol → Driver → VendorProfile → DeviceProfile → MetricMapping, keeping vendor knowledge as data, never as branches in driver code.
13. Documented idempotency mechanisms explicitly for every asynchronous workflow (ingestion, imports, polling, notifications, jobs, callbacks).
14. Introduced a transactional Outbox pattern for domain events, with a defined list of event types and an at-least-once, idempotent-handler delivery model.
15. Added alarm hysteresis (independent trigger/clear conditions), maintenance windows, and escalation policies, replacing a single-threshold model exposed to flapping.
16. Added a full notification architecture (channel/policy/recipient/attempt) with retry and dedup.
17. Strengthened event identity with `occurred_at`/`recorded_at`, `correlation_id`, `causation_id`, and `dedup_key`.
18. Added a configuration-vs-discovered-state reconciliation layer (`DiscoveredDevice`/`ReconciliationDiff`) that structurally prevents discovery from overwriting authoritative inventory.
19. Added an `ExternalReference` framework for ITSM correlation (ServiceNow-shaped, not ServiceNow-specific), framework only.
20. Introduced `ThermalProfile`/`ThermalZone` as separate, optionally-attached tables instead of columns on `EquipmentModelRevision`.
21. Formalized the U-position constraint as a GIST exclusion constraint parameterized by `side`, supporting front/rear half-depth co-occupancy.
22. Formalized temporal semantics (`effective_from/to` vs. `occurred_at`/`recorded_at` vs. `created_at`/`updated_at`) as one consistent model applied everywhere, renaming v1.0's `valid_from/valid_to` for consistency.
23. Strengthened the audit model (`request_id`, `correlation_id`, `source`, `reason`, masking of sensitive fields, database-grant-enforced append-only).
24. Hardened security with concrete values/mechanisms for JWT lifetimes, refresh rotation/revocation, cookie flags, CSRF, CORS, rate limiting, and lockout — previously stated only as principles.
25. Resolved the single-vs-multi-tenant question with an explicit recommendation (single-tenant) rather than leaving it silently assumed, while still flagging it for management sign-off.
26. Expanded capacity management beyond rack U to space/weight/power/cooling/PDU-outlet/circuit/UPS/generator/network-port/floor-loading domains.
27. Made PDU/power telemetry fully capability/profile-driven — reinforced as an explicit principle rather than left implicit.
28. Split async job architecture into named queues (polling/telemetry/alarms/imports/reports/notifications/maintenance/high_priority) so imports can never block telemetry.
29. Added optimistic concurrency (`version`/`If-Match`) and idempotency keys to the API layer.
30. Replaced the hardcoded 2D SVG/Canvas object-count rule with a `SpatialRenderer` abstraction and a configurable selection strategy.
31. Corrected the TimescaleDB "zero schema change" claim to an honest "designed to minimize migration impact, requires validation and benchmarking."
32. Reordered phases: Power moved earlier (Phase 7, before Integration/Telemetry/Alarm, since those need something real to attach readings and alarms to); 3D moved later (Phase 11, after Alarm, since a twin without status/power overlays is materially less useful to build first).
33. Added a Data Ownership Matrix and explicit Domain Boundaries diagram (no backward-pointing dependencies; domains publish events rather than cross-write).

---

## 3. Mandatory Gaps Closed — Requirement → Section Map

| Requirement (from the revision prompt) | Closed in |
|---|---|
| 4.1 Digital Object / Asset Identity | §4 |
| 5 Equipment Placement Model | §7 |
| 6 Spatial Authority and Consistency | §8 |
| 7 Coordinate System Architecture | §9 |
| 8 Floor Plan Importer Abstraction | §10 |
| 9 Import Diagnostics | §11 |
| 10 Power Topology — PowerNode | §13 |
| 11 Power Capacity | §14 |
| 12 Network Topology | §15 |
| 13 Telemetry Identity | §16 |
| 14 Telemetry Quality Model | §17 |
| 15 Collector Architecture | §18 |
| 16 Edge Collector Readiness | §19 |
| 17 Integration Layering | §20 |
| 18 Idempotency | §21 |
| 19 Internal Domain Events | §22 |
| 20 Outbox Pattern | §22 |
| 21 Alarm Hysteresis | §23 |
| 22 Alarm Notification Architecture | §24 |
| 23 Event Identity and Correlation | §25 |
| 24 Configuration vs. Discovered State | §26 |
| 25 ITSM / Service Management Readiness | §27 |
| 26 Thermal / CFD Readiness | §28 |
| 27 Rack U-Position Constraint | §7 (folded into `EquipmentPlacement`'s exclusion constraint) |
| 28 Rack/Equipment Model Versioning | Preserved from v1.0 (§5 of v1.0, restated where it interacts with §7/§29); no defect found |
| 29 Temporal / History Model | §29 |
| 30 Audit Model | §30 |
| 31 Security Hardening | §31 |
| 32 RBAC and Site Scoping | §32 |
| 33 Capacity Management | §33 |
| 34 PDU / Power Telemetry | §34 |
| 35 Async Job Architecture | §35 |
| 36 API Architecture | §36 |
| 37 Observability | §37 |
| 38 Database Architecture | §38 |
| 39 Time-Series Architecture (wording) | §39 |
| 40 2D Rendering Architecture | §40 |
| 41 3D Digital Twin | §41 |
| 42 3D Scale | §41 |
| 43 Single Source of Truth | §12 |
| 44 Data Ownership Matrix | §42 |
| 45 Domain Boundaries | §43 |
| 46 Failure / Resilience Architecture | §44 |
| 47 Backup / Disaster Recovery | §45 |
| 48 Architectural Decision Records (AD7–AD20) | §46 |
| 49 Phase Reordering | §47 |
| 50 Phase Gates | §47 |
| 51 Implementation Principles | §2 |
| 52 Architecture Consistency Check | §48 |
| 53 Remaining Open Decisions | §49 |

Every item in the revision prompt's mandatory list (§4.1 through §53) maps to a section that actually incorporates it — none is merely mentioned in passing.

---

## 4. Decisions Preserved From v1.0

No concrete architectural defect was found in the following, so they remain unchanged:

- **Technology stack** — React/TypeScript, FastAPI, PostgreSQL, SQLAlchemy, Alembic, Redis, Celery, Docker, Three.js. The stack itself was never the problem; the *schema and identity design on top of it* was.
- **Rack/Equipment model versioning** (`RackModel`/`RackModelRevision`, `EquipmentModel`/`EquipmentModelRevision`) — this was v1.0's strongest decision and required no change; v1.1 only reinforces how it interacts with the new placement/temporal model.
- **Human-reviewed floor-plan import** — the confidence-based, no-auto-create-without-confirmation principle was correct in v1.0; v1.1 only replaces the *mechanism* (importer abstraction, diagnostics) around that same principle.
- **Adapter-based integrations** — the core `Protocol`-based adapter interface was correct; v1.1 adds layering (driver/vendor-profile/device-profile) around it, not a replacement of it.
- **Quality-tagged telemetry** — the concept was right; v1.1 refines it into two dimensions (§17) rather than discarding it.
- **REST API, WebSocket, RBAC, audit, modular monolith, API-first, future AI/thermal readiness** — all preserved as stated in v1.0, extended where the revision required more specificity (security values, audit fields, capacity domains) but never redirected.

---

## 5. New Risks Introduced or Discovered

1. **`PowerNode` creation discipline** — every power-graph participant now needs an accompanying `PowerNode` row created by the `power` module's service layer; this is a code-review/testing risk if a future contributor adds a new participant type without it. Not present in v1.0 because v1.0's untyped pair didn't require a companion row (and didn't have integrity either — this is a net improvement, but it does add a discipline that must be tested for).
2. **Outbox dispatcher as a new operational component** — if unmonitored, it could silently back up. Flagged as a required Observability surface (§37), not left implicit.
3. **`ManagedAsset` shared-PK inheritance** requires every future new asset type (anything added after Phase 1) to be threaded through the same pattern deliberately — a design discipline, not a schema limitation, but one that must be documented for future contributors.
4. **Slightly larger Phase 1 schema** than a minimal single-collector deployment strictly needs (Collector/Heartbeat/Assignment/Capability tables exist even though only one Collector row will exist for many phases) — judged worth it against the cost of retrofitting edge-readiness later, but it is additional up-front schema to review and test.
5. **Reordered phases move 3D later** (Phase 11 instead of Phase 7) — this is a scope/timeline change worth flagging explicitly to stakeholders who may have expected an earlier visual milestone; the tradeoff (a materially more useful twin, once built) is stated in AD-adjacent rationale (§47) but is worth a direct conversation, not just a document note.

---

## 6. Remaining Open Decisions

See `ARCHITECTURE_REVIEW.md` §49 for the full table (16 items with recommended direction, owner, and phase dependency). None of these block Phase 1 from starting on the *foundation* work (repo, Docker, DB, auth skeleton) — the earliest hard blocker is the Postgres-extension-availability question (§49), needed before Phase 1's migration work can finalize the U-range constraint.

---

## 7. Phase Impact

The phase sequence changed in two places, both explained with rationale in §47 of the architecture review:

- **Power moved from Phase 10 to Phase 7** (immediately after Spatial, before Integration/Telemetry/Alarm) — Telemetry and Alarms are far more meaningful once there's a real power topology to attach readings and thresholds to; building them first and retrofitting Power afterward would have meant revisiting Telemetry/Alarm work.
- **3D Digital Twin moved from Phase 7 to Phase 11** (after Alarm/Event) — a 3D twin's main value proposition (see status at a glance, click through to alarms) requires Power and Alarm data to actually exist; building it earlier would produce an empty-feeling demo that gets rebuilt once real data is present.

No phase was removed or added; all 15 phases (0–14) from v1.0 remain, renumbered only where content shifted (Power/3D swap).

---

## 8. Architecture Gate Status

**READY FOR ARCHITECTURE APPROVAL**

This status reflects that every mandatory item in the revision prompt has been incorporated into `ARCHITECTURE_REVIEW.md` v1.1 with a concrete mechanism (table, protocol, state machine, or diagram), not merely acknowledged. It does not mean every open question is resolved — §49/§6 above list 16 organizational decisions that remain open by design, each with an owner and a phase by which it's needed. Architecture approval and organizational decision sign-off are tracked separately; the former does not wait on the latter except where a specific open decision is a stated blocker for a specific phase's schema lock-in (flagged individually in §49).

No implementation — code, migration, endpoint, component, or dependency — has been added as part of this revision. Per the mandatory stop condition, this session halts here pending explicit approval or a further revision request.
