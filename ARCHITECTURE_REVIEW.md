# ARCHITECTURE_REVIEW.md

**Project:** In-House DCIM Platform
**Version:** 1.3 (closes the two Phase 1 blockers from the final architecture validation gate)
**Phase:** 0 — Architecture (pre-implementation)
**Status:** PHASE 1 BLOCKERS CLOSED — SEE RE-VALIDATION GATE
**Date:** 2026-09-16

Companion documents: `ARCHITECTURE_REVISION_REPORT.md` (v1.0→v1.1 diff), `ARCHITECTURE_RED_TEAM_REPORT.md` (adversarial findings against v1.1), `ARCHITECTURE_CHANGE_MATRIX.md` (v1.1→v1.2 before/after trace), `ARCHITECTURE_TARGETED_REVISION_REPORT.md` (v1.2 summary), `FINAL_ARCHITECTURE_VALIDATION_REPORT.md` (the gate that found F1 and confirmed H7 open — superseded by its v2 addendum recording this fix).

---

## 0. Document Control

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-09-16 | Initial Phase 0 architecture |
| 1.1 | 2026-09-16 | Revision closing identity, placement, spatial-authority, power-topology, telemetry-identity, collector, event/outbox, and security gaps identified in the v1.1 review pass. |
| 1.2 | 2026-09-16 | Targeted revision resolving red-team findings C1–C5 (asset replacement, U-range exclusion semantics, exactly-one-current-placement enforcement, placement/power concurrency control, floor-plan import security boundary), H1 (complete ManagedAsset subtype matrix), H6 (AuditLog partitioning/retention), H7 (site-scoped RBAC decision status), and M1 (identity-reference naming reconciliation). Scope was strictly limited to these findings — see `ARCHITECTURE_CHANGE_MATRIX.md` for the full before/after trace and the explicit list of findings intentionally left deferred. |
| 1.3 | 2026-09-16 | Closes the two Phase 1 blockers found at the final architecture validation gate: **F1** — `EquipmentPlacement`'s rack-mounted `CHECK` now also requires `side IS NOT NULL`, closing a residual gap where a NULL `side` silently escaped both of §7a's exclusion constraints; **H7** — the architecture owner recorded Option B (§32a): Phase 1 ships with global authorization, site-scoped RBAC enforcement is built additively when the stated trigger condition is met. No other section changed. |

This document supersedes v1.2 in place. §7 (F1's `CHECK` constraint) and §32a (H7's recorded decision), plus their references in §47a and §49, carry v1.3 changes; every other section is unchanged from v1.2. Nothing in this revision reverses ManagedAsset, PowerNode, EquipmentPlacement/RackPlacement, the integration layering, or the Outbox pattern.

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
ManagedAsset   PK id (UUID, IMMUTABLE — never reassigned, never reused across physical objects),
               asset_type ENUM(rack/equipment/pdu/ups/generator/power_panel/sensor/cable/... —
               extensible, see §4b), UQ asset_tag (mutable, audited), serial_number NULL (manufacturer
               serial — common to every physical asset, held here once rather than duplicated per
               subtype table, v1.2), lifecycle_status (planned/installed/active/reserved/maintenance/
               decommissioned/removed), external_ids JSONB, created_at, decommissioned_at,
               replaces_asset_id FK→ManagedAsset NULL (v1.2, §4a),
               UNIQUE (replaces_asset_id) WHERE replaces_asset_id IS NOT NULL (v1.2 — at most one
               new asset may claim to replace a given old one)
```

Concrete tables use **shared-primary-key inheritance**: `Rack.id`, `Equipment.id`, `PDU.id`, `UPS.id`, `Generator.id`, `PowerPanel.id`, `Sensor.id` *are* `ManagedAsset.id` (each is `PK, FK→ManagedAsset`, not a second independent ID). There is exactly one stable identifier per physical asset, and it is the same value in every table that references it. Full column lists for every subtype are in §4b (v1.2 — resolves red-team finding H1: v1.1 discussed this pattern narratively but never re-published complete subtype schemas).

```text
ManagedAsset (id)
   ├── concrete row: exactly one of Rack / Equipment / PDU / UPS / Generator / PowerPanel / Sensor / ...
   ├── EquipmentPlacement.equipment_id / RackPlacement.rack_id  → FK ManagedAsset.id
   ├── PowerNode.managed_asset_id                                → FK ManagedAsset.id (nullable, §13)
   ├── Alarm.object_id, Event.object_id, TelemetryReading.object_id
   │      (when object_class='managed_asset')                    → resolves to ManagedAsset.id (see caveat below)
   ├── AuditLog.entity_id                                          → FK ManagedAsset.id (when entity is an asset)
   └── ExternalReference.object_id (when object_class='managed_asset') → resolves to ManagedAsset.id
```

**Caveat, stated rather than hidden — and reconciled in v1.2 (resolves red-team finding M1):** Telemetry, Alarm, Event, and ExternalReference must also reference non-asset objects — Room, Site, Building, Floor — which are organizational containers, not assets, and are deliberately **not** part of `ManagedAsset` (a Room isn't commissioned/decommissioned as a physical unit the way a rack is). Postgres cannot express a single native foreign key that targets "whichever of these tables" polymorphically. **One pattern, one name, used everywhere this is unavoidable:** `object_class ENUM(managed_asset, room, site, building, floor)` + `object_id UUID`, validated by application code plus a database trigger checking `object_id` exists in the table named by `object_class` before insert. This is the *only* naming used for this pattern in this document — `Alarm`, `Event`, `TelemetryReading`, and `ExternalReference` all use `object_class`/`object_id` exactly as defined in §16/§25, never a column literally named `managed_asset_id` (v1.1's §4 diagram used that name inconsistently with §16/§25's actual table definitions; v1.2 corrects the diagram above to match the one real pattern). Every other cross-cutting reference in this document uses a real FK to `ManagedAsset.id` or a similarly-scoped identity table (`PowerNode`, §13).

**Identifier authority:** `ManagedAsset.asset_tag` is the human-facing authoritative identifier. `ManagedAsset.id` is the internal, immutable stable identifier every table actually joins on and the correct target for barcode/QR encoding (not `asset_tag`, which is mutable and audited — encoding the mutable tag would orphan a printed label the day someone renames the asset). `external_ids` and `discovered_device.external_identifier` (§26) hold IDs from other systems for correlation — they are never authoritative and never overwrite `asset_tag`/`id`/`serial_number`.

### 4a. Asset Replacement (v1.2 — resolves red-team finding C1)

**Step A — Locate:** this gap sits entirely in §4; no other v1.1 section defined asset-replacement behavior.

**Step B — Validate:** *Confirmed.* v1.1 asserted "one physical asset, one `ManagedAsset.id`, for its entire life" but never defined the procedure that keeps that true across a real replacement event. Nothing prevented an implementer from mutating an existing `ManagedAsset`/`Rack` row in place when a physical unit was swapped, which would silently make prior telemetry/alarm/audit history describe two different physical objects under one identity.

**Step C — Correct.** v1.2 distinguishes, explicitly, two operations that were previously conflated:

```text
SAME PHYSICAL ASSET MOVED                         PHYSICAL ASSET REPLACED
Equipment X: U20 → U30                            Equipment X removed from U20
   │                                               Equipment Y installed at U20
   ▼                                                  │
ManagedAsset.id unchanged                              ▼
   +                                                ManagedAsset.id for X: unchanged, retired
new EquipmentPlacement row for X                    NEW ManagedAsset.id created for Y
(§7 — ordinary placement history)                   Y.replaces_asset_id = X.id
                                                     new EquipmentPlacement row for Y
                                                     X's history (telemetry/alarms/audit) stays
                                                     attributed to X, permanently
