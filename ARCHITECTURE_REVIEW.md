# ARCHITECTURE_REVIEW.md

**Project:** In-House DCIM Platform
**Version:** 1.1 (revision of v1.0)
**Phase:** 0 — Architecture (pre-implementation)
**Status:** READY FOR ARCHITECTURE APPROVAL
**Date:** 2026-09-16

Companion document: `ARCHITECTURE_REVISION_REPORT.md` (what changed from v1.0, gap-closure map, gate status).

---

## 0. Document Control

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-09-16 | Initial Phase 0 architecture |
| 1.1 | 2026-09-16 | Revision closing identity, placement, spatial-authority, power-topology, telemetry-identity, collector, event/outbox, and security gaps identified in the v1.1 review pass. See revision report for the full diff. |

This document supersedes v1.0 in place. Sections are renumbered; a mapping from v1.0 section numbers to v1.1 is in the revision report, not repeated here.

---

## 1. Executive Summary

The platform remains a relational, API-first system of record for physical data-center infrastructure: PostgreSQL as the single authoritative store, FastAPI as the sole write/read path, React (2D/3D/dashboard) as a pure consumer of that API. That core thesis from v1.0 is unchanged and is restated prominently in §12 (Single Source of Truth) because it is the one principle every other section must not violate.

v1.1 closes five structural gaps found in v1.0:

1. **Weak identity.** v1.0 used ad hoc `(object_type, object_id)` pairs in Spatial, Power, Telemetry, Alarm, and Event tables, with no real foreign-key target. v1.1 introduces **`ManagedAsset`** as the one stable identity for anything with an independent physical lifecycle (Rack, Equipment, PDU, UPS, Generator, PowerPanel, Sensor), so cross-cutting domains reference a real, FK-checked ID (§4).
2. **Rack-centric equipment.** v1.0 forced every equipment row to imply a rack. v1.1 introduces **`EquipmentPlacement`** as the sole authoritative placement record, supporting rack-mounted, floor-standing, wall-mounted, and ceiling-mounted equipment without a mandatory `rack_id` (§7).
3. **Divergent spatial state.** v1.0 had `Rack.room_id` and `SpatialObject` as two things that *could* disagree. v1.1 collapses a rack's room and coordinates into one `RackPlacement` record per active placement, with an explicit, enforced invariant against divergence (§8).
4. **Non-referential power topology.** v1.0's `PowerConnection` used typed-but-unenforced `(source_type, source_id)` pairs. v1.1 introduces **`PowerNode`** as a real join target so `PowerConnection` uses actual foreign keys (§10).
5. **Under-specified telemetry identity, quality, and delivery guarantees.** v1.1 adds an explicit occurred-at/received-at split, a `dedup_key`, source precedence for multi-source metrics, a corrected (non-absolute) staleness model, an idempotency policy across every asynchronous workflow, and a transactional Outbox for domain events (§14–§20).

Beyond these five, v1.1 adds network topology, collector/edge-collector architecture, integration layering (protocol → driver → vendor profile → device profile), alarm hysteresis, notification architecture, configuration-vs-discovered-state reconciliation, thermal-profile abstraction, ITSM references, expanded capacity domains, and a full temporal/audit/security hardening pass — each mapped to the specific requirement it closes in the revision report.

Nothing here is implemented. This is still a Phase 0 (architecture-only) deliverable; no code, migration, or dependency has been added.

---

## 2. Implementation & Architectural Principles

These govern every section that follows and every future implementation decision not explicitly covered here:

1. The database/domain model is authoritative. Every other surface (2D, 3D, elevation, reports, dashboards) is a derived view.
2. No duplicated source-of-truth representations — one fact, one row, one place it's read from.
3. Prefer strong relational integrity (real foreign keys) over generic/polymorphic references; use the latter only where Postgres genuinely cannot express the former (documented explicitly where this occurs).
4. JSONB only where variability is genuinely open-ended (vendor-specific attributes, custom fields) — never for core relational facts.
5. Domain boundaries are explicit; cross-domain reads happen through APIs/queries, cross-domain side-effects happen through domain events, not direct cross-module writes.
6. Integrations are adapter-driven; no protocol- or vendor-specific logic in domain/core code.
7. External systems (BMS, discovery, ITSM) never dictate the internal domain model — their data lands in a reconciliation layer first.
8. Every asynchronous workflow (ingestion, imports, jobs, notifications, callbacks) is idempotent by design, not by convention.
9. Long-running work (imports, reports) never blocks operational telemetry/alarm processing — separate queues, separate worker pools.
10. Every important mutation is auditable: who, when, what, before/after, why (when overriding a guarded action).
11. Historical state must remain reconstructable — "what was true at time T" is always answerable from stored data, never inferred.
12. 2D and 3D consume the same spatial truth; neither is allowed its own coordinate store.
13. Inventory (authoritative) and discovered state (observed) remain distinguishable and are never silently merged.
14. Human review is required wherever automated import confidence is insufficient; nothing is auto-created past a documented confidence floor.
15. Security is enforced server-side; frontend hiding is UX convenience only.
16. AI (future) consumes stable APIs/domain services, never UI internals or scraped state.

---

## 3. Technology Stack

Unchanged from v1.0 — no defect was found here, and the review explicitly preserves proven choices absent a concrete architectural reason to change them.

| Layer | Choice | Notes |
|---|---|---|
| Frontend | React 18 + TypeScript + Vite | |
| Styling | Tailwind CSS | Enterprise information density (§46 in v1.0, preserved) |
| Frontend state | TanStack Query (server state) + Zustand (UI state) | |
| 2D spatial | Renderer abstraction — see §26 (was a hardcoded object-count rule in v1.0; corrected) | |
| 3D | Three.js + `@react-three/fiber` | Instanced meshes, LOD — see §27 |
| Backend | Python 3.12 + FastAPI + Pydantic v2 | Async-first |
| ORM | SQLAlchemy 2.0 (async) + Alembic | |
| Database | PostgreSQL 16 | System of record |
| Time-series | Native partitioned Postgres now; TimescaleDB adoption is a validated, benchmarked later step, not an assumed zero-cost flip — see §31 (wording corrected from v1.0) | |
| Cache/broker | Redis | Celery broker + read cache |
| Background jobs | Celery + Celery Beat, named queues — see §25 | |
| Auth | JWT access + rotating refresh, Argon2id | Hardened in §21 |
| Containers | Docker + Docker Compose | |
| Logging/Observability | `structlog` + metrics + OpenTelemetry traces — see §23 | |

Preferred deployment shape remains a **modular monolith** (FastAPI + Postgres + Redis + Celery), not microservices, not Kafka, not a graph database, not Kubernetes — per the explicit instruction to avoid architectural fashion without a measurable need. Every "future" capability below (edge collectors, network topology, thermal/CFD) is designed to slot into this shape without a rewrite, not to justify introducing distributed infrastructure now.

---

## 4. Digital Object / Asset Identity

v1.0's weakest point: Spatial, Power, Telemetry, Alarm, and Event tables each carried their own `(object_type, object_id)` pair with no real join target, so nothing stopped an orphaned reference, and there was no single place to ask "what is this asset, everywhere."

**`ManagedAsset`** is the identity root for anything with an independent commissioning lifecycle:

```text
ManagedAsset   PK id (UUID), asset_type ENUM(rack/equipment/pdu/ups/generator/power_panel/sensor/cable),
               UQ asset_tag, lifecycle_status (planned/installed/active/reserved/maintenance/
               decommissioned/removed), external_ids JSONB (map of external-system → id, e.g.
               {"servicenow_ci": "...", "discovered_snmp_id": "..."}), created_at, decommissioned_at
```

Concrete tables use **shared-primary-key inheritance**: `Rack.id`, `Equipment.id`, `PDU.id`, `UPS.id`, `Generator.id`, `PowerPanel.id`, `Sensor.id` *are* `ManagedAsset.id` (each is `PK, FK→ManagedAsset`, not a second independent ID). There is exactly one stable identifier per physical asset, and it is the same value in every table that references it.

```text
ManagedAsset (id)
   ├── concrete row: exactly one of Rack / Equipment / PDU / UPS / Generator / PowerPanel / Sensor
   ├── EquipmentPlacement.equipment_id / RackPlacement.rack_id  → FK ManagedAsset.id
   ├── PowerNode.managed_asset_id                                → FK ManagedAsset.id (nullable, §10)
   ├── Alarm.managed_asset_id, Event.managed_asset_id             → FK ManagedAsset.id (see caveat below)
   ├── AuditLog.entity_id                                          → FK ManagedAsset.id (when entity is an asset)
   └── ExternalReference.managed_object_id                         → FK ManagedAsset.id (§32)
```

**Caveat, stated rather than hidden:** Telemetry, Alarm, and Event must also reference non-asset objects — Room, Site, Building, Floor — which are organizational containers, not assets, and are deliberately **not** part of `ManagedAsset` (a Room isn't commissioned/decommissioned as a physical unit the way a rack is). Postgres cannot express a single native foreign key that targets "whichever of these five tables" polymorphically. Where this is unavoidable (telemetry/alarm/event object references), v1.1 uses a documented, narrower pattern: `object_class ENUM(managed_asset, room, site, building, floor)` + `object_id UUID`, with **application-level validation plus a database trigger** that checks `object_id` exists in the table named by `object_class` before insert — real integrity, just not a native FK, and explicitly called out as the one place this pattern remains (§14, §22). Every other cross-cutting reference in this document uses a real FK to `ManagedAsset.id` or a similarly-scoped identity table (`PowerNode`, §10).

**Identifier authority:** `ManagedAsset.asset_tag` is the human-facing authoritative identifier (barcode/QR target, §41 of the original master prompt). `ManagedAsset.id` is the internal stable identifier every table actually joins on. `external_ids` and the new `discovered_device.external_identifier` (§19) hold IDs from other systems for correlation — they are never authoritative and never overwrite `asset_tag`/`id`.

---

## 5. System Architecture

Unchanged in shape from v1.0; the Power module gains Network as a sibling, and a Collector layer sits between Integration and physical devices.

```text
┌─────────────────────────────────────────────────────────────┐
│  React SPA — Dashboard · Rack Elevation · 2D Plan · 3D Twin   │
└───────────────────────┬───────────────────────────────────────┘
                         │ REST (JSON) + WebSocket
┌───────────────────────▼───────────────────────────────────────┐
│  FastAPI application (stateless, horizontally scalable)       │
│  Location │ Rack/Equipment │ Spatial │ Power │ Network         │
│  Integration │ Telemetry │ Alarm/Event │ Capacity              │
│  Auth/RBAC │ Audit │ Reporting │ Import/Export                 │
└──────────┬───────────────────────────────────────┬────────────┘
           │ SQLAlchemy (async)                     │ enqueue
┌──────────▼──────────────┐              ┌──────────▼────────────┐
│ PostgreSQL 16            │              │ Redis (broker+cache)  │
└──────────────────────────┘              └──────────┬────────────┘
                                                       │
                                           ┌───────────▼────────────┐
                                           │ Celery Workers          │
                                           │ (named queues, §25)     │
                                           └───────────┬────────────┘
                                                        │
                                           ┌────────────▼────────────┐
                                           │ Collector layer (§15)   │
                                           │ Central (Phase 8) /     │
                                           │ Edge (later, §16)       │
                                           └────────────┬────────────┘
                                                         │ protocol drivers
                                           ┌─────────────▼────────────┐
                                           │ Devices / EMS / BMS       │
                                           └───────────────────────────┘
```

One data-access layer: every worker and the API use the same SQLAlchemy models. This is unchanged and remains the mechanism, not just the claim, behind §12.

---

## 6. Component Architecture

```text
backend/app/modules/
  location/       equipment/       power/          network/        spatial/
  integration/    collector/       telemetry/       alarm/          notification/
  capacity/       reporting/       auth/            audit/          discovery/
  itsm/
```

`network/` and `collector/` are new modules (§13, §15–16). `discovery/` and `itsm/` hold the configuration-vs-discovered reconciliation layer (§19) and external-reference framework (§20) respectively — both intentionally thin (framework only, no live ServiceNow/discovery-protocol implementation in Phase 1–13). Extraction into an independently deployable service remains deferred until a module's operational load actually requires it — no module is split preemptively.

---

## 7. Equipment Placement Model

v1.0 modeled placement as `Equipment.rack_id` + `Equipment.u_position`, which structurally assumed every equipment item lives in a rack. It doesn't — floor-standing CRAC units, wall-mounted patch enclosures, and ceiling-mounted sensors are equipment too.

`Equipment` (a `ManagedAsset` subtype, §4) carries **no** `rack_id`/`u_position` columns. Placement is entirely owned by:

```text
EquipmentPlacement   PK id, equipment_id FK→ManagedAsset NOT NULL,
                     placement_type ENUM(rack_mounted/floor_standing/wall_mounted/ceiling_mounted/other),
                     room_id FK→Room NOT NULL,
                     rack_id FK→ManagedAsset NULL (Rack; required iff placement_type=rack_mounted),
                     u_range INT4RANGE NULL (required iff rack_mounted),
                     side ENUM(front/rear/both) NULL,
                     spatial_object_id FK→SpatialObject NULL UNIQUE (present once the item has been
                     placed on a floor plan — floor/wall/ceiling items use this for x/y/z; rack-mounted
                     items may also have one, for a 3D room view, without it being their placement authority),
                     rotation_deg NULL, mounting_method TEXT NULL, orientation NULL,
                     effective_from TIMESTAMPTZ NOT NULL, effective_to TIMESTAMPTZ NULL,
                     CHECK (placement_type <> 'rack_mounted' OR (rack_id IS NOT NULL AND u_range IS NOT NULL)),
                     EXCLUDE USING gist (rack_id WITH =, u_range WITH &&, side WITH <>)
                       WHERE (placement_type = 'rack_mounted' AND effective_to IS NULL),
                     IDX(equipment_id, effective_to), IDX(rack_id, effective_to)
```

"Current placement" = `effective_to IS NULL`; a materialized/queryable view `equipment_current_placement` (indexed the same way) is what the rack elevation endpoint and floor/wall equipment lists actually read, so callers never hand-roll the `effective_to IS NULL` filter. Moving equipment — rack to rack, or rack to floor-standing — closes the current row (`effective_to = now()`) and opens a new one; this **is** the placement history, so no separate history table is needed (correcting v1.0's split `Equipment` + `EquipmentPlacementHistory` into one temporal table, per the temporal model in §29).

The U-overlap exclusion constraint (needs `btree_gist`, flagged in §51/Open Decisions) now supports front/rear half-depth co-occupancy: two rows with the same `rack_id` and overlapping `u_range` are allowed only if their `side` differs and neither is `both`.

Rack elevation is still computed, never stored: `SELECT ... FROM equipment_current_placement WHERE rack_id = :id AND placement_type='rack_mounted' ORDER BY lower(u_range)`, joined to `RackModelRevision.height_u` for the frame.

---

## 8. Spatial Authority and Consistency

v1.0's exposure: `Rack.room_id` (authoritative location) and `SpatialObject` (authoritative coordinates) were two different tables that could, in principle, disagree after a partial update. v1.1 removes the possibility structurally rather than policing it with application checks alone.

```text
Room
  │  (exactly one ACTIVE FloorPlan at a time; prior ones retained, status=superseded)
  ▼
FloorPlan  →  SpatialLayer  →  SpatialObject (geometry + x/y/z/rotation, canonical mm)
  ▲
  │ referenced 1:1, optionally
  │
RackPlacement   PK id, rack_id FK→ManagedAsset NOT NULL, room_id FK→Room NOT NULL,
                spatial_object_id FK→SpatialObject NULL UNIQUE,
                x_mm NULL, y_mm NULL, rotation_deg NULL,
                effective_from NOT NULL, effective_to NULL,
                IDX(rack_id, effective_to) -- exactly one row per rack has effective_to IS NULL
```

`Rack` (the concrete `ManagedAsset` subtype table) has **no** `room_id` column. A rack's current room is `SELECT room_id FROM rack_placement WHERE rack_id = :id AND effective_to IS NULL` — read through the `rack_current_placement` view. There is exactly one place a rack's room lives.

**Enforced invariant:** when `RackPlacement.spatial_object_id` is set, a `BEFORE INSERT OR UPDATE` trigger checks that `spatial_object.spatial_layer_id → floor_plan.room_id = rack_placement.room_id`. A rack cannot be visually placed on a floor plan belonging to a different room than its authoritative placement — enforced in the database, not only in the service layer, closing exactly the divergence v1.0 left possible.

**Defined transitions:**
- **Rack moved** (§29's `RackMoved` event): one transaction closes the current `RackPlacement` row (`effective_to = now()`) and inserts a new one (new `room_id`, new/cleared `spatial_object_id`). This is the entire "move" operation — there is no second place to update.
- **Rack changes room without a floor plan yet:** valid — `spatial_object_id` is NULL until the rack is actually placed on a plan; `room_id` alone is enough for inventory/capacity purposes (a `planned`/`installed` rack may have a room but no drawn position yet).
- **Floor plan replaced:** old `FloorPlan.status → superseded`; its `SpatialObject` rows are either (a) carried forward by re-linking to the new plan's layers with recalibrated coordinates (an explicit migration step, reviewed like an import), or (b) archived read-only, and `RackPlacement.spatial_object_id` is cleared back to NULL (rack keeps its `room_id`, loses only its drawn position) until re-placed on the new plan.
- **Floor plan becomes inactive:** its objects stay queryable (history, audit) but are excluded from the default "current" 2D/3D view.
- **Spatial placement missing:** a valid, expected state for `planned`/newly-`installed` racks — never treated as an error, just "not yet drawn."
- **Imported geometry conflicts with inventory** (e.g., an import candidate looks like an existing rack's tag but at a different position): routed to `FloorPlanImportCandidate` review (§9), never silently overwriting `RackPlacement`.

`EquipmentPlacement` (§7) follows the identical pattern for non-rack-mounted equipment, using its own `spatial_object_id` link and the same room-consistency trigger.

---

## 9. Coordinate System Architecture

```text
Source Coordinates                 (SVG px / DXF units / VSDX EMU / PDF pt / image px / manual click / 3D pick)
   ↓  per-format Importer transform (§10)
Import Coordinate System            (raw numeric values in the source's native unit + its declared or assumed scale)
   ↓  calibration transform (user-supplied reference distance, or a declared DXF/VSDX unit-to-mm factor)
Canonical DCIM Coordinate System    (integer millimeters, room-local origin)
   ↓  × FloorPlan.calibration_scale (mm↔px)                 ↓  ÷1000 (mm→m)
2D Rendering Coordinates (px, per active viewport/zoom)      3D Coordinates (m, per active scene)
```

- **Units:** integer millimeters, canonical and stored. Sub-millimeter precision is not meaningful for rack/equipment placement; storing floats invites drift across repeated transforms, so canonical storage is integer.
- **Origin:** each `Room`'s coordinate system has its own origin at the room's top-left corner as drawn in plan view (matches the SVG/raster-image convention directly, avoiding a sign flip on every import).
- **Axes:** X increases right, Y increases downward (2D/plan convention). Z (height) is a separate, independent axis used only for 3D and wall/ceiling-mounted equipment; it is not part of the 2D plan's X/Y.
- **Rotation:** degrees, clockwise, 0° = object's declared "front" faces away from the room origin along +Y. Rack placement snaps to 90° by default (configurable); annotations and sensors are free-angle.
- **Scaling/calibration:** owned entirely by `FloorPlan.calibration_scale_mm_per_px` (raster/SVG imports needing user calibration) or a declared unit-per-drawing-unit factor (DXF/VSDX, which usually declare their own units) — the canonical mm value is computed once at import/placement time and stored; it is never recomputed implicitly from calibration on every read.
- **3D layout of multiple rooms/buildings:** schematic (rooms/buildings placed at defined, non-overlapping offsets from each other for readability) rather than true georeferenced GPS placement in Phase 1–13; true geo-referencing is an open decision (§53) if a future requirement needs it (e.g., a real campus map).
- **Precision:** integer mm is sufficient at the target scale (10,000+ racks); no floating-point coordinate ever becomes the canonical value.

---

## 10. Floor Plan Importer Abstraction

VSDX is one importer among several, not the foundation. A common interface, mirroring the Integration adapter pattern (§17):

```python
class FloorPlanImporter(Protocol):
    def can_handle(self, file: SourceFile) -> bool: ...
    def parse(self, file: SourceFile) -> RawGeometry: ...           # format-specific extraction
    def normalize(self, raw: RawGeometry) -> NormalizedGeometry: ... # common shape/text/dimension model
```

Registered implementations: `SVGImporter`, `DXFImporter`, `PDFImporter`, `VSDXImporter`, `ImageImporter`. `ImageImporter` (PNG/JPG) explicitly performs calibration-only import — no shape auto-detection — stated as `NOT IMPLEMENTED` for that capability rather than faked, unchanged from v1.0's stance.

```text
Source File → Importer.can_handle/parse → Geometry Normalization → Coordinate Transformation (§9)
   → Object Classification (rectangle aspect-ratio/size vs. known RackModelRevision footprints, text-label
     matching against existing asset tags)
   → Candidate Detection → Confidence Scoring
   → Human Review (FloorPlanImportCandidate queue)
   → Confirmed Spatial Objects (SpatialObject rows)
   → Inventory Association (new Rack, or linked to an existing one by asset-tag match)
```

Adding a new format is additive: implement the three-method interface and register it. Nothing in `spatial` or `rack`/`equipment` changes. No candidate becomes an authoritative `Rack`/`SpatialObject`/`EquipmentPlacement` without an explicit confirm action (batch-confirm above a confidence threshold is a UI convenience, never the default).

---

## 11. Import Diagnostics

```text
FloorPlanImportDiagnostics   PK job_id FK→FloorPlanImportJob UQ,
                             source_format, source_file_version, parser_name, parser_version,
                             objects_discovered, objects_classified, racks_detected, equipment_detected,
                             unsupported_object_count, warnings JSONB[], errors JSONB[],
                             confidence_histogram JSONB, ambiguous_count, rejected_count, confirmed_count,
                             started_at, finished_at, duration_ms
```

One row per import job, surfaced on the admin diagnostics screen (§23 Observability) so "why did this import produce poor detection" is answerable from stored data — parser version and confidence histogram specifically make regressions and format-quirk issues diagnosable after the fact, not just in the moment.

---

## 12. Single Source of Truth

Stated explicitly and prominently, because every other section depends on it holding:

```text
Database / domain model  =  AUTHORITATIVE
2D floor plan             =  visualization of SpatialObject
3D digital twin            =  visualization of SpatialObject
Rack elevation              =  derived from EquipmentPlacement (§7), never stored
Reports                      =  derived, read-only queries
Telemetry                     =  observed state, quality-tagged (§14), never treated as inventory fact
Discovered data (§19)          =  non-authoritative until explicitly reconciled into inventory
```

No section in this document introduces a second store for any of these. Where a cache exists (Redis, materialized views, `capacity_snapshot`), it is explicitly labeled derived/refreshable, never a second source of truth.

---

## 13. Power Topology — PowerNode

v1.0's `PowerConnection.source_type/source_id` and `target_type/target_id` were typed but not FK-checked — nothing prevented a dangling reference. v1.1 introduces a real join target:

```text
PowerNode      PK id, node_type ENUM(generator/ups/power_panel/power_circuit/pdu/pdu_outlet/
               equipment_power_input), managed_asset_id FK→ManagedAsset NULL UNIQUE
               (set for node types that are independently tracked assets: generator/ups/power_panel/pdu),
               owning_asset_id FK→ManagedAsset NULL (set for sub-component node types that belong to a
               tracked asset but aren't independently commissioned: power_circuit→its PowerPanel,
               pdu_outlet→its PDU, equipment_power_input→its Equipment), label
               CHECK (managed_asset_id IS NOT NULL OR owning_asset_id IS NOT NULL)

PowerConnection   PK id, source_node_id FK→PowerNode NOT NULL, target_node_id FK→PowerNode NOT NULL,
                  connection_type ENUM(feed/distribution), feed_label ENUM(A/B/single), phase,
                  voltage, rated_current_a, status, effective_from NOT NULL, effective_to NULL,
                  IDX(source_node_id, effective_to), IDX(target_node_id, effective_to)
```

Every Generator/UPS/PowerPanel/PDU (already `ManagedAsset` subtypes, §4) gets exactly one `PowerNode` row at creation. Every `PowerCircuit`/`PDUOutlet` (owned sub-components, not independent assets) gets a `PowerNode` row referencing its owner via `owning_asset_id`. `EquipmentPowerInput` — a connector on an equipment item, not a table of its own — is likewise a `PowerNode` row with `owning_asset_id` = the equipment's `ManagedAsset.id`; a dual-corded server has two such `PowerNode` rows (feed A, feed B).

`PowerConnection` now has real, enforceable foreign keys on both ends. Redundant A/B topology is two `PowerConnection` rows into two different `EquipmentPowerInput` nodes with different `feed_label`s — no schema special-casing. Power-path tracing (equipment → outlet → PDU → circuit → panel → UPS → generator) is a recursive CTE over `power_connection` joined through `power_node`, exposed as `GET /api/v1/equipment/{id}/power-path`; every edge in the returned path is now guaranteed to resolve to a real node, not a potentially-dangling typed pair.

---

## 14. Power Capacity

Power is modeled as more than a connection graph. Any `PowerNode` can carry a capacity record:

```text
PowerCapacity   PK id, power_node_id FK→PowerNode NOT NULL,
                rated_capacity_kw, configured_capacity_kw NULL (derates the nameplate rating for site
                conditions/redundancy policy; falls back to rated if unset),
                measured_load_kw NULL (a cached/derived read of the latest GOOD telemetry for this node —
                explicitly documented as non-authoritative; TelemetryReading is authoritative for the
                actual measured value),
                warning_threshold_pct, critical_threshold_pct, redundancy_factor (e.g. N/N+1/2N),
                effective_from NOT NULL, effective_to NULL, IDX(power_node_id, effective_to)
```

`available_kw = COALESCE(configured_capacity_kw, rated_capacity_kw) - measured_load_kw` (measured_load_kw defaults to 0 / "unknown" when no telemetry exists yet — surfaced as such, not silently treated as zero load in capacity reports). Applies uniformly at Generator, UPS, Panel, Circuit, PDU, and PDU-outlet level — one table, not per-level bespoke columns.

---

## 15. Network Topology

v1.0 relied on a fully generic `Cable`/`Connection` pair for everything physical, which cannot distinguish a physical patch cable from the logical VLAN membership riding over it. v1.1 splits these:

```text
EquipmentInterface   PK id, equipment_id FK→ManagedAsset NOT NULL, name (e.g. "Gi0/1"),
                     interface_type (copper/fiber/virtual), speed_mbps NULL, mac_address NULL
                     -- used uniformly for switch ports, patch-panel ports, and server NICs

PortConnection        PK id, source_interface_id FK→EquipmentInterface NOT NULL,
                     target_interface_id FK→EquipmentInterface NOT NULL, cable_id FK→Cable NULL,
                     status, effective_from NOT NULL, effective_to NULL
                     -- the physical link (patch cable A-end/B-end)

NetworkSegment         PK id, name, segment_type (vlan/subnet/logical), vlan_id NULL, cidr NULL

InterfaceSegmentMembership   PK (interface_id FK→EquipmentInterface, segment_id FK→NetworkSegment),
                             role (access/trunk/native)
                     -- the logical relationship, independent of the physical cable
```

`Cable` (physical medium: id, type, length) is now shared by both `PortConnection` (network) and, where a physical power cord is tracked as an asset, power connections — but the *logical* relationships (VLAN membership, power feed identity) live in their own domain-typed tables (`InterfaceSegmentMembership`, `PowerConnection`), never conflated with "is there a cable." v1.0's fully-generic `Connection` table is retired in favor of these two domain-specific tables, matching the same fix applied to Power (§13) — a generic polymorphic edge table is replaced with a typed one wherever the domain is known.

---

## 16. Telemetry Identity

v1.0's `(id, ts)` primary key on `TelemetryReading` didn't express *what* was measured clearly enough to support multiple sources reporting the same logical metric. v1.1:

```text
TelemetryReading   PK (id, occurred_at)   -- partition key
                   object_class ENUM(managed_asset/room/site/building/floor),  -- see §4 caveat
                   object_id UUID,        -- validated against object_class by trigger, not native FK
                   metric_id FK→TelemetryMetric NOT NULL,
                   source_id FK→Integration NULL, collector_id FK→Collector NULL,  -- §17
                   occurred_at TIMESTAMPTZ NOT NULL,   -- when the measurement actually happened (device/edge time)
                   received_at TIMESTAMPTZ NOT NULL,   -- when this platform ingested it (may lag occurred_at)
                   value NUMERIC, unit, quality ENUM(...),   -- §17
                   dedup_key TEXT NOT NULL,   -- hash(object_class, object_id, metric_id, source_id, occurred_at)
                   UNIQUE (dedup_key) per partition,
                   IDX (object_class, object_id, metric_id, occurred_at DESC)
```

Multiple sources reporting "Rack-001 Temperature" (BMS, a physical Sensor, a manual entry) are **not** merged into one row — each is its own `TelemetryReading` with a distinct `source_id`/`collector_id`, all sharing the same `(object_class, object_id, metric_id)`. A `source_precedence` config (per `TelemetryMetric`, overridable per object) resolves which source is shown as "the" current value on a dashboard; a `telemetry_current_value` view picks the highest-precedence non-stale `GOOD` reading per object+metric — the other sources' readings stay in the table, queryable for comparison/audit, never discarded.

**occurred_at vs. received_at** exists specifically to support buffered/backfilled data (a device polled after a WAN outage, an edge collector forwarding backlog, §18) — a late-arriving reading still lands in the correct historical bucket by `occurred_at`, while `received_at` lets operators see *when* the platform actually learned about it. Aggregation jobs use a configurable **watermark delay** (don't finalize an hour's aggregate until that hour is more than N minutes in the past by `received_at`) so backlogged data isn't silently excluded from rollups.

---

## 17. Telemetry Quality Model

Quality is **two independent dimensions**, not one overloaded enum, resolving v1.0's ambiguity directly:

1. **Provenance/trust** (stored, set at capture time, mutually exclusive): `GOOD, BAD, UNKNOWN, ESTIMATED, SIMULATED, CALCULATED, DERIVED, MANUAL`. This describes the *nature* of the value itself — a `SIMULATED` reading doesn't become `GOOD` just because time passes, and a `GOOD` reading doesn't retroactively change its provenance.
2. **Staleness** (computed at read time, never stored): a function of "how long ago was this the newest reading for this object+metric," which changes continuously — storing it would mean writing to old rows forever, which append-only telemetry explicitly must not do.

Staleness threshold is **not** a fixed `3×` multiplier applied everywhere. It is `Integration.stale_after_s` (or a `TelemetryMetric`-level override), defaulting to `3 × polling_interval_s` for polled sources, but explicitly documented exceptions:
- **Push/event-driven sources** (MQTT, webhook) have no polling interval — they require an explicit `stale_after_s`.
- **Manual entries** are exempt from staleness by default (or use a very long configurable window) — a human-entered value isn't "stale" the way a missed poll is.
- **ESTIMATED/SIMULATED/CALCULATED/DERIVED** readings are exempt from staleness — they were never "live" in the first place; their trust dimension already communicates that distinction.

The dashboard/API therefore reports both dimensions together, e.g. `{value: 7.8, unit: "kW", quality: "GOOD", is_stale: true, last_updated: "..."}`, never collapsing them into one field.

---

## 18. Collector Architecture

v1.0's `Integration` config went straight to an `IntegrationAdapter`. v1.1 makes the runtime executor an explicit, addressable entity — required for edge readiness (§19) and for observability (§23):

```text
Integration → Collector → ProtocolDriver → Device → MetricMapping → TelemetryReading
```

```text
Collector             PK id, name, site_id FK→Site NULL (NULL = central), collector_type ENUM(central/edge),
                      software_version, status, last_heartbeat_at, ip_address NULL
CollectorCapability     PK (collector_id FK, protocol_code) -- which drivers this collector instance can run
CollectorAssignment      PK (collector_id FK, integration_id FK) -- which Integrations currently run where
CollectorHeartbeat        PK (id, collector_id FK, ts), queue_depth, cpu_pct NULL, mem_pct NULL, status
```

`ProtocolDriver` is what v1.0 called `IntegrationAdapter` — renamed here only for clarity within the fuller Collector→Driver→VendorProfile→DeviceProfile stack (§20); its interface contract (`connect/poll/disconnect/normalize`) is unchanged from v1.0.

For Phase 1–8, exactly one `Collector` row exists (`collector_type=central`), and it *is* the Celery worker pool — no behavior change from v1.0, just an explicit identity for "which runtime executed this poll," which is what makes edge collectors (§19) additive rather than a redesign.

---

## 19. Edge Collector Readiness

Not built in Phase 1–13 — deliberately deferred — but the schema above already accommodates it without rework:

```text
                Central DCIM (API + DB)
                     │  secure outbound only (edge → central; central never dials in)
              Site Edge Collector (collector_type=edge, site_id=<site>)
              /      |       \
            SNMP    Modbus   BACnet
             │        │        │
          Devices   Devices   BMS
```

An edge `Collector` runs the same `ProtocolDriver` code locally, buffers `TelemetryReading`/`Event` rows in a local durable queue when the link to central is down, and forwards backlog once connectivity returns — each buffered reading keeps its true `occurred_at` (§16) and gets a fresh `received_at` on arrival at central, and `dedup_key` means a retried forward-batch is a safe no-op. Authentication is outbound-initiated (mTLS or a signed callback), never requiring an inbound port open at the site. `CollectorHeartbeat` is how central knows an edge collector is alive versus silently buffering versus genuinely down (a health signal distinct from "the devices behind it are unreachable").

---

## 20. Integration Layering

```text
Protocol (e.g. SNMP)
   ↓
Driver (GenericSNMPDriver — protocol mechanics only, no vendor knowledge)
   ↓
VendorProfile (SchneiderProfile — common OID conventions for that vendor)
   ↓
DeviceProfile (APCPDUProfile — a specific device family's default metric set)
   ↓
MetricMapping (per Integration instance; seeded from the DeviceProfile's defaults, overridable)
```

```text
Protocol        PK code, label
Driver           PK code, protocol_code FK→Protocol, implementation_class
VendorProfile     PK id, driver_code FK→Driver, vendor_name, default_config JSONB
DeviceProfile      PK id, vendor_profile_id FK→VendorProfile, model_name
DeviceProfileMetric PK (device_profile_id FK, metric_id FK→TelemetryMetric), default_source_key, unit, scale
Integration          device_profile_id FK→DeviceProfile NULL (NULL = generic, no profile)
```

Creating an `Integration` against a known `DeviceProfile` copies its `DeviceProfileMetric` rows into that Integration's own `MetricMapping` set, which the operator can then edit — vendor defaults exist as data, and `GenericSNMPDriver`/`GenericRESTDriver`/etc. never contain an `if vendor == "APC"` branch anywhere in code.

---

## 21. Idempotency

| Workflow | Duplicate-detection mechanism |
|---|---|
| Telemetry ingestion | `dedup_key` unique constraint per partition; conflicting insert is a no-op or quality-aware upsert |
| Event ingestion (external) | `UNIQUE (source, source_event_id)` on `Event` (§22) |
| Alarm evaluation | Naturally idempotent — re-evaluating current telemetry against `AlarmRule` never creates a second open `Alarm`; enforced by a partial unique index on `(alarm_rule_id, object_class, object_id)` WHERE `state IN ('active','acknowledged')` |
| Floor-plan imports | Re-uploading an identical file (checksum match) reuses the existing `FloorPlanImportJob`; already-confirmed candidates are not re-proposed |
| Integration polling | Celery task key `(integration_id, scheduled_at)`; a redelivered task for an already-completed slot is a no-op, checked via `PollLog` |
| Notifications | `NotificationAttempt` deduped on `(alarm_id, channel_id, alarm_state_transition)` within a debounce window |
| Background jobs generally | Every task carries a `job_correlation_id`; handlers are written as upserts, never blind inserts (Principle 8, §2) |
| External callbacks (webhooks in) | Caller-supplied idempotency key required where the protocol supports it; otherwise deduped by payload hash within a time window |

---

## 22. Domain Events and the Outbox Pattern

```text
Business Transaction
   UPDATE domain rows (e.g. close/open RackPlacement)
   INSERT AuditLog row (synchronous — the audit of *this* mutation is never deferred)
   INSERT OutboxEvent row
   COMMIT
        ↓ (only after commit)
Dispatcher reads pending OutboxEvent rows in commit order
   → enqueues one idempotent Celery task per event (keyed by event_id)
   → handlers run: notification, WebSocket push, capacity recalculation, alarm re-evaluation
     trigger, reporting-cache invalidation, integration-workflow triggers
   → success → status=dispatched ; failure → retry w/ backoff → dead-letter after max attempts
```

```text
OutboxEvent   PK id, event_id UUID UNIQUE, event_type, aggregate_type, aggregate_id, payload JSONB,
              occurred_at, created_at, status ENUM(pending/processing/dispatched/failed),
              attempts, next_attempt_at, last_error, IDX(status, next_attempt_at)
```

Representative event types: `RackCreated, RackMoved, RackModelRevisionCreated, EquipmentInstalled, EquipmentMoved, EquipmentRemoved, PowerConnectionChanged, TelemetryReceived, TelemetryQualityChanged, AlarmRaised, AlarmAcknowledged, AlarmCleared, FloorPlanImported, ImportCandidateConfirmed, IntegrationStatusChanged`.

**Important distinction:** the *primary* audit entry for a mutation is written synchronously, in the same transaction as the mutation — it is never at risk of being lost if the dispatcher is down. The Outbox/event system is for *downstream, asynchronous* effects only (a notification, a recalculation, a push to a connected WebSocket client). This is why a Redis outage never loses data (§29): the durable record is the committed `OutboxEvent` row; Redis is only the transport that moves it to a worker.

---

## 23. Alarm Hysteresis

`AlarmRule` gains independent trigger and clear conditions:

```text
AlarmRule   PK id, name, metric_id FK→TelemetryMetric, scope_type, scope_id NULL,
            trigger_operator, trigger_threshold, trigger_duration_s,
            clear_operator, clear_threshold, clear_duration_s,   -- independent of trigger; may default to
                                                                   the trigger reversed but is overridable
            severity, enabled
```

State machine: `OK → (trigger condition holds continuously for trigger_duration_s) → ACTIVE → (clear condition holds continuously for clear_duration_s) → CLEARED`. `ACKNOWLEDGED` is an orthogonal flag settable while `ACTIVE`. `SUPPRESSED` is driven by an active `MaintenanceWindow`:

```text
MaintenanceWindow   PK id, scope_type (object/site/room), scope_id, starts_at, ends_at,
                    created_by FK→User, reason
```

Alarms whose object falls inside an active window are still computed (state transitions still happen, still recorded as `Event`s) but suppressed from notification and from "active alarm" counts — never deleted or skipped, just silenced. Escalation: `AlarmEscalationPolicy(alarm_rule_id, level, after_s, notification_policy_id)` — an unacknowledged `ACTIVE` alarm escalates to the next `NotificationPolicy` level after its configured delay. This directly prevents the flapping v1.0's single-threshold model was exposed to.

---

## 24. Alarm Notification Architecture

```text
Alarm state transition → OutboxEvent(AlarmRaised/Acknowledged/Cleared)
   → resolve applicable NotificationPolicy (by severity + scope)
   → for each NotificationRecipient × NotificationChannel → NotificationAttempt
   → dispatched via the `notifications` Celery queue (§25), retried with backoff,
     deduped per (alarm_id, channel_id, transition) within a debounce window (§21)
```

```text
NotificationChannel     PK id, type ENUM(email/sms/teams/whatsapp/webhook/servicenow),
                       config JSONB (endpoint/address; credentials encrypted identically to
                       IntegrationCredential, §17 of v1.0, preserved)
NotificationPolicy       PK id, name, severity_filter, scope_type NULL, scope_id NULL
NotificationRecipient      PK id, policy_id FK, recipient_type (user/external_address),
                          user_id FK→User NULL, address NULL
NotificationPolicyChannel   PK (policy_id FK, channel_id FK), order
NotificationAttempt          PK id, alarm_id FK NULL, event_id FK NULL, channel_id FK, recipient,
                            status (pending/sent/failed/suppressed_dedup), attempt_count,
                            last_attempted_at, error
```

No channel is implemented against a live provider in Phase 1–13 beyond what's explicitly scheduled — the framework (policy → recipient → channel → attempt, with retry/dedup) is the Phase-11 deliverable; specific provider integrations (which Teams/ServiceNow tenant, which SMS gateway) are an open decision (§53), not assumed.

---

## 25. Event Identity and Correlation

```text
Event   PK event_id UUID, occurred_at, recorded_at,   -- same split rationale as telemetry, §16
        source ENUM(system/user/integration), source_event_id NULL (the external system's own ID
        for the same occurrence, when one exists — e.g. a BMS's internal event log ID),
        object_class, object_id (same pattern/caveat as §4/§16),
        event_type, severity, description,
        correlation_id NULL (groups events from one logical workflow — e.g. every candidate-confirmation
        event from one floor-plan import run shares a correlation_id),
        causation_id NULL (the event_id that directly caused this one — e.g. an AlarmRaised event's
        causation_id is the TelemetryReceived event that tripped the rule, enabling a causal-chain query),
        dedup_key (hash of source+source_event_id, or source+object+event_type+occurred_at when no
        source_event_id exists) UNIQUE,
        IDX(object_class, object_id), IDX(occurred_at), IDX(correlation_id)
```

`correlation_id`/`causation_id` are what make "why did this alarm fire" or "what else happened as part of this import" a direct query instead of manual log archaeology — the same IDs are propagated through `OutboxEvent.payload` and into `request_id`/`trace_id` (§23 Observability) for full request-to-effect tracing.

---

## 26. Configuration vs. Discovered State

A dedicated reconciliation layer, kept structurally separate from authoritative inventory:

```text
DiscoveredDevice        PK id, integration_id FK→Integration, external_identifier (e.g. IP + sysObjectID),
                       first_seen_at, last_seen_at, raw_attributes JSONB (sysDescr, firmware, reported
                       serial, reported model, observed values),
                       reconciliation_status ENUM(unmatched/matched/conflicting/ignored),
                       matched_asset_id FK→ManagedAsset NULL

ReconciliationDiff        PK id, discovered_device_id FK, field_name, inventory_value, discovered_value,
                         detected_at, resolution_status ENUM(open/accepted/rejected),
                         resolved_by FK→User NULL, resolved_at NULL
```

Discovery/polling **never writes to `Rack`/`Equipment`/`PDU` directly.** A reconciliation workflow surfaces `ReconciliationDiff` rows for operator decision: accepting one updates the authoritative record through the normal, audited mutation path (§30); rejecting one leaves inventory untouched and the diff flagged. This is what makes "the system must be able to identify configuration drift... and not overwrite authoritative inventory blindly" true by construction rather than by convention.

---

## 27. ITSM / Service Management Readiness

```text
ExternalReference   PK id, object_class, object_id (same pattern as §4/§16/§25), system ENUM(servicenow/
                    other), reference_type ENUM(incident/change/request/ticket), external_id,
                    external_url NULL, status_cached NULL, created_at, last_synced_at NULL
```

Generic enough to hold a reference to any external ticketing/change system against any managed object. No live ServiceNow (or other ITSM) integration is implemented in Phase 1–13 — this is framework only, so that when a concrete ITSM requirement/vendor is confirmed (§53), it's an adapter addition, not a schema change.

---

## 28. Thermal / CFD Readiness

Thermal properties are **not** columns on `EquipmentModelRevision`. A separate, optionally-attached table:

```text
ThermalProfile   PK id, equipment_model_revision_id FK NULL, rack_model_revision_id FK NULL,
                heat_output_w, heat_output_mode ENUM(fixed/load_based), airflow_cfm,
                airflow_direction ENUM(front_to_rear/front_to_top/other),
                rated_inlet_temp_c, rated_exhaust_temp_c
ThermalZone       PK id, room_id FK→Room, name, spatial_object_id FK→SpatialObject NULL
                (reserved for future CFD zone geometry — not populated by any Phase 1–13 feature)
```

This lets thermal/CFD analysis (measured vs. estimated vs. simulated, per the quality model in §17) be added by extending `ThermalProfile`/`ThermalZone` and consuming existing quality-tagged telemetry, without touching the core `EquipmentModelRevision`/`RackModelRevision` tables at all.

---

## 29. Temporal / History Model

Four distinct concepts, never conflated, applied consistently across every table above:

| Concept | Meaning | Columns | Example tables |
|---|---|---|---|
| Current state | What is true now | derived: `WHERE effective_to IS NULL` | `RackPlacement`, `EquipmentPlacement`, `PowerConnection`, `PowerCapacity`, `MaintenanceWindow` |
| Historical (business) state | What was true at time T | `effective_from`, `effective_to` | same tables — `WHERE effective_from <= T AND (effective_to IS NULL OR effective_to > T)` |
| Event history | What happened, and when | `occurred_at`, `recorded_at` | `Event`, `TelemetryReading` |
| Audit history | Who changed what, and why | `created_at` (row bookkeeping only, never business validity), `AuditLog` fields (§30) | every table, plus `AuditLog` |

`created_at`/`updated_at` (row bookkeeping) exist on nearly every table but are never used to answer "what was true" — that's exclusively `effective_from`/`effective_to` (business facts) or `occurred_at`/`recorded_at` (events/telemetry). v1.0's naming (`valid_from`/`valid_to`) is renamed to `effective_from`/`effective_to` throughout v1.1 for consistency with this table — a naming correction, not a semantic change.

---

## 30. Audit Model

```text
AuditLog   PK audit_id, actor_user_id FK→User NULL, action, entity_type, entity_id,
           timestamp, request_id, correlation_id, source ENUM(ui/api/system/import),
           user_agent NULL, before JSONB, after JSONB, result ENUM(success/failure), reason NULL
```

Fields marked `sensitive` in a small config (credential payloads, password hashes, encrypted secrets) are **never** captured in `before`/`after` — redacted to `"***REDACTED***"` at the serialization layer before the row is written, not after. `reason` is required by the service layer for specific guarded actions (e.g., re-pointing a `Rack` to a different `RackModelRevision`, §31/original v1.0's AD2). Append-only is enforced at the database grant level: the application's DB role has no `UPDATE`/`DELETE` privilege on `audit_log` — only `INSERT` and `SELECT` — so even a bug in application code cannot silently rewrite history.

---

## 31. Security Hardening

| Control | Value / mechanism |
|---|---|
| Access JWT lifetime | 15 minutes |
| Refresh token | 7 days, rotating (new token issued + old invalidated on every use), stored server-side hashed for explicit revocation |
| Password hashing | Argon2id, parameters tuned to ~250ms verify time on target hardware |
| Refresh cookie | `HttpOnly`, `Secure`, `SameSite=Strict` |
| CSRF | Bearer-header access tokens carry no CSRF risk; the refresh-cookie endpoint specifically adds a double-submit CSRF token in addition to `SameSite=Strict` |
| CORS | Explicit origin allow-list (frontend origin(s) only), credentialed requests restricted to that list |
| Rate limiting | Login endpoint limited per-IP and per-account; exact thresholds are tunable configuration, not hardcoded |
| Account lockout | Progressive delay after repeated failures, admin- or timer-based unlock |
| Password policy | Configurable minimum length/complexity, not hardcoded (Principle: avoid hard-coded values) |
| Secret management | Environment variables for Phase 1–13; a KMS/Vault integration is a later, explicitly open decision (§53), not built |
| Encryption at rest | Disk-level (managed Postgres provider, an ops concern) + application-level Fernet specifically for `IntegrationCredential`/`NotificationChannel.config` secrets (defense in depth, unchanged from v1.0) |
| TLS | Terminated at reverse proxy; internal traffic on a private Docker network |
| Service-to-service auth | Internal (API↔worker via Redis/DB) trusts the private network; anything externally reachable (a future edge collector callback) authenticates via mTLS or a signed key, never the user-facing JWT scheme |
| Token storage | Access token in memory only; refresh token in an `HttpOnly` cookie — **never** `localStorage`, closing the specific gap called out in the review |

---

## 32. RBAC and Site Scoping

**Tenancy decision, stated explicitly rather than left implicit:** this architecture assumes and recommends **single-tenant** (one `Organization` per deployment) for an in-house platform, consistent with the "in-house DCIM" framing and with no stated cross-organization isolation requirement. This is a recommendation, not a unilateral decision on the user's behalf — it is carried into Open Decisions (§53) requiring explicit sign-off before Phase 2's schema is locked.

Given single-tenant, RBAC generalizes v1.0's `UserRole.scope_site_id` into a broader, still-unenforced-by-default reservation:

```text
RoleAssignment   PK (user_id FK→User, role_id FK→Role, scope_type ENUM(global/site/building), scope_id NULL)
```

Site-scoped enforcement (actually filtering queries by a user's assigned scope) is **not wired up** in Phase 1 — it becomes architecturally necessary, not optional, the first time two operationally distinct teams share one deployment and need isolation (a concrete trigger condition, not a vague "later"). Until then, `RoleAssignment` rows with `scope_type != global` may exist but are not enforced; enforcement is additive middleware on top of the existing `require_permission` dependency (§8 of v1.0, unchanged), not a schema change, when the trigger condition is met.

---

## 33. Capacity Management

Capacity is modeled per domain, not only as rack U:

```text
CapacitySnapshot   PK id, object_class, object_id, capacity_domain ENUM(space_rack_u/weight/power/
                   cooling/pdu_outlet/circuit/ups/generator/network_port_copper/network_port_fiber/
                   floor_loading), rated, configured, measured, reserved,
                   available NUMERIC GENERATED ALWAYS AS (configured - measured - reserved) STORED,
                   warning_threshold, critical_threshold, computed_at
```

A periodically refreshed (Phase 12) cache, **not** an authoritative store — its source of truth per domain is `RackModelRevision.height_u`/`weight_capacity_kg` (space/weight), `PowerCapacity` (power/cooling proxy), `PDUOutlet` count (pdu_outlet), `EquipmentInterface` count (network ports), explicitly labeled derived per §12. `reserved` capacity accounts for `planned` (not yet `active`) racks/equipment holding a claim on capacity before they're physically live — distinguishing current vs. available vs. reserved as required.

---

## 34. PDU / Power Telemetry — Capability-Driven, Not Column-Driven

No PDU/PDUOutlet table carries fixed columns for voltage/phase/current/power/energy/power-factor/frequency/outlet-status. Every one of those is a `TelemetryMetric` reading scoped to the relevant `PowerNode` (§13), produced by whatever `MetricMapping` that Integration's `DeviceProfile` (§20) actually defines. Whether a given PDU model exposes power factor is answered by "does a `MetricMapping` exist for `pdu.power_factor` on this Integration," never by a nullable column existing on a `PDU` row that may or may not be populated. This closes the "don't require every PDU model to expose every field" requirement structurally, consistent with how v1.0 already treated PDU readings as telemetry rather than static attributes — made explicit here as a stated principle rather than left implicit.

---

## 35. Asynchronous Job Architecture

Named Celery queues, each with independent worker pool sizing so one workload never starves another (the specific concern: a large import must never delay telemetry/alarm processing):

```text
polling | telemetry | alarms | imports | reports | notifications | maintenance | high_priority
```

- **Retries:** per-task `max_retries` + exponential backoff (`countdown = base × 2^attempt`).
- **Timeouts:** `soft_time_limit`/`time_limit` per task type.
- **Rate limiting:** Celery `rate_limit` per task type, protecting downstream devices from poll storms.
- **Dead-letter:** a task exhausting retries writes a `JobFailure` row (`object_class=celery_task`, `job_correlation_id`) visible in Observability (§23) rather than vanishing silently.
- **Idempotency/correlation:** per §21/§25 — never repeated here, referenced.

---

## 36. API Architecture

`/api/v1` REST is preserved. Additions:

- **Optimistic concurrency:** a `version INT` column on frequently-contended entities (`Rack`, `Equipment`, `AlarmRule`); `PATCH` requires `If-Match`, returns `409 Conflict` on mismatch.
- **Idempotency-Key header:** supported on POST endpoints with external side-effects (trigger a floor-plan import, send a test notification) so client retries are safe.
- **Correlation:** every request gets/propagates `X-Request-Id` (generated if absent), stamped into `AuditLog`/`Event`/`OutboxEvent` for end-to-end tracing (§23, §25).
- **Errors:** `application/problem+json` (RFC 7807), unchanged from v1.0.
- **Sync vs. async:** simple CRUD is synchronous; floor-plan import, report generation, bulk import, and anything touching an external device are asynchronous — `202 Accepted` + a job resource URL to poll or a WebSocket topic to subscribe to.
- **WebSocket:** unchanged topic-subscription model from v1.0 (`rack:{id}`, `alarms`, `site:{id}:telemetry`).

---

## 37. Observability

```text
Application Logs (structlog, JSON) · Metrics (request latency, queue depth, poll success rate,
ingestion rate, alarm-eval latency) · Traces (OpenTelemetry)
Integration Health · Collector Health · Queue Health · Database Health
Telemetry Ingestion Health · Alarm Processing Health · Import Health
```

Correlation chain, propagated and loggable together: `request_id` (one HTTP call) → `correlation_id` (one logical business transaction, may span several requests/events) → `trace_id` (OTel span tree) → `job_id` (one Celery task instance) → `event_id` (one domain/outbox event). Each per-subsystem health surface is exposed both as an admin screen and a small `/api/v1/admin/health/{subsystem}` read, reusing the diagnostics tables already defined per subsystem (`PollLog`, `FloorPlanImportDiagnostics`, `CollectorHeartbeat`, `JobFailure`).

---

## 38. Database Architecture

- **Schema ownership:** a single Postgres schema (`public`) with domain-prefixed table names for Phase 1–13 — simpler migrations and no cross-schema FK friction; multi-schema separation is reserved for if/when a module is actually extracted into its own service (§6), not adopted preemptively.
- **FK strategy:** real foreign keys everywhere Postgres can express them; the two documented exceptions (telemetry/event/alarm `object_class`+`object_id`, §4/§16/§25) use application validation plus a database trigger, stated explicitly rather than silently accepted as a gap.
- **JSONB boundaries:** unchanged principle — `custom_attributes`, model-revision `mounting`/`features`, `raw_attributes` (discovered state), notification channel config. Never core relational facts.
- **Partitioning:** `TelemetryReading` and `Event` are declaratively range-partitioned by month from Phase 1.
- **Concurrency:** optimistic locking (`version`, §36) for user-editable master data; `SELECT ... FOR UPDATE` plus the GIST exclusion constraint together (belt-and-suspenders) on the U-range placement write path, since bulk import can race the constraint check with an in-flight transaction.
- **Required extensions:** `btree_gist` (U-range exclusion constraints, §7), `pgcrypto` or equivalent (UUID generation). Both flagged for confirmation in the target hosting environment (§53).

---

## 39. Time-Series Architecture — Corrected Wording

v1.0 stated TimescaleDB adoption would require "zero schema change." Restated accurately:

- The telemetry schema (`TelemetryReading`'s composite key on `(id, occurred_at)`, append-only inserts, no in-place updates) is **designed to minimize migration impact** if TimescaleDB is adopted — it is not a guarantee of zero effort.
- Native Postgres declarative partitioning is the Phase 1–8 mechanism and is **acceptable as the permanent mechanism** if TimescaleDB is never adopted — it is not a stopgap that must be replaced.
- TimescaleDB adoption must be **validated against the actual deployment environment** (can the extension be installed at all — flagged in §53) before being scheduled into Phase 9.
- Retention policies, continuous aggregates, and compression are Timescale features that must be **benchmarked** against this schema's actual query patterns, not assumed to behave identically to the hand-rolled `TelemetryAggregateHourly`/`Daily` + `RetentionPolicy` mechanism used in native Postgres.
- Any eventual migration requires tested migration scripts and a documented operational runbook — not an in-place `ALTER`.

---

## 40. 2D Rendering Architecture

v1.0 hardcoded "SVG below N objects, Canvas above." Replaced with a renderer abstraction:

```python
class SpatialRenderer(Protocol):
    def render(self, objects: list[SpatialObject], viewport: Viewport) -> None: ...
```

Implementations: `SVGRenderer`, `CanvasRenderer`, `WebGLRenderer`. Selection is delegated to a `RendererSelectionStrategy`, evaluated per floor-plan load against: object count actually in the current viewport (not the whole floor plan), presence of complex custom geometry, a runtime WebGL-availability check, and threshold values held in `SpatialRenderingConfig` (tunable configuration, not a hardcoded constant in code). All three renderers consume the identical `SpatialObject` query result — switching renderers is a rendering-layer decision that never touches the domain model, closing the "support switching renderer without changing spatial domain data" requirement directly.

---

## 41. 3D Digital Twin

Unchanged principle (§12): `SpatialObject` is source, 3D is never authoritative. Mechanics for scale:

- **Instancing:** one draw call per rack-model shape across all racks sharing that geometry.
- **LOD:** near objects render with elevation detail; far objects render as simplified boxes.
- **Culling:** frustum + occlusion culling on top of instancing.
- **Lazy loading:** only the active Room's (or Building's, in an overview mode) `SpatialObject`s are loaded into the WebGL scene graph — switching rooms unloads the previous one. The entire enterprise is never loaded into one scene at once.
- **Aggregation:** a site-overview 3D mode renders Buildings as simple blocks until the user drills into one — same domain data, different visual level of detail, not a separate dataset.
- **Shared identity:** a Three.js object's `userData.managedAssetId` always equals the domain `ManagedAsset.id` (§4) used everywhere else — one selection handler serves 2D, 3D, and list views identically.
- **Status overlays:** alarm/online-offline coloring is computed client-side from the same `Alarm`/`status` data the dashboard uses — not a 3D-only data source.

This directly answers the target-scale question: 10,000 racks are never all resident as heavy meshes simultaneously — instancing collapses repeated geometry to one draw call per shape, and lazy loading bounds what's in the scene graph to the room/building actually being viewed.

---

## 42. Data Ownership Matrix

| Domain | Owns |
|---|---|
| Location | Organization→Room physical hierarchy |
| Inventory (Rack/Equipment modules) | Asset identity (`ManagedAsset`) and lifecycle |
| Rack | Rack models/revisions and rack instances |
| Equipment | Equipment models/revisions, equipment instances, `EquipmentPlacement` |
| Spatial | `FloorPlan`, `SpatialObject`, `RackPlacement`/`EquipmentPlacement` coordinates |
| Power | `PowerNode`, `PowerConnection`, `PowerCapacity` |
| Network | `EquipmentInterface`, `PortConnection`, `NetworkSegment` |
| Integration | `Integration`, `MetricMapping`, `VendorProfile`/`DeviceProfile` |
| Collector | `Collector`, heartbeat/assignment/capability |
| Telemetry | `TelemetryReading`, aggregates, retention |
| Discovery | `DiscoveredDevice`, `ReconciliationDiff` |
| Alarm | `AlarmRule`, `Alarm`, `MaintenanceWindow`, escalation |
| Notification | `NotificationChannel/Policy/Recipient/Attempt` |
| Event | `Event` (append-only ledger) |
| Capacity | `CapacitySnapshot` (derived) |
| ITSM | `ExternalReference` |
| Audit | `AuditLog` |
| Auth | `User`, `Role`, `Permission`, `RoleAssignment` |

**Cross-domain references** are always by real FK to the owning domain's identity table (`ManagedAsset`, `PowerNode`, `Room`) — never by a domain copying another domain's data into its own tables.

---

## 43. Domain Boundaries

```text
Location  →  Inventory (Rack/Equipment)  →  Spatial  →  Power  →  Network
                                                     ↘        ↘
                                                      Integration → Collector → Telemetry → Alarm → Notification
                                                                                      ↓
                                                                                  Capacity, Reporting
```

No arrow points backward — Telemetry never writes to Inventory; Alarm never writes to Telemetry; Spatial never writes to Power. Where a downstream domain needs to react to an upstream change (Capacity recalculating after `EquipmentMoved`), it does so via a domain-event handler (§22), never a direct cross-module write. Domains **publish** events; they only **directly call** another domain's read API, never its write path, except through the domain's own service layer (e.g., only the `power` module writes `PowerConnection`, even though `equipment` module code may need to read power-path data for a detail view).

---

## 44. Failure / Resilience Architecture

| Condition | Behavior |
|---|---|
| Database outage | API returns `503` for writes; reads may serve Redis-cached data explicitly marked stale; Celery tasks retry with backoff rather than crash-looping |
| Redis outage | No data loss — the durable record is the committed `OutboxEvent`/DB row; async side-effects (notifications, recalculation) are delayed until Redis/dispatcher recovers, never lost (§22) |
| Worker outage | Queued tasks wait in Redis/Outbox until a worker returns (Celery ack-late semantics) |
| Integration/device outage | Isolated per-device circuit breaker (v1.0, preserved) — never affects other devices |
| Central Collector outage | Polling for its assigned Integrations pauses; the platform's own alarm engine fires an "integration stale" alarm on itself |
| Edge Collector outage / WAN outage | Degrades to delayed telemetry, not lost telemetry, up to the local buffer's retention window (a concrete size/duration value flagged for ops approval, §53) — §19 |
| BMS outage | Treated as any device outage: a connectivity signal, never conflated with "equipment failure" (unchanged principle from v1.0) |
| Telemetry backlog | Ingested in `occurred_at` order where possible; aggregation watermark (§16) ensures backlog lands in the correct historical bucket |
| Duplicate telemetry/events | Deduped via `dedup_key` (§16/§21/§25) |
| Failed notification | Retried per policy; recorded `failed` after max attempts — never silently dropped |
| Failed floor-plan import | Job `status=failed` with full `FloorPlanImportDiagnostics` (§11) explaining why; the import pipeline never commits partial/corrupt spatial state — nothing is written past the review-queue stage until confirmed |

---

## 45. Backup / Disaster Recovery

RPO/RTO are **explicitly not assumed** — flagged as requiring management-approved targets before Phase 1's infrastructure setup finalizes (§53). Mechanisms available regardless of the specific numbers chosen:

- PostgreSQL: nightly `pg_dump` + continuous WAL archiving (point-in-time recovery to any moment, bounded by whatever RPO is approved).
- Attachments/floor-plan source files: backed up on a matching schedule, referenced by `storage_path`, never stored as DB blobs.
- Configuration backup: deployment config in version-controlled infra-as-code; secrets excluded and backed up via the secret manager's own mechanism (§31).
- Secret recovery: a documented runbook — not yet written, flagged as a Phase 1 deliverable.
- Restore testing: a periodic scheduled restore-to-scratch-environment drill, cadence TBD pending an approved RTO.
- DR testing: a full failover drill, cadence TBD.
- Backup monitoring: a scheduled job verifies the latest backup exists and is restorable/checksum-valid, alerting on a miss (ties into §37).

---

## 46. Architectural Decision Records

### Preserved from v1.0 (still valid, no defect found)

| # | Decision | Rationale | Status |
|---|---|---|---|
| AD1 | Discrete relational location tables + `LocationType` registry, not a generic polymorphic tree | Query integrity/performance at 10k+ rack scale; frontend still hierarchy-agnostic via the registry | Preserved |
| AD2 | `RackModel`/`EquipmentModel` revisioned; instances pin a specific revision | Prevents catalog edits from corrupting installed assets | Preserved, reinforced by §7's temporal model |
| AD3 | Typed edge tables for topology instead of a rigid FK chain | Real A/B/redundant topologies aren't a strict chain | Preserved, **strengthened** — v1.1's `PowerNode` (AD10) makes the edges FK-real, closing v1.0's referential-integrity gap |
| AD4 | Telemetry schema designed for low-friction TimescaleDB adoption; native Postgres partitioning acceptable as a permanent mechanism | Avoids an infra dependency before it's validated | Preserved, wording corrected (§39) |
| AD5 | Celery + Redis for background work | Per-device failure isolation, retry/backoff at target scale | Preserved |
| AD6 | JWT access+refresh, backend-enforced RBAC | API-first design for web + future clients | Preserved, hardened (§31) |

### New in v1.1

**AD7 — Digital Object Identity.**
*Context:* v1.0 had no single stable ID cross-cutting domains could reference with real integrity.
*Decision:* Introduce `ManagedAsset` as shared-PK identity root for Rack/Equipment/PDU/UPS/Generator/PowerPanel/Sensor.
*Rationale:* Real FK integrity for Spatial/Power/Audit/Capacity references; one place to hold external-system correlation IDs.
*Alternatives:* A single giant `Asset` table (rejected, §48-equivalent below); no shared identity at all, keep per-domain typed pairs (rejected — this is the defect being fixed).
*Consequences:* Every concrete asset table's PK is now also an FK to `ManagedAsset`; migrations must maintain this invariant for any new asset type added later.

**AD8 — Equipment Placement.**
*Context:* v1.0 forced `rack_id` on every equipment row.
*Decision:* `EquipmentPlacement` as the sole, temporal, placement-type-aware placement record.
*Rationale:* Floor/wall/ceiling-mounted equipment is real and common (CRAC units, wall patch enclosures); modeling it as a degenerate rack case would be worse than a dedicated table.
*Alternatives:* Nullable `rack_id` with a separate "floor placement" side table (rejected — two authorities for one concept); keep `rack_id` and add a `placement_type` discriminator on `Equipment` itself (rejected — doesn't give temporal history for free).
*Consequences:* Every placement change is an insert, not an update — slightly more storage, but placement history is free and the rack elevation query is one join away.

**AD9 — Spatial Authority.**
*Context:* `Rack.room_id` and `SpatialObject` could diverge in v1.0.
*Decision:* `RackPlacement` (and `EquipmentPlacement`) as the sole owner of both room and coordinates, with a DB trigger enforcing spatial/room consistency.
*Rationale:* A structural fix (can't diverge because it's one row) beats a policed one (an application check that might be skipped by a future code path).
*Alternatives:* Keep `Rack.room_id` and add an application-level consistency check on every write (rejected — relies on every future write path remembering to check).
*Consequences:* `Rack`/`Equipment` tables lose their own location columns; every query for "current location" goes through the placement view, requiring that view to be well-indexed (done, §7/§8).

**AD10 — PowerNode Topology.**
*Context:* `PowerConnection`'s typed-pair endpoints had no real FK.
*Decision:* `PowerNode` as a real join target for every power-graph participant.
*Rationale:* Referential integrity for a graph that operators will trust for redundancy/capacity decisions — an orphaned edge here is an operationally dangerous silent failure.
*Alternatives:* A graph database (rejected — no measured need at target scale, adds an operational dependency); keeping the untyped pair with only application-level checks (rejected — same category of defect as AD9).
*Consequences:* Every power-graph participant (including sub-components like `PDUOutlet`) needs a `PowerNode` row created alongside it — a service-layer discipline, not automatic, so this must be enforced in the `power` module's creation logic.

**AD11 — Telemetry Identity.**
*Context:* `(id, ts)` didn't express multi-source semantics or occurred/received timing.
*Decision:* `dedup_key`, `occurred_at`/`received_at` split, `source_precedence`-driven current-value resolution.
*Rationale:* Multiple sources reporting the same logical metric is a real DCIM scenario (BMS + local sensor + manual override) and must not silently collide or overwrite.
*Alternatives:* One row per metric, last-write-wins (rejected — destroys the ability to compare sources or audit discrepancies).
*Consequences:* `telemetry_current_value` becomes a view with real logic (precedence + staleness), not a trivial "latest row" query — must be kept performant as data grows (indexed appropriately, §16).

**AD12 — Collector / Edge Architecture.**
*Context:* No explicit runtime-executor identity; edge deployment would have required a redesign.
*Decision:* `Collector` as an addressable entity from Phase 1 (one central instance), with `CollectorAssignment`/`Heartbeat`/`Capability` from day one.
*Rationale:* Making the abstraction real now, even with a single instance, avoids a breaking schema change when edge collectors are actually built.
*Alternatives:* Add the Collector concept only when edge deployment is actually scheduled (rejected — the `occurred_at`/`received_at` split and buffered-forward semantics it depends on are much cheaper to design in from the start than retrofit).
*Consequences:* Slightly more schema in Phase 1 than strictly needed for a single-collector deployment; judged worth it given edge readiness is an explicit requirement.

**AD13 — Domain Events + Outbox.**
*Context:* No transactional guarantee that a mutation's side-effects (notification, recalculation) wouldn't be lost on a crash between commit and dispatch.
*Decision:* Outbox pattern — write the event in the same transaction as the mutation; a separate dispatcher delivers it at-least-once to idempotent handlers.
*Rationale:* Standard, well-understood pattern for exactly this guarantee without introducing a message broker beyond what's already in the stack (Redis/Celery does the transport).
*Alternatives:* Publish directly from application code after commit, best-effort (rejected — a crash between commit and publish silently loses the side-effect); a full event-sourced architecture (rejected — overengineering for this platform's actual needs, §56).
*Consequences:* A dispatcher process/task must exist and be monitored (§37); handlers must be idempotent (§21) since delivery is at-least-once, not exactly-once.

**AD14 — Alarm Hysteresis.**
*Context:* A single threshold flaps when a value oscillates near it.
*Decision:* Independent trigger/clear conditions and durations per `AlarmRule`.
*Rationale:* Standard industrial-monitoring practice; explicitly requested and operationally necessary.
*Alternatives:* A single threshold with a fixed deadband percentage (rejected — less expressive, doesn't fit every metric's real-world behavior).
*Consequences:* Rule authoring UI is slightly more complex (two conditions instead of one) — judged worth the reduced alarm noise.

**AD15 — Network Topology.**
*Context:* Generic `Cable`/`Connection` couldn't distinguish physical from logical network relationships.
*Decision:* `EquipmentInterface` + `PortConnection` (physical) + `NetworkSegment`/`InterfaceSegmentMembership` (logical), replacing the fully-generic `Connection` table for network's case.
*Rationale:* Physical and logical network facts change independently (a VLAN reassignment doesn't move a cable) and need independent history.
*Alternatives:* Model VLANs as equipment custom attributes (rejected — loses relational query power, e.g. "which interfaces are on VLAN 100" becomes a JSONB scan).
*Consequences:* `Connection` (v1.0) is retired for network use; any v1.0-style generic connection data would need migrating to the typed tables — moot since nothing is implemented yet.

**AD16 — Configuration vs. Discovered State.**
*Context:* No structural barrier prevented discovery/polling from silently overwriting authoritative inventory.
*Decision:* `DiscoveredDevice`/`ReconciliationDiff` as a fully separate layer; authoritative tables are only ever updated through the normal, audited mutation path.
*Rationale:* An unattended discovery process must never be able to corrupt inventory that an operator relies on for physical work orders.
*Alternatives:* Auto-update inventory fields when discovery disagrees, log a warning (rejected — exactly the "blind overwrite" the review explicitly prohibits).
*Consequences:* A reconciliation UI/workflow is a real Phase 8+ deliverable, not just a data model — flagged in the phase plan (§49).

**AD17 — Thermal Profile.**
*Context:* Thermal attributes risked being bolted onto `EquipmentModelRevision` piecemeal.
*Decision:* Separate `ThermalProfile`/`ThermalZone` tables, optionally attached.
*Rationale:* Keeps the core equipment schema stable as thermal/CFD requirements evolve independently and later.
*Alternatives:* Add thermal columns directly to `EquipmentModelRevision` (rejected — couples an evolving, speculative feature area to a table that must stay stable for §7/§29's placement/versioning guarantees).
*Consequences:* A thermal-aware query joins an extra table; negligible cost given thermal data is optional and Phase-13+-scoped.

**AD18 — Coordinate System.**
*Context:* No explicit origin/axis/unit/precision decision existed to keep every importer and both renderers consistent.
*Decision:* Integer millimeters, room-local top-left origin, Y-down, degrees-clockwise rotation, as specified in §9.
*Rationale:* Matches the SVG/raster convention directly (fewest sign-flip bugs on import), and integer mm avoids float drift across repeated transforms.
*Alternatives:* Floating-point meters (rejected — drift risk across many small transforms at 10k-rack scale); a georeferenced (GPS) canonical system (rejected for now — no stated multi-campus geo requirement; flagged as open, §53).
*Consequences:* Every importer must convert into this exact convention at parse time — a shared, tested conversion utility is required, not an implementation detail left to each importer.

**AD19 — Importer Abstraction.**
*Context:* VSDX parsing risked becoming the architectural foundation for floor-plan import.
*Decision:* `FloorPlanImporter` protocol with per-format implementations, registered generically.
*Rationale:* New formats (a future CAD format, a different vendor's export) are additive; the spatial domain never depends on any one format's quirks.
*Alternatives:* A single "universal" parser attempting to handle every format (rejected — format quirks would leak into shared code, exactly what the abstraction prevents).
*Consequences:* Each importer must independently solve calibration/coordinate-transform into the shared canonical system (§9/AD18) — some duplication across importers is accepted as the cost of isolation.

**AD20 — Site-Scoped RBAC.**
*Context:* v1.0 left this as an open question with a reserved column.
*Decision:* Generalize the reservation (`RoleAssignment.scope_type/scope_id`, supporting global/site/building) but explicitly defer *enforcement* until a stated trigger condition (two operationally distinct teams sharing one deployment) is met.
*Rationale:* Building enforcement speculatively, before a real need exists, adds complexity (query-filtering middleware, scope-aware caching) without a validated requirement.
*Alternatives:* Build full enforcement in Phase 1 (rejected — no current requirement justifies the cost); ignore scoping entirely (rejected — v1.0 already flagged it as likely needed, and retrofitting query-level scoping later is more expensive than reserving the column now).
*Consequences:* If the trigger condition is met earlier than expected, enforcement work is scoped and estimable (additive middleware) rather than a schema surprise.

---

## 47. Phase Sequencing

The reordering proposed for review is adopted, with the stated rationale for each change from v1.0's sequence:

| Phase | Scope | Change from v1.0 |
|---|---|---|
| 0 | Architecture Gate — **current phase** | — |
| 1 | Foundation: repo, Docker Compose, DB + Alembic, auth/RBAC skeleton, logging, CI | — |
| 2 | Location + Inventory: Organization→Room, `ManagedAsset`, Rack, Equipment, search/filter | `ManagedAsset` identity introduced here, not bolted on later |
| 3 | Rack Models: `RackModel`/`RackModelRevision`, dimensions, visualization | — |
| 4 | Equipment + Rack Elevation: `EquipmentModel`/Revision, `EquipmentPlacement`, computed elevation | `EquipmentPlacement` replaces the old rack_id/u_position columns from the start |
| 5 | Spatial: `FloorPlan`, `SpatialObject`, `RackPlacement`, calibration, grid/snap | `RackPlacement` (unified room+coordinates) replaces split `Rack.room_id` + `SpatialObject` |
| 6 | Floor Plan Import: importer abstraction, review queue, diagnostics | — |
| 7 | **Power** (moved earlier — was Phase 10 in v1.0): `PowerNode`, `PowerConnection`, `PowerCapacity` | Telemetry/Alarms in later phases need something to attach readings to meaningfully; Power now precedes them |
| 8 | Integrations + Collectors: adapter/driver/vendor-profile framework, ICMP/SNMP/REST, `Collector` identity | Collector concept folded in from the start (§18), not retrofitted |
| 9 | Telemetry: metrics, readings, historical storage, polling wire-up, TimescaleDB evaluation | Timescale wording corrected (§39) — evaluated, not assumed |
| 10 | Alarm + Event: rules with hysteresis, state machine, notifications, maintenance windows | Hysteresis (§23) and notification framework (§24) are in-scope for this phase, not deferred |
| 11 | **3D Digital Twin** (moved later — was Phase 7 in v1.0) | A compelling twin needs real alarm/status overlays and power data to show — building it before Power/Alarm exist would mean re-visiting it once they do |
| 12 | Capacity: expanded domains (§33), `CapacitySnapshot` | — |
| 13 | Reporting + Advanced Operations: reports, exports, QR/barcode, advanced search, heat-map layers | — |
| 14 | AI Readiness: query ergonomics on the completed API surface | — |

**Phase gates**, applied uniformly to every phase above (not restated per-phase to keep this table usable):

- **Entry criteria:** the prior phase's exit gate has been reviewed and approved; no phase begins on an assumption that a later phase will retroactively fix a skipped dependency.
- **Deliverables:** the specific tables/endpoints/UI listed in the phase's scope, plus that phase's tests (§Testing, unchanged from v1.0 — every phase ships its own tests, never deferred).
- **Acceptance criteria:** the phase's slice of the §64 (original master prompt) end-to-end workflow is demonstrable against real (seeded) data, not mocked.
- **Architecture constraints:** nothing in the phase may violate §12 (Single Source of Truth) or introduce a second representation of a fact this document already assigns an owner (§42).
- **Exit gate:** a phase completion report per the original master prprompt's §72 format, explicitly listing what was deferred — "no hidden scope."

---

## 48. Architecture Consistency Check

| Question | Answer |
|---|---|
| Can every major physical asset be uniquely identified? | Yes — `ManagedAsset.asset_tag` (human-facing) + `ManagedAsset.id` (system-internal), §4 |
| Can an asset's physical location be reconstructed historically? | Yes — `RackPlacement`/`EquipmentPlacement` are temporal (`effective_from/to`), §7/§8/§29 |
| Can 2D and 3D use the same spatial truth? | Yes — both read `SpatialObject` exclusively, §8/§40/§41 |
| Can rack elevation be derived from inventory? | Yes — computed from `equipment_current_placement`, never stored, §7 |
| Can non-rack equipment exist? | Yes — `EquipmentPlacement.placement_type` covers floor/wall/ceiling/other, §7 |
| Can an end-to-end power path be traversed with FK-safe topology? | Yes — `PowerNode`/`PowerConnection` are real FKs, §13 |
| Can physical and logical network relationships be represented? | Yes — `PortConnection` (physical) vs. `InterfaceSegmentMembership` (logical), §15 |
| Can different protocols/vendors/devices be supported without changing core domain tables? | Yes — Protocol→Driver→VendorProfile→DeviceProfile→MetricMapping, §20 |
| Can multiple sources report the same metric? | Yes — distinct `TelemetryReading` rows per source, precedence resolved at read time, §16 |
| Can alarm trigger and clear conditions differ? | Yes — independent `trigger_*`/`clear_*` fields, §23 |
| Can external events be correlated and deduplicated? | Yes — `correlation_id`/`causation_id`/`dedup_key`, §25 |
| Can the system answer "what was true at time T"? | Yes — `effective_from/to` on every temporal table, §29 |
| Can the system answer "who changed this and why"? | Yes — `AuditLog` with `reason`, synchronous with the mutation, §30 |
| Can new import file formats be added without redesigning spatial? | Yes — `FloorPlanImporter` protocol, additive registration, §10 |
| Can 10k racks be represented without loading 10k heavy meshes? | Yes — instancing + LOD + room/building-scoped lazy loading, §41 |
| Can access be scoped to sites? | Architecturally yes (`RoleAssignment.scope_type/scope_id`); enforcement deferred to a stated trigger condition, §32 |
| What happens when a site loses WAN connectivity? | Edge collector buffers locally and forwards backlog with true `occurred_at` preserved on reconnect; central degrades to delayed, not lost, telemetry, §19/§44 |
| Can an AI layer consume the platform without scraping the UI? | Yes — versioned OpenAPI across every domain listed in §42, unchanged principle from v1.0 |

Every answer above traces to a specific mechanism defined earlier in this document — none is asserted without a corresponding section.

---

## 49. Remaining Open Decisions

These are organizational/operational decisions this document does not make on the user's behalf. Each needs an explicit owner and answer before the phase listed.

| Decision | Options | Recommended direction | Decision owner | Required by phase | Status |
|---|---|---|---|---|---|
| Single vs. multi-tenant | Single-tenant / multi-org-in-one-DB | Single-tenant (§32) | Product owner | Phase 2 | Open |
| Site-scoped RBAC enforcement timing | Build now / defer to trigger condition | Defer (§32) | Product owner | Whenever the trigger condition is met | Open (direction set) |
| Edge collector deployment | Build in Phase 8 / defer entirely | Defer past Phase 13 unless a site with real WAN constraints is identified | Ops/Infra | Phase 8 planning | Open |
| Priority device vendors/models for SNMP | (needs a concrete list) | — | DCIM operations | Phase 8 | Open |
| TimescaleDB availability in target hosting | Can install / cannot | Confirm before committing Phase 9 to it (§39) | Infra/DBA | Phase 9 | Open |
| Required Postgres extensions available | `btree_gist`, `pgcrypto`/`uuid-ossp` | Confirm | Infra/DBA | Phase 1 | Open |
| Asset-tag/barcode numbering convention | Match existing / DCIM-owned generation | — | DCIM operations | Phase 2 | Open |
| RPO/RTO targets | (needs concrete values) | — | Management/Ops | Phase 1 (infra setup) | Open |
| VSDX support level | Native `.vsdx` parsing / "export to SVG/PDF first" workaround | Workaround acceptable unless native parsing is a hard requirement | Product owner | Phase 6 | Open |
| CAD/DXF support level | Full DXF entity coverage / basic geometry only | Basic geometry + text labels first | Product owner | Phase 6 | Open |
| Notification providers | Email/SMS/Teams/WhatsApp/webhook/ServiceNow — which are real integrations vs. framework-only | — | Product owner | Phase 10 | Open |
| Network topology scope for v1 | Full port-level modeling / location+capacity only, network deferred | Recommend starting with `EquipmentInterface`+`PortConnection` for physical cabling only; defer `NetworkSegment`/VLAN modeling until a concrete use case | Product owner | Phase 7 (or later) | Open |
| Thermal/CFD integration timing | Data model only (now) / simulation integration (later) | Data model now, simulation integration explicitly out of scope until requested | Product owner | Not required before Phase 13 | Open |
| Object storage for attachments/floor-plan files | Local volume / S3-compatible | S3-compatible recommended for backup/DR simplicity (§45) | Infra | Phase 1 | Open |
| Deployment environment | Cloud (which provider) / on-prem | — | Infra | Phase 1 | Open |
| KMS/secret-manager adoption | Env vars only / Vault or cloud KMS | Env vars for Phase 1–13, revisit if compliance requires more | Security/Infra | Not required before Phase 1 | Open (direction set) |

---

## 50. Risks (carried forward and updated)

1. Floor-plan import fidelity (Visio/PDF) remains inherently limited — confidence-based review mitigates, doesn't eliminate, false positives/negatives. Unchanged from v1.0.
2. SNMP OID/vendor coverage grows incrementally — the Protocol/Driver/VendorProfile/DeviceProfile layering (§20) makes this additive work, not reduces the amount of work needed.
3. TimescaleDB availability is unconfirmed (§39, §49) — native partitioning is now explicitly documented as an acceptable permanent fallback, reducing this risk's severity from v1.0.
4. 3D performance at 10,000+ racks needs a real load test with synthetic data before Phase 11 is called done — unchanged concern, phase moved later (§47) so this validation happens with real power/alarm data present.
5. `PowerNode` creation discipline (AD10) is a service-layer responsibility, not automatic — a code-review/testing risk if a future contributor adds a power-graph participant without also creating its `PowerNode` row.
6. Two required Postgres extensions (`btree_gist`, and a UUID-generation extension) need confirming in the target hosting environment (§49) — same category of risk as v1.0's single-extension flag, now covering both the U-range constraint and `ManagedAsset`/UUID identity strategy.
7. The Outbox dispatcher (§22) is a new single point that, if not itself monitored (§37), could silently back up — flagged as a required Observability surface, not optional.

---

**Architecture status: READY FOR ARCHITECTURE APPROVAL**

This document (with its companion `ARCHITECTURE_REVISION_REPORT.md`) is the complete v1.1 Phase 0 deliverable. No Phase 1 implementation — repository scaffolding, database migrations, backend/frontend code, authentication, integrations, 2D/3D engines — begins until this revision is explicitly approved or further revised.