```

**Asset Replacement domain operation** (`ReplaceAsset(old_asset_id, new_asset_attributes, reason) → new_asset_id`), executed as a **single database transaction**:

1. Validate `old_asset_id`'s `lifecycle_status ∈ {installed, active, maintenance, reserved}` — an already-`removed` asset cannot be "replaced" again (it was already replaced or decommissioned outright; the domain rule rejects this with a clear error rather than allowing an ambiguous chain).
2. Create a new `ManagedAsset` row (new UUID) with `replaces_asset_id = old_asset_id`. The partial unique constraint on `replaces_asset_id` guarantees at most one new asset claims this replacement.
3. Set `old_asset.lifecycle_status = 'removed'`, `old_asset.decommissioned_at = now()`. **The old row's identity, `asset_tag`, `serial_number`, and every historical fact about it are never edited to describe the new asset.**
4. Close the old asset's current placement row (`RackPlacement`/`EquipmentPlacement`, §7/§8 — `effective_to = now()`) via the same locked close-then-open sequence defined for an ordinary move (§7c/C4). If the new asset occupies the same rack/U/room, its placement is opened at that location through the **ordinary placement-open operation** — never by editing the old placement row's `rack_id`/`equipment_id` to point at the new asset. If the new asset is installed elsewhere, it simply receives whatever placement reflects its real location.
5. Any `Alarm` in `active`/`acknowledged` state against the old asset transitions to `cleared` with a system-generated `Event` (`event_type='alarm_cleared_on_asset_retirement'`) — an alarm is never left open against a `removed` asset.
6. `TelemetryReading` and `ExternalReference` rows already pointing at `old_asset_id` are **never modified or reattributed** — they remain the permanent historical record of the old physical object, exactly as §12 (Single Source of Truth) requires.
7. One `AuditLog` entry captures the replacement (`action='asset_replaced'`, `before={old_asset_id, old_status}`, `after={new_asset_id}`, `reason` REQUIRED — added to the guarded-action list in §30) plus one `OutboxEvent` (`AssetReplaced`, payload `{old_asset_id, new_asset_id, location, replaced_at}` — added to the event-type list in §22).

**Step D — Enforcement:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| One `ManagedAsset` id = one physical lifecycle, never reused | `id` is PK, never updated after insert (immutability is a row-level guarantee any PK provides) | `ReplaceAsset` always creates a new row, never updates `asset_type`/`id` of an existing one | Replacement is its own endpoint (`POST /api/v1/managed-assets/{id}/replace`), not a `PATCH` on the existing asset | `AssetReplaced` event + `asset_replaced` audit entry, both required | Given any `ManagedAsset.id`, every `TelemetryReading`/`Event`/`Alarm` row referencing it describes exactly one physical object for its whole history |
| At most one asset claims to replace a given old one | `UNIQUE(replaces_asset_id) WHERE NOT NULL` | — | Transaction fails cleanly (`409`) on a race to replace the same asset twice | Failed attempt is not audited (nothing changed) | A `replaces_asset_id` chain never forks |

**Step E — Boundary check:** no second source of truth is introduced — `replaces_asset_id` is metadata about *succession*, not a mechanism for reattributing history; the reverse "replaced by" lookup (`SELECT id FROM managed_asset WHERE replaces_asset_id = :old_id`) is a query, not a duplicated column.

**Step F — Dependencies updated:** §7/§8 (placement transfer uses the ordinary move operation), §22 (new `AssetReplaced` event type), §30 (replacement added to the guarded-action `reason`-required list), §46 (new AD21).

**Non-impact:** `ManagedAsset`'s shared-PK-inheritance pattern, `asset_type` enum, and every other subtype table are unchanged.

### 4b. ManagedAsset Subtype Matrix (v1.2 — resolves red-team finding H1)

**Step A — Locate:** v1.1 discussed the shared-PK pattern narratively (§4) and referenced `RackModelRevision`/PowerNode/etc. from other sections, but never re-published a complete, authoritative column list for any concrete subtype table after introducing `ManagedAsset`.

**Step B — Validate:** *Confirmed.* An implementer starting Phase 2 from v1.1 alone would need to mentally merge v1.0's now-partially-superseded table definitions with v1.1's structural changes (room_id removed from Rack, rack_id/u_position removed from Equipment, identity fields moved to `ManagedAsset`) — exactly the ambiguity §65 of the red-team gate warns against.

**Step C — Correct.** The rule, stated once: **`ManagedAsset` = identity + lifecycle anchor. A subtype table = domain-specific state only — it never repeats `asset_tag`, `serial_number`, `lifecycle_status`, or `external_ids`.** Placement (location) lives in `RackPlacement`/`EquipmentPlacement` for anything that moves with any regularity, or a plain FK for large fixed installations — never on the subtype table itself.

```text
Rack                PK id FK→ManagedAsset, model_revision_id FK→RackModelRevision NOT NULL,
                    name, barcode, owner NULL, installed_at, commissioned_at, notes,
                    custom_attributes JSONB
                    Placement: RackPlacement (temporal — racks are moved/re-sited)
                    Telemetry: object_class='managed_asset' rows against this id
                    Power: usually a PowerConnection *target* only via mounted PDUs/equipment,
                           not itself a PowerNode unless the rack has its own metered feed
                    Network: none directly (its mounted Equipment/PDU carry EquipmentInterface rows)

Equipment           PK id FK→ManagedAsset, model_revision_id FK→EquipmentModelRevision NOT NULL,
                    hostname NULL, ip_address INET NULL, mac_address NULL, owner NULL, service NULL,
                    environment NULL, installed_at, warranty_expires_at NULL, notes,
                    custom_attributes JSONB
                    Placement: EquipmentPlacement (temporal — §7; placement_type covers rack/floor/
                               wall/ceiling/other)
                    Telemetry: object_class='managed_asset'
                    Power: PowerNode(node_type='equipment_power_input', owning_asset_id=this.id) —
                           one or two nodes (dual-corded)
                    Network: EquipmentInterface.equipment_id = this.id (column name retained from
                             v1.1 for continuity; see §4c naming note)

PDU                 PK id FK→ManagedAsset, pdu_model_id FK→PDUModel NOT NULL, ip_address INET NULL,
                    protocol ENUM(snmp/rest/none), input_voltage NULL, rated_current_a NULL
                    Placement: EquipmentPlacement (v1.2 — a PDU is rack-mounted exactly like
                               Equipment; it uses the same temporal mechanism rather than a bespoke
                               one, so a future "smart PDU" or "rack PDU" subtype needs no new
                               placement design). placement_type is typically 'rack_mounted'.
                    Telemetry: object_class='managed_asset' (input voltage/current/power etc. are
                               telemetry, not columns here — v1.1 §34 capability-driven principle,
                               unchanged)
                    Power: PowerNode(node_type='pdu', managed_asset_id=this.id) — the PDU itself is
                           a power-graph node; see PDUOutlet below for its outlets
                    Network: EquipmentInterface rows only if the PDU has its own management NIC

PDUOutlet           PK id FK→PowerNode UNIQUE, pdu_asset_id FK→ManagedAsset NOT NULL,
                    outlet_number, UQ(pdu_asset_id, outlet_number), label NULL, state
                    ENUM(on/off/unknown)
                    **NOT a ManagedAsset** (H1 explicit question, answered): an outlet has no
                    independent lifecycle — it is never separately installed, decommissioned, or
                    replaced apart from its parent PDU. It is identified by its owning PDU's
                    ManagedAsset.id plus its outlet_number, consistent with v1.1 §13's own
                    reasoning for sub-component PowerNodes.

UPS                 PK id FK→ManagedAsset, capacity_kva, runtime_minutes NULL,
                    room_id FK→Room NOT NULL
                    Placement: **plain FK to Room, not temporal EquipmentPlacement** (v1.2
                    simplification, deliberate: a UPS is a large fixed installation that is
                    relocated only as a major project, not a routine move — forcing it through the
                    close/open temporal machinery designed for frequent moves adds complexity with
                    no operational benefit; if a UPS relocation ever does happen, it is recorded as
                    an audited `room_id` update, not a placement-history row)
                    Power: PowerNode(node_type='ups', managed_asset_id=this.id)

Generator           PK id FK→ManagedAsset, capacity_kw, fuel_type NULL, site_id FK→Site NOT NULL
                    Placement: plain FK to Site (same rationale as UPS — generators are yard/roof
                    fixed installations)
                    Power: PowerNode(node_type='generator', managed_asset_id=this.id)

PowerPanel          PK id FK→ManagedAsset, capacity_kw, room_id FK→Room NOT NULL
                    Placement: plain FK to Room (same rationale as UPS)
                    Power: PowerNode(node_type='power_panel', managed_asset_id=this.id)

Sensor              PK id FK→ManagedAsset, sensor_type ENUM(temperature/humidity/airflow/pressure/
                    door/leak)
                    Placement: EquipmentPlacement (v1.2 — sensors are wall/ceiling-mounted and do
                    get rehung/relocated occasionally; placement_type typically 'wall_mounted' or
                    'ceiling_mounted')
                    Telemetry: object_class='managed_asset' — the sensor itself is the source of
                    the readings it reports (distinct from Collector, which is the *transport*)
```

**Extensibility (the H1 success criterion — "future asset classes without redesigning ManagedAsset"):** adding **CRAC, CRAH, Chiller, ATS, Transformer, Busway, Rack PDU, Smart Cabinet**, or any future physical asset class requires exactly three additive decisions, none touching `ManagedAsset`:
1. Add one `asset_type` enum value.
2. Add one thin subtype table, keyed `PK id FK→ManagedAsset`, holding only that class's domain-specific attributes.
3. Choose temporal placement (`RackPlacement`/`EquipmentPlacement`, for anything moved with any regularity) **or** a plain FK (for fixed installations, per the UPS/Generator/PowerPanel pattern) — and, if it participates in the power or network graph, add a `PowerNode`/`EquipmentInterface` row exactly as any existing subtype does.

No new identity mechanism, no change to `ManagedAsset`'s columns, no change to any cross-cutting domain (Telemetry/Alarm/Event/Audit) is ever required to add a subtype.

### 4c. Naming Note (LOW, tracked — not fixed in this revision)

`EquipmentPlacement.equipment_id` and `EquipmentInterface.equipment_id` retain that column name from v1.1 for continuity even though both now explicitly scope to any placeable/networkable `ManagedAsset` (Equipment, PDU, Sensor), not only the `Equipment` subtype narrowly. Renaming both to `asset_id` would be clearer but is a cosmetic change outside this revision's scope (it fixes no confirmed defect on its own). **Recorded under OUT-OF-SCOPE / FUTURE REVISION** (§ARCHITECTURE_CHANGE_MATRIX.md) rather than done silently.

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
                     occupies_front BOOLEAN GENERATED ALWAYS AS (side IN ('front','both')) STORED (v1.2),
                     occupies_rear  BOOLEAN GENERATED ALWAYS AS (side IN ('rear','both'))  STORED (v1.2),
                     spatial_object_id FK→SpatialObject NULL UNIQUE,
                     rotation_deg NULL, mounting_method TEXT NULL, orientation NULL,
                     version INT NOT NULL DEFAULT 1 (v1.2, §7c),
                     effective_from TIMESTAMPTZ NOT NULL, effective_to TIMESTAMPTZ NULL,
                     CHECK (placement_type <> 'rack_mounted' OR (rack_id IS NOT NULL AND u_range IS NOT NULL AND side IS NOT NULL)) (v1.3 — closes F1),
                     IDX(equipment_id, effective_to), IDX(rack_id, effective_to)
```

**v1.3 correction (finding F1, final validation gate):** the constraint above now also requires `side IS NOT NULL` for every rack-mounted placement. Without this, a row left with `side = NULL` produces `NULL` (not `false`) for both `occupies_front`/`occupies_rear` (§7a's generated columns), and a `NULL` value in a partial exclusion constraint's `WHERE` clause excludes that row from enforcement entirely — silently reopening the exact same-U-range overlap risk the §7a constraints exist to close. Requiring `side` to be one of `front`/`rear`/`both` at the same point the schema already requires `rack_id`/`u_range` closes this without changing §7a's constraints themselves — every rack-mounted row is now guaranteed to fall into at least one of `occupies_front`/`occupies_rear`'s enforced sets.

"Current placement" = `effective_to IS NULL`; a queryable view `equipment_current_placement` (indexed the same way) is what the rack elevation endpoint and floor/wall equipment lists actually read, so callers never hand-roll the `effective_to IS NULL` filter. Moving equipment — rack to rack, or rack to floor-standing — closes the current row (`effective_to = now()`) and opens a new one; this **is** the placement history, so no separate history table is needed (correcting v1.0's split `Equipment` + `EquipmentPlacementHistory` into one temporal table, per the temporal model in §29).

### 7a. U-Range Overlap Semantics (v1.2 — resolves red-team finding C2)

**Step A — Locate:** the single `EXCLUDE ... side WITH <>` constraint shown in v1.1's §7.

**Step B — Validate:** *Confirmed, and worth showing exactly why.* A GiST exclusion constraint rejects a candidate row only when **every** listed operator evaluates true against some existing row. `side WITH <>` therefore rejects a new row only when the two rows' `side` values **differ** — meaning v1.1's constraint blocked the one case it was supposed to allow (front+rear coexisting) and allowed the two cases it was supposed to block (two `front` rows overlapping, or anything overlapping a `both` row). The red-team's own suggested literal SQL was not adopted as-is; the semantics below were derived independently and checked row-by-row against the required truth table.

**Step C — Correct.** Required truth table (unchanged from the request, restated as the specification this constraint must satisfy):

| Side A | Side B | Overlap | Allowed? |
|---|---|---|---|
| front | front | yes | NO |
| front | both | yes | NO |
| rear | rear | yes | NO |
| rear | both | yes | NO |
| front | rear | yes | **YES** |
| both | both | yes | NO |
| any | any | no | YES |

**Database enforcement strategy:** two computed boolean columns (`occupies_front`, `occupies_rear`, shown above — `GENERATED ALWAYS AS ... STORED`, so `side` remains the single, only-writable source of truth and the booleans are derived, indexable projections of it, never a second independent representation) plus **two partial exclusion constraints**, one per occupied plane:

```text
ALTER TABLE equipment_placement ADD CONSTRAINT no_front_overlap
  EXCLUDE USING gist (rack_id WITH =, u_range WITH &&)
  WHERE (placement_type = 'rack_mounted' AND effective_to IS NULL AND occupies_front);

ALTER TABLE equipment_placement ADD CONSTRAINT no_rear_overlap
  EXCLUDE USING gist (rack_id WITH =, u_range WITH &&)
  WHERE (placement_type = 'rack_mounted' AND effective_to IS NULL AND occupies_rear);
```

(Shown for semantic precision — this is architecture documentation, not a migration; the exact DDL is subject to implementation validation.)

**Why this satisfies every row of the truth table:** a partial exclusion constraint's `WHERE` clause determines which rows are even subject to it. `front`/`both` rows have `occupies_front=true` and are compared against each other by `no_front_overlap` — any two with overlapping `u_range` in the same rack conflict (blocks front–front, front–both, both–both). `rear`/`both` rows are compared by `no_rear_overlap` the same way (blocks rear–rear, rear–both). A `front` row (`occupies_rear=false`) is never in `no_rear_overlap`'s constrained set, and a `rear` row (`occupies_front=false`) is never in `no_front_overlap`'s set — so front vs. rear rows are never compared against each other by either constraint, and **legitimately overlap without conflict.** Non-overlapping `u_range`s never trigger either constraint regardless of `side` (the GiST `&&` operator is false). The `WHERE effective_to IS NULL` clause scopes both constraints to current placements only — historical (closed) rows, including a case where a *replaced* asset (§4a) occupied the exact same U range as its replacement, are correctly excluded from the check because they are never both open at once (the replacement operation closes the old row before opening the new one, in the same transaction).

**Cross-rack behavior:** unaffected — the `rack_id WITH =` term means overlap in different racks never conflicts.

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| Two same-side (or `both`) placements never overlap a U range in one rack | Two partial GiST exclusion constraints (above) | Placement service pre-validates and surfaces a friendly error before hitting the DB constraint | `409` on constraint violation, surfaced with the conflicting placement's details | Rejected attempt is not audited (nothing changed); a successful placement is | Truth table above passes for every row, verified by construction (§7a) — **requires PostgreSQL integration test to confirm in practice** |

**Step E — Boundary check:** no second source of truth — `occupies_front`/`occupies_rear` are generated from `side`, never independently set.

**Step F — Dependencies updated:** §7c (concurrency, below) sequences the close-then-open transaction so these constraints are checked against a consistent snapshot; §4a (replacement) relies on this ordering.

### 7b. Exactly-One-Current-Placement (v1.2 — resolves red-team finding C3)

**Step A — Locate:** v1.1's §7/§8 stated "exactly one row per asset has `effective_to IS NULL`" backed only by a non-unique index comment.

**Step B — Validate:** *Confirmed.* An `IDX` enforces nothing; a retried move request, a close/open race, or a bulk-import bug could leave two `EquipmentPlacement`/`RackPlacement` rows for the same asset both "current" — which directly undermines §12 (Single Source of Truth).

**Step C — Correct.** The red-team's own suggestion (`UNIQUE(asset_id) WHERE effective_to IS NULL`) is valid but was evaluated against a stronger alternative and superseded by it: a **range-exclusion constraint over the whole validity interval**, which catches the same "two current rows" defect *and* the broader case of two historical rows for the same asset ever overlapping in time (a bug creating two rows both claiming to cover the same period) — one mechanism, a strictly larger guarantee:

```text
ALTER TABLE equipment_placement ADD CONSTRAINT one_timeline_per_asset
  EXCLUDE USING gist (
    equipment_id WITH =,
    tstzrange(effective_from, effective_to) WITH &&
  );
```

(Postgres's `tstzrange(from, NULL)` produces the unbounded-upper range `[effective_from, ∞)` for a currently-open row, so two attempts to open a second current row for the same asset are range-overlapping by construction and rejected — "at most one current" falls out of the same constraint that also guarantees "no two historical intervals for one asset ever overlap." The equivalent `UNIQUE(...) WHERE effective_to IS NULL` remains a valid, simpler fallback if the range-based form is judged too implicit for the implementation team's comfort; either satisfies C3.) The identical constraint applies to `RackPlacement` keyed on `rack_id`.

**Distinguishing the two invariants the request asks about:**
- **"At most one current placement"** — the constraint above enforces this as a hard DB guarantee, including the zero-current case (an asset in transit, newly created but not yet installed, or fully decommissioned with its placement row closed and nothing reopened — all valid, all zero-current states).
- **"An active asset must have exactly one current placement"** — this is a *cross-table existence* requirement (`ManagedAsset.lifecycle_status='active'` implies a matching current-placement row exists) that Postgres cannot express as a single declarative constraint without deferred triggers of meaningful complexity for what is fundamentally a data-quality question, not a physical-impossibility question. v1.2 enforces this at the **domain layer**: the `lifecycle_status → 'active'` transition is rejected by the service layer unless a current placement already exists, and a scheduled consistency check (reusing the existing Alarm/Event machinery, §17/§23) raises a data-quality alarm for any `active` asset found with zero current placements — visible to operators rather than silently wrong.
- **Future-dated placements are out of scope for v1.1/v1.2**: `effective_from` is always ≤ the transaction's `now()` at insert time — placements are recorded as they happen, not scheduled in advance. This keeps "current" unambiguous (`effective_to IS NULL`, without also needing `effective_from <= now()`). Scheduled/future placements are recorded under OUT-OF-SCOPE / FUTURE REVISION if ever needed.

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| At most one current placement per asset | Range-exclusion constraint (or equivalent partial unique index) on `RackPlacement`/`EquipmentPlacement` | Move/install/retire workflow always closes-before-opening | §7c | Placement-change events | Two concurrently-current rows for one asset are impossible — **requires PostgreSQL integration test** |
| Active asset has exactly one current placement | Not DB-enforceable cleanly; not attempted | Activation blocked without a current placement; scheduled consistency check | — | Data-quality alarm on violation | Violations are surfaced, not silent |

### 7c. Concurrency Control for Placement (v1.2 — resolves red-team finding C4, this part)

**Step A — Locate:** v1.1 §36 scoped `version`/`If-Match` to `Rack`, `Equipment`, `AlarmRule` — none of which hold placement data after §7/§8 moved it into `EquipmentPlacement`/`RackPlacement`.

**Step B — Validate:** *Confirmed.* The tables two concurrent operators actually race on (moving the same asset, per the red-team's Test 28 scenario) had no stated concurrency mechanism at all.

**Step C — Correct — the move/retire transaction:**

```text
BEGIN;
  SELECT * FROM equipment_placement
    WHERE id = :expected_current_placement_id AND effective_to IS NULL
    FOR UPDATE;                                  -- blocks a concurrent mover on the same asset
  -- if zero rows returned (either raced-and-lost, or the client's view was stale):
  --   ABORT → 409, response body includes the actual current placement so the client can refresh
  UPDATE equipment_placement SET effective_to = now() WHERE id = :locked_row_id;
  INSERT INTO equipment_placement (...) VALUES (...);  -- the new current row
COMMIT;
```

This relies on standard, unexotic Postgres behavior: under READ COMMITTED, a `SELECT ... FOR UPDATE` that blocks on a locked row re-evaluates that row's `WHERE` clause against the *post-commit* state once unblocked (`EvalPlanQual`). Since the query's `WHERE` includes `effective_to IS NULL`, a transaction that was waiting behind a just-committed move finds **zero rows**, not the stale row it originally intended to lock — the service layer treats "zero rows" as the conflict signal and returns `409` with the fresh current-placement state, never a silent double-write. This is exactly what makes the 7a/7b DB constraints a backstop rather than the primary defense: concurrency control here produces a clean `409`; without it, a race would instead surface as a raw constraint-violation error.

**Idempotency-Key interaction (§21):** the Idempotency-Key check runs **first**, before the `FOR UPDATE` sequence above — a client retrying an already-succeeded move (e.g., after a network timeout) gets back the original result, not a false `409`. Ordering: Idempotency-Key lookup → placement lock/close/open → response.

**Concurrent placement retirement (two workflows closing the same current placement):** if the *intended end state* is identical for both (both are decommissioning the asset, not moving it to different destinations), a second workflow finding zero rows on retirement is treated as an **idempotent no-op success**, not a `409` — retiring an already-retired asset achieves the caller's intent. A second workflow attempting a *move* (a different destination) does surface `409`, since the caller's intended destination might not match what actually happened.

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| Concurrent moves of the same asset never both "succeed" against different intended destinations | Row lock via `FOR UPDATE`, re-evaluated on unblock | Move/retire workflow as above | `409` with current state on conflict; idempotent no-op on same-intent retire | Only the winning transaction is audited/eventedI | **Requires PostgreSQL integration test** to confirm `EvalPlanQual` behavior under load |

**Step E — Boundary check:** no second source of truth — the lock targets the same row the exclusion constraints protect; nothing is cached or duplicated to make this work.

**Step F — Dependencies:** §4a's replacement operation reuses this exact sequence; §13a (below) applies the analogous pattern to `PowerConnection`.

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
                version INT NOT NULL DEFAULT 1 (v1.2, §7c's concurrency pattern applies identically here),
                effective_from NOT NULL, effective_to NULL,
                EXCLUDE USING gist (rack_id WITH =, tstzrange(effective_from, effective_to) WITH &&)
                  (v1.2 — resolves red-team finding C3; identical mechanism and rationale as §7b's
                  EquipmentPlacement constraint, applied here to guarantee at most one current room/
                  coordinate assignment per rack and no overlapping historical intervals),
                IDX(rack_id, effective_to)
```

Moving/closing/opening a `RackPlacement` row follows the identical locked close-then-open transaction defined in §7c (`SELECT ... FOR UPDATE ... WHERE rack_id = :id AND effective_to IS NULL`, `409` on zero-rows-after-unblock) — resolving red-team finding C4 for racks the same way §7c resolves it for equipment.

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
Upload → Quarantine → Validation → Isolated Parse (§10a) → Sanitized Intermediate Representation (SIR)
   → Coordinate Transformation (§9) → Object Classification (rectangle aspect-ratio/size vs. known
     RackModelRevision footprints, text-label matching against existing asset tags)
   → Candidate Detection → Confidence Scoring → Human Review (FloorPlanImportCandidate queue)
   → Confirmed Spatial Objects (SpatialObject rows) → Inventory Association (new Rack, or linked to an
     existing one by asset-tag match)
```

Adding a new format is additive: implement the three-method interface and register it. Nothing in `spatial` or `rack`/`equipment` changes. No candidate becomes an authoritative `Rack`/`SpatialObject`/`EquipmentPlacement` without an explicit confirm action (batch-confirm above a confidence threshold is a UI convenience, never the default).

### 10a. Untrusted-File Parsing Security Boundary (v1.2 — resolves red-team finding C5)

**Step A — Locate:** v1.1 §10 described format detection, classification, and confidence scoring in detail but never addressed the safety of the parsing step itself.

**Step B — Validate:** *Confirmed.* SVG/DXF/VSDX are XML- or ZIP-based formats with known attack surfaces (XXE, zip/decompression bombs, path traversal, embedded scripts); PDF parsers have their own history of memory-exhaustion and parsing-bug classes. User-uploaded files reaching an unconstrained parser in the same runtime that holds database credentials is a genuine trust-boundary gap, not a theoretical one.

**Step C — Correct.** Every uploaded file is untrusted input, always, regardless of extension or claimed type. The pipeline (updated above) inserts an explicit boundary **before** any geometry the rest of the system can act on exists:

```text
Upload
   ↓  (file lands in a quarantine area — not the working import directory, not scanned into
   ↓   inventory-adjacent storage)
Quarantine
   ↓
Validation  — content-sniffed (magic bytes, not filename/extension) file-type check; reject on
   ↓           mismatch. File-size cap enforced. For ZIP-based containers (VSDX): decompressed-size
   ↓           limit and compression-ratio guard (reject if decompressed:compressed exceeds a
   ↓           configured threshold — the zip-bomb defense) checked before any entry is expanded.
   ↓           Malformed files are rejected here, not best-effort-parsed.
Isolated Parse  — runs in a narrowly-scoped worker/subprocess that:
   ↓               • holds NO PostgreSQL credentials
   ↓               • holds NO general Redis credentials (at most a narrow, write-only progress channel)
   ↓               • has NO route to the device/collector/BMS network
   ↓               • has NO outbound network access beyond reading the quarantined file
   ↓               • has every XML parser configured with external entity resolution and DTD
   ↓                 processing DISABLED (the XXE defense) and external resource references
   ↓                 (e.g. an SVG <image href="..."> to an outside URL) stripped, never fetched
   ↓               • cannot execute embedded content (an SVG <script> tag is stripped and reported
   ↓                 as an unsupported/rejected object, never executed)
   ↓               • is bounded: hard wall-clock timeout, memory limit (cgroup/ulimit), and a cap on
   ↓                 the number of objects/entities parsed before the job aborts as failed
Sanitized Intermediate Representation (SIR)  — the ONLY thing that crosses back out of the isolated
   ↓                                            parse step: a schema-validated, constrained structure
   ↓                                            of normalized shapes/text/coordinates. No raw markup,
   ↓                                            no scripts, no external references survive into it.
   ↓                                            This SIR is what v1.1 already called "Geometry
   ↓                                            Normalization" — v1.2's fix is inserting the trust
   ↓                                            boundary one step earlier than v1.1 implied, so the
   ↓                                            parse itself, not just the classification after it,
   ↓                                            is isolated.
(rest of the v1.1 pipeline, unchanged, operating only on the SIR — never on the raw file again)
```

The importer — at every stage — never directly mutates authoritative DCIM inventory (unchanged v1.1 principle, now explicitly extended one step earlier to cover the parser itself, which also never touches inventory or credentials).

**Also specified, minimally (per the explicit instruction not to over-build this into an enterprise malware platform):**
- **Malware-scan hook:** an optional integration point after quarantine, before parse — framework only, `NOT IMPLEMENTED` against any specific product, consistent with how ExternalReference/ITSM and notification providers are already framework-only elsewhere in this document.
- **Audit trail:** `FloorPlanImportJob`/`FloorPlanImportDiagnostics` (§11, unchanged) now explicitly capture uploader identity, file hash, and which control (if any) rejected the file (size/ratio/timeout/malformed).
- **Cleanup:** quarantined and intermediate files are deleted after job resolution (success or failure), on a short, configurable retention window — no indefinite retention of arbitrary uploaded content.
- **Sensitive data handling:** access to quarantined/source files is scoped by the same `floor_plan:import` permission as the import feature itself.
- **Site/tenant authorization:** import jobs are scoped to the `Room`/`Site` the uploading user has permission for — this inherits whatever RBAC scoping exists (see §32/H7) automatically, since it is just another `room_id`-scoped write, with no special-casing required.

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| Untrusted files never reach inventory/credentials directly | N/A (execution-boundary control, not a DB constraint) | Quarantine → isolated-parse → SIR pipeline; only SIR is used past this point | Import job is async (§36), status polled/subscribed | Import job diagnostics + audit trail | A malformed/malicious file produces a failed job with diagnostics, never a partial write to inventory, never parser code execution outside its resource limits — **requires security testing to validate control effectiveness; architecturally defined, not yet implementation-verified** |

**Step E — Boundary check:** the SIR is not a new source of truth — it is the same "Geometry Normalization" output v1.1 already treated as an intermediate, review-gated artifact; v1.2 only moves the trust boundary to enclose the parser that produces it.

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
                  voltage, rated_current_a, status, version INT NOT NULL DEFAULT 1 (v1.2, §13a),
                  effective_from NOT NULL, effective_to NULL,
                  IDX(source_node_id, effective_to), IDX(target_node_id, effective_to)
```

`PDUOutlet` (`PK id FK→PowerNode UNIQUE, pdu_asset_id FK→ManagedAsset, outlet_number, ...`) is fully specified in §4b alongside the rest of the `ManagedAsset` subtype matrix.

### 13a. Concurrency Control for Power Topology (v1.2 — resolves red-team finding C4, this part)

**Step A — Locate:** v1.1 §36 did not scope `version`/`If-Match` to `PowerConnection`.

**Step B — Validate:** *Confirmed.* Two operators editing the same power connection (Test 11) or one disconnecting while another modifies (Test 11) had no stated conflict-detection mechanism.

**Step C — Correct.** `PowerConnection` is a single-row edit target (unlike placement, it isn't a close-then-open pair for an ordinary edit), so straightforward optimistic concurrency suffices: the API requires `If-Match` against `PowerConnection.version` for `PATCH`; a mismatch is rejected with `409` before any write. For the specific **disconnect** operation (which closes a connection, `effective_to = now()`, rather than editing it) — a concurrent modify-and-disconnect race uses the same `SELECT ... FOR UPDATE` pattern as §7c: the row is locked before either the update or the close, and whichever transaction commits first wins; the second, on unblocking, finds `effective_to` already set (for a disconnect race) or `version` already incremented (for a modify race) and returns `409`, never a silent overwrite. When creating a new `PowerConnection` between two `PowerNode`s, both node rows are locked in a canonical order (lower UUID first) to prevent a deadlock between two transactions connecting the same pair of nodes in opposite order.

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| Concurrent edit/disconnect of one `PowerConnection` never silently overwrites | `version` column | Canonical node-lock ordering on create | `If-Match`/`409`, or `FOR UPDATE`/`409` for disconnect races | Only the winning transaction is audited/evented | **Requires PostgreSQL integration test** |

**Step E — Boundary check:** no new source of truth; `version` is bookkeeping on the existing row.

**Out of scope for this revision (recorded, not fixed):** self-loop prevention (`source_node_id <> target_node_id`) and cycle-detection on the recursive power-path CTE were flagged by the red-team as H4 — **not** in this revision's mandatory scope (C1–C5, H1, H6, H7, M1) and therefore left deferred; see `ARCHITECTURE_CHANGE_MATRIX.md`.

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

Fields marked `sensitive` in a small config (credential payloads, password hashes, encrypted secrets) are **never** captured in `before`/`after` — redacted to `"***REDACTED***"` at the serialization layer before the row is written, not after. `reason` is required by the service layer for specific guarded actions (e.g., re-pointing a `Rack` to a different `RackModelRevision`, §31/original v1.0's AD2; **Asset Replacement, §4a, v1.2**). Append-only is enforced at the database grant level: the application's DB role has no `UPDATE`/`DELETE` privilege on `audit_log` — only `INSERT` and `SELECT` — so even a bug in application code cannot silently rewrite history.

### 30a. AuditLog Partitioning and Retention (v1.2 — resolves red-team finding H6)

**Step A — Locate:** §38's partitioning strategy named only `TelemetryReading` and `Event`; `AuditLog` was omitted despite an identical unbounded-growth profile (every mutation, every domain, at 10,000+ rack / 100,000+ equipment scale).

**Step B — Validate:** *Confirmed*, and a second, related gap: the append-only DB grant that correctly protects `AuditLog` from tampering (above) also means a future *legitimate, approved* retention policy has no stated path to execute — the application role that can't `DELETE` also can't purge old partitions.

**Step C — Correct.**
- **Partitioning:** `AuditLog` is added to §38's monthly declarative range-partitioning strategy, partitioned on `timestamp`, with the same indexing pattern as `Event` (`entity_type, entity_id, timestamp` and `actor_user_id, timestamp`).
- **Growth characteristics:** comparable order-of-magnitude to `Event` over time (one row per mutation across every domain, bursty during bulk imports) — smaller than `TelemetryReading` but equally unbounded without a retention mechanism.
- **Retention duration is a compliance question, not an engineering one** — the actual "keep for N years" number is an **open organizational decision** (§49, new item), not invented here. What v1.2 specifies regardless of the eventual number is the **mechanism**:
  - **Default posture: archive, don't delete.** Given audit data's evidentiary value, partitions older than the approved retention window are detached and exported to cold/object storage (compressed), not dropped outright, unless a specific legal/compliance policy calls for deletion.
  - **A separate, privileged database role** (e.g. `dcim_retention_admin`) — distinct from the application's normal role — is the only credential that can detach/archive/drop old `AuditLog` partitions. This role is used exclusively by a scheduled, tightly-scoped maintenance job (the existing `maintenance` Celery queue, §35) — never by the application's request-handling path, never used interactively by a human directly (a human need is satisfied by triggering the job, not connecting with the privileged role).
  - **The retention job's own actions are themselves logged** — structured application logs (§37) at minimum, recording which partitions were archived/dropped, when, and by which job run — avoiding the circularity of needing to write to the very table whose deletion-restriction is the point.
  - **Backup interaction:** a detached/archived partition remains in backup scope until its export is completed and verified, so there is never a window where data exists in neither the live table nor a completed backup (§45, unchanged, cross-referenced here).

**Step D — Enforcement summary:**

| Invariant | DB enforcement | Domain enforcement | API/concurrency | Audit/event | Acceptance criterion |
|---|---|---|---|---|---|
| AuditLog growth stays operationally bounded | Monthly partitioning (as Event/Telemetry) | Retention policy applied by a scheduled job, not ad hoc | Privileged role scoped to that job only | Retention actions logged (structured logs, not AuditLog itself) | Old partitions are archived/dropped only by the privileged path; the application role never gains destructive privilege |

**Step E — Boundary check:** no second source of truth — archived partitions are the same rows, relocated, not duplicated; application-role privileges are unchanged (still no `UPDATE`/`DELETE`).

**Step F — Dependencies updated:** §38 (partitioning list), §45 (backup interaction note), §49 (new open decision: approved retention duration).

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

### 32a. Site-Scoped RBAC — Decision Status (v1.2 — resolves red-team finding H7)

**Step A — Locate:** §32 above, and Open Decision #2 in §49.

**Step B — Validate:** *Confirmed as an open item, not a hidden defect* — v1.1 already disclosed the deferral; the red-team review's contribution is insisting this be an explicit, dated management decision before Phase 1 begins, not a default reached by silence, since Phase 1 builds the RBAC foundation itself.

**Step C — Record the decision, without making it.** Per the explicit instruction not to select an option on the architecture's own preference, the three options are recorded here exactly as they must be presented to the architecture owner:

| Option | Description | Consequence if chosen |
|---|---|---|
| **A** | Site-scoped RBAC enforcement is required *before* Phase 1 ships | Phase 1's RBAC foundation (§65 of the red-team gate) must include the query-filtering middleware referenced above as in-scope work, not deferred — this extends Phase 1's scope and timeline |
| **B** | Phase 1 intentionally ships with global authorization; site-scoped RBAC is a defined later phase, built when the stated trigger condition (two operationally distinct teams sharing one deployment) is actually met | Phase 1 proceeds as currently scoped in this document; every authenticated user with a global role can read/write every site's data until enforcement is built — an explicitly accepted, temporary condition, not a silent one |
| **C** | Another explicitly documented decision, approved by the architecture owner | To be recorded here once made |

**Decision recorded (v1.3): Option B.** The architecture owner has confirmed Phase 1 intentionally ships with global authorization; site-scoped RBAC is a defined later addition, built when the stated trigger condition (two operationally distinct teams sharing one deployment) is actually met. This is recorded here as an explicit, dated decision — not a default reached by silence, and not this document selecting on its own authority (the decision was made by the architecture owner in response to the options above being presented at the final validation gate).

**Consequences of Option B, made explicit rather than left implicit:**
- Every authenticated user holding a global role can read and write every site's data from Phase 1 onward, until enforcement middleware is built. This is an **accepted, temporary condition**, not an oversight.
- `RoleAssignment.scope_type`/`scope_id` (§32) remain in the schema, unused for enforcement, ready for the additive middleware described above when the trigger condition is met — no schema change is needed at that time, only new query-filtering logic.
- **Finding F6 (final validation gate) does not activate under Option B:** F6 flagged that `TelemetryReading`/`Event`/`Alarm` carry no denormalized site reference, which would matter *only* if Option A were chosen (RBAC-filtered queries at telemetry's target scale would need one). Since Option B was chosen, no denormalized site column is required now. If the trigger condition is ever met and enforcement is built, F6's concern must be re-evaluated at that time — it is not resolved, it is correctly inert under the option actually selected.
- Phase 1 proceeds exactly as scoped in this document — no additional query-filtering middleware, no schema change, no timeline extension.

**Step D — Phase 1 gate consequence:** with the decision now recorded, this is no longer an unconfirmed assumption blocking Phase 1's entry criterion (§47a). The trigger condition itself remains the thing to watch for — the day two operationally distinct teams share one deployment, enforcement work becomes required, scoped and estimable (additive middleware, per AD20, §46) rather than a surprise.

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

- **Optimistic concurrency:** a `version INT` column on frequently-contended entities. **Corrected in v1.2 (resolves red-team finding C4):** v1.1 scoped this to `Rack`, `Equipment`, `AlarmRule` — but placement and topology data live in `RackPlacement`, `EquipmentPlacement`, and `PowerConnection` (§7/§8/§13), which is what concurrent operators actually contend on. The scope is now: `AlarmRule` (simple `If-Match`), `PowerConnection` (`If-Match`, §13a), and `RackPlacement`/`EquipmentPlacement` (locked close-then-open transaction returning `409` on a lost race, §7c — a stronger mechanism than `If-Match` alone, since a "move" is a multi-row operation, not a single-row edit). `Rack`/`Equipment` themselves (the `ManagedAsset` subtype rows) hold no placement/topology data post-v1.1 and have correspondingly little concurrent-edit contention risk; they may still carry a `version` column for their own narrow fields (name, notes) at implementation time, but this is not the mechanism protecting placement or power correctness.
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
- **Partitioning:** `TelemetryReading`, `Event`, **and `AuditLog` (added in v1.2, §30a — resolves red-team finding H6)** are declaratively range-partitioned by month from Phase 1.
- **Concurrency:** optimistic locking (`version`, §36) for `PowerConnection`/`AlarmRule`; `SELECT ... FOR UPDATE` combined with the GiST exclusion constraints (§7a/§7b, corrected in v1.2) on the placement write path — this pairing is deliberate, not redundant: the lock produces a clean `409` for the common concurrent-move case, while the exclusion constraints remain a backstop against any write path that reaches the database without going through the locked service-layer transaction (e.g., a bulk import, §9 v1.0).
- **Required extensions:** `btree_gist` (needed for both the U-range exclusion constraints, §7a, and the `tstzrange`-based placement-timeline exclusion constraints, §7b/§8 — v1.2 uses this extension more heavily than v1.1 did, making its confirmation in the target hosting environment more important, not less), `pgcrypto` or equivalent (UUID generation). Both flagged for confirmation in the target hosting environment (§53/§49).
- **Privileged retention role** (v1.2, §30a): a database role distinct from the application's, scoped only to a scheduled maintenance job, authorized to detach/archive/drop old `AuditLog` (and, per existing v1.1 `RetentionPolicy`, `TelemetryReading`) partitions.

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

### New in v1.2 (targeted revision)

**AD21 — Asset Replacement as an explicit domain operation.**
*Context:* v1.1 asserted permanent, unique physical-asset identity but never defined the procedure for a physical swap.
*Decision:* `ReplaceAsset` transactionally retires the old `ManagedAsset` and creates a new one linked via `replaces_asset_id`, never mutating the old identity.
*Rationale:* The alternative (editing the existing row) would silently corrupt years of historical attribution the first time it happened.
*Alternatives:* Mutate the existing row and rely on `AuditLog` to record the change (rejected — audit records the change but doesn't prevent all *subsequent* telemetry/alarm history from misattributing to the wrong physical object going forward, since nothing else in the system would know the identity's meaning changed mid-stream).
*Consequences:* One new nullable, uniquely-constrained FK column on `ManagedAsset`; one new domain operation and event type; no impact to any other subtype table.

**AD22 — Corrected U-range exclusion via computed occupancy columns + dual partial constraints.**
*Context:* v1.1's single `side WITH <>` exclusion constraint enforced the inverse of the intended semantics.
*Decision:* Two GiST partial exclusion constraints, gated on `GENERATED`-column projections (`occupies_front`/`occupies_rear`) of `side`, verified row-by-row against the required truth table.
*Rationale:* A single constraint expressing "conflict unless sides differ, except both-vs-anything" cannot be written as one exclusion predicate; two predicates, each scoped to one occupied plane, composes correctly and was checked exhaustively rather than assumed.
*Alternatives:* A custom PostgreSQL operator encoding side-compatibility directly (rejected — more powerful than needed, and generated boolean columns are far easier for an implementation team to read, test, and reason about than a custom operator class).
*Consequences:* Two constraints instead of one; `btree_gist` remains the only extension dependency.

**AD23 — Placement/topology concurrency via row-lock-then-reevaluate, not purely optimistic concurrency.**
*Context:* v1.1 scoped `version`/`If-Match` to the wrong tables and, even if rescoped, optimistic concurrency alone doesn't cleanly handle a multi-row close-then-open operation.
*Decision:* `RackPlacement`/`EquipmentPlacement` moves use `SELECT ... FOR UPDATE` on the current-placement row, relying on Postgres's standard `EvalPlanQual` re-check to surface a clean `409` on a lost race; `PowerConnection` (a single-row edit target) uses simpler `version`/`If-Match`.
*Rationale:* Matching the mechanism to the shape of the operation (multi-row temporal transition vs. single-row edit) rather than forcing one pattern everywhere.
*Alternatives:* Serializable isolation level for all placement transactions (rejected — correct but unnecessarily costly given a targeted row lock achieves the same outcome for this specific, well-understood access pattern).
*Consequences:* Two related but distinct concurrency patterns to document and implement correctly; both are standard, well-known Postgres techniques, not novel mechanisms.

**AD24 — Untrusted-file parsing isolated behind a Sanitized Intermediate Representation boundary.**
*Context:* v1.1 had no stated parser security boundary for user-uploaded floor plans.
*Decision:* Parsing runs in a credential-less, network-restricted, resource-bounded worker; only a schema-validated SIR crosses back into the trusted pipeline.
*Rationale:* The existing v1.1 pipeline already had a natural insertion point ("Geometry Normalization"); v1.2 moves the trust boundary one step earlier to enclose the parser itself, rather than inventing a new pipeline stage.
*Alternatives:* Rely on library-level XXE/zip-bomb mitigations alone without process/credential isolation (rejected — defense in depth is warranted given the parser handles arbitrary user uploads and a library-level bug is not defended against by library-level configuration alone).
*Consequences:* Requires a distinct execution environment (subprocess/container) for the parse step at implementation time — an infrastructure/deployment detail, not a domain-model change.

**AD25 — Complete `ManagedAsset` subtype matrix, with placement-mechanism choice made explicit per subtype.**
*Context:* v1.1 introduced the shared-PK pattern but never published complete subtype schemas, and PDU/Sensor placement was left undefined entirely.
*Decision:* §4b's matrix; PDU and Sensor use `EquipmentPlacement` (temporal — they move); UPS/Generator/PowerPanel use a plain FK to `Room`/`Site` (fixed installations, relocated only as major projects).
*Rationale:* Forcing every subtype through the same temporal-placement machinery regardless of how often it actually moves adds complexity without benefit for the fixed-installation cases; the distinction is made explicit rather than left for each future subtype to reinvent inconsistently.
*Alternatives:* Force all subtypes through `EquipmentPlacement` uniformly (rejected — UPS/Generator/PowerPanel relocations are rare, audited, project-level events, not the routine moves the temporal model optimizes for; a plain FK is simpler and equally correct for that access pattern).
*Consequences:* Two placement patterns to document (already done, §4b) instead of one; every future subtype's placement choice is a decision the matrix already shows how to make, not a new design question.

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

### 47a. v1.2 Phase Impact

No phase was reordered. Per finding:

| Finding | Phase 1 | Phase 2 (Location/Inventory) | Phase 5 (Spatial) | Phase 6 (Import) | Phase 7 (Power) | Other phases |
|---|---|---|---|---|---|---|
| C1 (Asset Replacement) | NO PHASE CHANGE | New prerequisite: the `ReplaceAsset` operation and `replaces_asset_id` must exist before Phase 2 is considered complete (it's part of `ManagedAsset`, built in this phase) | — | — | — | — |
| C2 (U-range exclusion) | NO PHASE CHANGE | — | New prerequisite: `EquipmentPlacement`'s corrected dual-constraint mechanism (§7a) must be in place before Phase 5 exit gate, since Phase 4 (Equipment/Elevation) already depends on U-range integrity | — | — | — |
| C3 (Exactly-one-current) | NO PHASE CHANGE | New prerequisite: the range-exclusion constraint (§7b) ships with `RackPlacement`/`EquipmentPlacement` themselves (Phase 2/4), not deferred to Phase 5 | Confirms/depends on the same constraint | — | — | — |
| C4 (Concurrency) | New prerequisite: the `SELECT ... FOR UPDATE` pattern (§7c) and `version` columns are part of Phase 1's API-conventions foundation (§65), not added later | — | — | — | New prerequisite: `PowerConnection.version` ships with Phase 7 | — |
| C5 (Import security boundary) | NO PHASE CHANGE | — | — | New prerequisite: the quarantine/isolated-parse/SIR boundary (§10a) is now part of Phase 6's scope, not an add-on after — Phase 6's exit gate cannot pass without it | — | — |
| H1 (Subtype matrix) | NO PHASE CHANGE | Directly informs Phase 2/3/4's table definitions (Rack/Equipment) and Phase 7's (PDU/UPS/Generator/PowerPanel) — no new work, just removes ambiguity those phases already had to resolve themselves | — | — | Directly informs Phase 7 | — |
| H6 (AuditLog partitioning) | New prerequisite: AuditLog's partitioned schema and the privileged retention role are part of Phase 1's database/audit foundation (§65) | — | — | — | — | — |
| H7 (RBAC decision) | **RESOLVED (v1.3):** Option B recorded (§32a) — Phase 1 proceeds with global authorization as originally scoped; no additional Phase 1 work | — | — | — | — | — |
| M1 (Naming) | NO PHASE CHANGE | NO PHASE CHANGE | NO PHASE CHANGE | NO PHASE CHANGE | NO PHASE CHANGE | Documentation-only, no phase depends on the column name itself |

---

## 47b. Architecture Invariant & Enforcement Matrix (v1.2, new)

Every invariant this revision touches, in one place, each traced to a concrete enforcement mechanism rather than a paragraph of intent:

| Invariant | Source of Truth | DB Enforcement | Domain Enforcement | API/Concurrency | Audit/Event | Acceptance Criterion |
|---|---|---|---|---|---|---|
| ManagedAsset identity: one physical lifecycle, one id, forever | `ManagedAsset` | PK, never updated | Lifecycle-transition rules; subtype creation is centralized | — | `AuditLog` on every lifecycle transition | Every reference to one `ManagedAsset.id`, across every domain, describes one physical object for its entire history |
| Asset replacement: old identity never reused | `ManagedAsset.replaces_asset_id` | `UNIQUE(replaces_asset_id) WHERE NOT NULL`, FK | `ReplaceAsset` transaction (§4a) | Single transaction; dedicated endpoint, not a `PATCH` | `AssetReplaced` event + `asset_replaced` audit (reason required) | Old asset's telemetry/alarm/audit history is never edited or reattributed |
| Current rack placement: at most one, ever | `RackPlacement` | Range-exclusion constraint on `(rack_id, tstzrange(effective_from, effective_to))` (§8) | Close-then-open workflow | `FOR UPDATE` + `409` on lost race (§7c pattern, applied to racks) | Placement-change event per move | Two simultaneously-current `RackPlacement` rows for one rack are impossible |
| Current equipment placement: at most one, ever | `EquipmentPlacement` | Range-exclusion constraint on `(equipment_id, tstzrange(...))` (§7b) | Close-then-open workflow | `FOR UPDATE` + `409` on lost race (§7c) | Placement-change event per move | Two simultaneously-current `EquipmentPlacement` rows for one asset are impossible |
| Active asset has exactly one current placement | `ManagedAsset.lifecycle_status` + `EquipmentPlacement`/`RackPlacement` | Not DB-enforceable cleanly; not attempted (§7b) | Activation blocked without a current placement; scheduled consistency check | — | Data-quality alarm on violation | Violations are surfaced, never silently wrong |
| U-range overlap: truth table (§7a) holds exactly | `EquipmentPlacement.side`, `occupies_front`/`occupies_rear` (generated) | Two partial GiST exclusion constraints (§7a) | Pre-validation before hitting the DB constraint | `409` with conflicting placement detail | Rejected attempts are not audited (nothing changed) | Truth table passes for every row — requires PostgreSQL integration test |
| Placement/topology concurrency: no silent overwrite | `RackPlacement`/`EquipmentPlacement`/`PowerConnection` | Row locks (`FOR UPDATE`) re-evaluated on unblock; `version` on `PowerConnection` | Idempotency-Key check runs before the concurrency check (§7c) | `409` + fresh state, or idempotent no-op for same-intent retirement | Only the winning transaction is audited/evented | Requires PostgreSQL integration test under concurrent load |
| Power topology: real FKs, no dangling edges | `PowerNode`/`PowerConnection` | FK on both ends (unchanged from v1.1) | `PowerNode` created alongside every power-graph participant (service discipline, §46 risk 1, unchanged) | `409` on concurrent edit/disconnect (§13a) | Topology-change event | No `PowerConnection` edge can reference a non-existent `PowerNode` (self-loop/cycle prevention remains H4, explicitly deferred) |
| Import security: untrusted files never reach inventory/credentials directly | Quarantine → isolated parse → SIR pipeline (§10a) | N/A (execution-boundary, not a DB constraint) | Parser worker is credential-less and network-restricted | Import job is async, status polled (§36) | Import diagnostics + audit trail | Architecturally defined; requires security testing to validate control effectiveness |
| AuditLog: bounded growth, tamper-evident, but purgeable by policy | `AuditLog` | Monthly partitioning; app role has no UPDATE/DELETE | Retention policy executed only by a separate privileged role/job (§30a) | — | Retention actions logged (structured logs) | Application code can never purge audit history; an approved retention policy can, through the privileged path only |
| Identity-reference naming: one pattern, one name | `Alarm`/`Event`/`TelemetryReading`/`ExternalReference` | Trigger validates `object_id` against `object_class` (unchanged mechanism, v1.1) | — | — | — | §4's diagram and §16/§25's table definitions now use the identical column names — no reader-facing inconsistency remains |

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
| Site-scoped RBAC enforcement timing | Option A / Option B / Option C — see §32a | **Decided (v1.3): Option B.** Phase 1 ships with global authorization; enforcement is built additively when the trigger condition (two operationally distinct teams sharing one deployment) is met | Architecture owner | Re-evaluate at the trigger condition, not before | **Decided** |
| AuditLog retention duration (v1.2, new) | A specific "keep for N years" figure, per compliance requirement | Mechanism (partitioning + archive-don't-delete + privileged role) is specified in §30a regardless of the number; the number itself is not invented here | Compliance/Legal + architecture owner | Phase 1 (mechanism), ongoing (duration) | Open |
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
8. **(v1.2, new)** The corrected U-range exclusion constraints (§7a), the `tstzrange`-based placement-timeline exclusion constraints (§7b/§8), and the `FOR UPDATE`/`EvalPlanQual` concurrency pattern (§7c/§13a) are architecturally validated by construction (checked row-by-row against the required truth table, and against documented Postgres transaction-isolation semantics) but **not yet validated against a running PostgreSQL instance** — every one of these mechanisms requires a PostgreSQL integration test before Phase 2/5/7's exit gate, not merely a code review.
9. **(v1.2, new)** The floor-plan import security boundary (§10a) is architecturally defined but its actual effectiveness against real malformed/malicious files requires security testing at implementation time — this document specifies the control, not evidence that the control holds under attack.

---

**Architecture status (v1.3): BOTH PHASE 1 BLOCKERS CLOSED — SEE RE-VALIDATION GATE FOR VERDICT**

This document, together with `ARCHITECTURE_CHANGE_MATRIX.md`, `ARCHITECTURE_TARGETED_REVISION_REPORT.md`, and `FINAL_ARCHITECTURE_VALIDATION_REPORT.md` (and its v1.3 addendum), is the complete revision chain. C1–C5, H1, H6, and M1 are resolved (§4a, §4b, §7a, §7b, §7c/§13a, §10a, §30a, §4/§16/§25 reconciliation). F1 (the residual C2 gap found at the final validation gate) is closed in §7. H7 is now a recorded decision, not an open question (§32a: Option B). Whether this is sufficient to authorize Phase 1 is the re-validation gate's call, not this document's own — see `FINAL_ARCHITECTURE_VALIDATION_REPORT.md`'s v1.3 addendum for the actual verdict.
