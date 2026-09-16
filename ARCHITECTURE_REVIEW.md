# ARCHITECTURE_REVIEW.md

**Project:** In-House DCIM Platform
**Phase:** 0 — Architecture (pre-implementation)
**Status:** READY FOR REVIEW
**Date:** 2026-09-16

---

## 1. Executive Architecture Summary

The platform is a relational, API-first system of record for physical data-center infrastructure. PostgreSQL holds every fact about location, rack, equipment, power topology, spatial placement, and telemetry. A FastAPI backend exposes that data through a versioned REST API and a WebSocket channel for live updates. React renders the dashboard, rack elevations, 2D floor plans, and a Three.js 3D twin — all reading the same API, so the same rack looks the same everywhere. Background workers (Celery + Redis), not the request/response cycle, run polling, alarm evaluation, and imports.

Three decisions carry the most weight and get their own sections below: rack/equipment models are versioned so editing a catalog entry never mutates an installed asset (§9, AD2); every spatial view (2D, 3D, elevation) reads one `SpatialObject`/placement record, never a duplicate (§67.3, AD1); and integrations are pluggable adapters behind one interface, so SNMP, SSH, REST, and future protocols never touch inventory code directly (§67.2, AD5).

Nothing here is implemented yet. This document is the Phase 0 deliverable required before Phase 1 (Foundation) begins.

---

## 2. Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | React 18 + TypeScript + Vite | Component-based, strict TS |
| Styling | Tailwind CSS | Utility-first, enterprise density (§57) |
| Frontend state | TanStack Query (server state) + Zustand (UI/local state) | No Redux boilerplate; server cache lives in Query |
| 2D spatial | SVG for small scenes, Canvas2D (via `konva`) once object count grows | Decided per floor-plan size at render time |
| 3D | Three.js + `@react-three/fiber` | Instanced meshes for rack/equipment repetition |
| Backend | Python 3.12 + FastAPI + Pydantic v2 | Async-first |
| ORM | SQLAlchemy 2.0 (async) + Alembic migrations | |
| Database | PostgreSQL 16 | System of record |
| Time-series | Native Postgres now, TimescaleDB extension enabled in Phase 9 | Schema designed hypertable-ready from day one (§53, §22) |
| Cache/broker | Redis | Celery broker + API response caching |
| Background jobs | Celery (workers) + Celery Beat (scheduler) | Polling, alarm eval, imports, reports |
| Auth | JWT (access + rotating refresh), Argon2 password hashing | Backend-enforced RBAC (§39) |
| Containers | Docker + Docker Compose (dev), same images for prod | §4 |
| Logging | `structlog` (backend), JSON output | Ships to any log aggregator |

**Alternative considered and rejected:** Node/NestJS backend — Python was chosen for stronger SNMP/Modbus/BACnet library ecosystem (pysnmp, pymodbus, BAC0) relevant to §16–§21.

---

## 3. System Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│  React SPA (Vite build, served by Nginx in prod)             │
│  Dashboard · Rack Elevation · 2D Plan · 3D Twin · Admin       │
└───────────────────────┬───────────────────────────────────────┘
                         │ REST (JSON) + WebSocket
┌───────────────────────▼───────────────────────────────────────┐
│  FastAPI application (stateless, horizontally scalable)       │
│  ┌───────────────┬───────────────┬───────────────┬──────────┐│
│  │ Location/Org  │ Rack/Equipment│ Spatial 2D/3D │ Power    ││
│  ├───────────────┼───────────────┼───────────────┼──────────┤│
│  │ Integration    │ Telemetry API │ Alarm/Event   │ Capacity ││
│  ├───────────────┼───────────────┼───────────────┼──────────┤│
│  │ Auth/RBAC      │ Audit         │ Reporting     │ Import/  ││
│  │                │               │               │ Export   ││
│  └───────────────┴───────────────┴───────────────┴──────────┘│
└──────────┬───────────────────────────────────────┬────────────┘
           │ SQLAlchemy (async)                     │ enqueue
┌──────────▼──────────────┐              ┌──────────▼────────────┐
│ PostgreSQL 16            │              │ Redis                 │
│ (+ TimescaleDB, Phase 9) │              │ broker + cache         │
└──────────────────────────┘              └──────────┬────────────┘
                                                       │
                                           ┌───────────▼────────────┐
                                           │ Celery Workers          │
                                           │  Poller (SNMP/ICMP/…)   │
                                           │  Alarm Evaluator        │
                                           │  Import Processor       │
                                           │  Report Generator       │
                                           └───────────┬────────────┘
                                                        │ adapters
                                           ┌────────────▼────────────┐
                                           │ Real devices/EMS/BMS    │
                                           │ SNMP · SSH · REST · SFTP│
                                           └──────────────────────────┘
```

Every worker talks to the database through the same SQLAlchemy models the API uses — there is one data-access layer, not two.

---

## 4. Component Architecture

Backend is a modular monolith (single deployable, internally partitioned by domain module) for Phase 1–13. Each module owns its SQLAlchemy models, Pydantic schemas, service layer, and router:

```text
backend/
  app/
    modules/
      location/       # Organization..Room
      rack/            # RackModel, RackModelRevision, Rack, placement history
      equipment/        # EquipmentModel, EquipmentModelRevision, Equipment
      power/            # PDU, PowerPanel, PowerCircuit, UPS, Generator, PowerConnection
      spatial/          # FloorPlan, SpatialObject, SpatialLayer, import pipeline
      integration/      # Integration, adapters, credentials, poll logs
      telemetry/        # TelemetryMetric, TelemetryReading, aggregates
      alarm/            # AlarmRule, Alarm, Event
      capacity/         # capacity calculators, snapshots
      reporting/        # report generators, exports
      auth/             # User, Role, Permission, JWT
      audit/             # AuditLog middleware + service
    core/               # config, db session, security, logging
    api/v1/             # routers aggregated here, versioned
  workers/              # Celery tasks, grouped by module
  migrations/           # Alembic
```

A module is split into its own service (e.g., `integration`) only if operational scaling requires independent deployment — not before. This keeps Phase 1–5 simple while leaving a clean seam for later extraction.

---

## 5. Domain Model — Rack/Equipment Lifecycle (§67.1, §67.3)

```text
RackModel (catalog family, e.g. "APC NetShelter SX 42U")
   │
   ├── RackModelRevision v1  ─┐  immutable once referenced
   ├── RackModelRevision v2  ─┤  by any Rack
   └── RackModelRevision v3  ─┘
          │
          │ Rack.model_revision_id (FK, never null after creation)
          ▼
Rack (physical instance, "RACK-A014")
   │
   ├── current placement (room_id, x, y, z, rotation) ── SpatialObject
   ├── placement history (append-only, closed intervals)
   ├── equipment (via Equipment.rack_id + u_position)
   ├── PDUs mounted in it
   └── derived rack elevation (computed, not stored)
```

Editing a `RackModel` never touches existing `Rack` rows. Editing creates a **new** `RackModelRevision`; existing racks keep pointing at their original revision until an operator explicitly re-points them (an audited action). New racks default to the latest revision. This directly satisfies §67.1: "changes to a model must not corrupt installed physical racks." `EquipmentModel`/`EquipmentModelRevision`/`Equipment` follow the identical pattern.

Rack elevation (§11) is **never stored as an independent drawing**. It is computed at request time from `Equipment` rows filtered by `rack_id`, ordered by `u_position`, joined against the rack's `RackModelRevision.total_u`. This makes it structurally impossible for the elevation to drift from inventory.

---

## 6. Database ERD

Notation: `PK` primary key, `FK→Table` foreign key, `UQ` unique constraint, `IDX` indexed, `JSONB` flexible attributes per §52 (used only where variability is genuinely open-ended).

### 6.1 Identity & Access

```text
User            PK id, UQ email, password_hash, full_name, is_active, created_at
Role            PK id, UQ name  (Administrator, DCIM Manager, Engineer, Operator, Viewer, + custom)
Permission      PK id, UQ (resource, action)   e.g. ("rack", "delete"), ("alarm", "acknowledge")
RolePermission  PK (role_id FK→Role, permission_id FK→Permission)
UserRole        PK (user_id FK→User, role_id FK→Role)
AuditLog        PK id, IDX(object_type, object_id), IDX(created_at), user_id FK→User NULL,
                action, object_type, object_id, before JSONB, after JSONB, ip_address, session_id, created_at
```

RBAC is enforced as a FastAPI dependency (`require_permission("rack:delete")`) on every mutating route — never only hidden in the frontend (§39). Phase 1 ships global (non-site-scoped) roles; site-scoped role assignment is an open question (§18).

### 6.2 Organization & Location Hierarchy (§7, §67 discussion in AD1)

```text
Organization    PK id, UQ name
Country         PK id, FK→Organization, name, iso_code, IDX(organization_id)
City            PK id, FK→Country, name, IDX(country_id)
Site            PK id, FK→City, UQ code (e.g. "DXB-DC01"), name, address, latitude, longitude, timezone
Building        PK id, FK→Site, name, code, IDX(site_id)
Floor           PK id, FK→Building, name, level_number, IDX(building_id)
Room            PK id, FK→Floor, name, code, room_type (data_hall/electrical/mechanical/office),
                area_sqm, raised_floor, IDX(floor_id)
LocationType    PK id, UQ code (org/country/city/site/building/floor/room), label, icon, sort_order
```

`LocationType` is a small metadata registry, not a polymorphic replacement for the tables above. Each concrete table is still a normal relational entity (needed because Site, Building, Room genuinely have different attributes and different indexes). The registry exists so the **frontend breadcrumb and tree navigation are driven by a config the backend serves** (`GET /api/v1/location-hierarchy/schema`) instead of a hardcoded chain of components — satisfying §3/§7's "do not hard-code the hierarchy into frontend logic" without paying the query-integrity cost of a fully generic entity-attribute-value tree at 10,000+ rack scale. Trade-off recorded in AD1.

Every table in this chain carries `FK NOT NULL` to its parent except `Organization`. Breadcrumb (`UAE / Dubai / DXB-DC01 / Building A / Floor 2 / Data Hall 03 / Rack A014`) is a single recursive query, indexed on each FK.

### 6.3 Rack Catalog & Instance

```text
RackModel               PK id, UQ (manufacturer, model, part_number), category, description
RackModelRevision       PK id, FK→RackModel, revision_number, UQ(rack_model_id, revision_number),
                         height_u, width_mm, depth_mm, weight_kg, weight_capacity_kg,
                         mounting JSONB (front_rail_offset, rear_rail_offset, rail_type),
                         features JSONB (front_door, rear_door, side_panels, roof, base, casters, plinth),
                         pdu_mount_positions JSONB,
                         is_locked BOOL DEFAULT false  -- set true the first time a Rack references it
Rack                     PK id, UQ asset_tag, FK→RackModelRevision NOT NULL, FK→Room NOT NULL,
                         name, barcode, serial_number, status (planned/installed/active/reserved/
                         maintenance/decommissioned/removed), owner, installed_at, commissioned_at,
                         decommissioned_at, notes, custom_attributes JSONB,
                         IDX(room_id), IDX(status), IDX(asset_tag)
RackPlacementHistory     PK id, FK→Rack, room_id, x_mm, y_mm, rotation_deg, valid_from, valid_to NULL,
                         IDX(rack_id, valid_to)   -- current row has valid_to IS NULL
```

### 6.4 Equipment Catalog & Instance

```text
EquipmentModel           PK id, UQ (manufacturer, model, part_number), category
                          (server/switch/router/firewall/storage/appliance/patch_panel/kvm/ups/other)
EquipmentModelRevision    PK id, FK→EquipmentModel, revision_number,
                          height_u, width_mm, depth_mm, weight_kg, power_rating_w, heat_output_btu,
                          orientation (front_only/front_rear), port_config JSONB, is_locked BOOL
Equipment                 PK id, UQ asset_tag, FK→EquipmentModelRevision NOT NULL,
                          rack_id FK→Rack NULL (NULL = in spares inventory, not installed),
                          u_position INT NULL, height_u INT (denormalized from revision at install time
                          for query speed), hostname, serial_number, ip_address INET, mac_address,
                          status, owner, service, environment, installed_at, warranty_expires_at,
                          notes, custom_attributes JSONB,
                          IDX(rack_id, u_position), IDX(ip_address), IDX(hostname), IDX(status),
                          CHECK (u_position IS NULL OR rack_id IS NOT NULL)
EquipmentPlacementHistory PK id, FK→Equipment, rack_id, u_position, valid_from, valid_to NULL,
                          IDX(equipment_id, valid_to)
```

`u_position`/`rack_id` overlap is enforced at the service layer (no two equipment rows may claim overlapping U ranges in the same rack) — a DB exclusion constraint (`EXCLUDE USING gist`) is used for this in Postgres to make it impossible to violate even under concurrent writes.

### 6.5 Power

```text
PDUModel        PK id, UQ (manufacturer, model), outlet_count, switched BOOL, metered BOOL
PDU              PK id, UQ asset_tag, FK→PDUModel, FK→Rack, position (left/right/top/bottom),
                 ip_address INET, protocol (snmp/rest/none), input_voltage, rated_current_a, status
PDUOutlet        PK id, FK→PDU, outlet_number, UQ(pdu_id, outlet_number),
                 equipment_id FK→Equipment NULL, label, state (on/off/unknown)
PowerPanel       PK id, FK→Room, name, capacity_kw
PowerCircuit     PK id, FK→PowerPanel, circuit_number, breaker_rating_a, capacity_kw
UPS              PK id, UQ asset_tag, FK→Room, capacity_kva, runtime_minutes, status
Generator        PK id, UQ asset_tag, FK→Site, capacity_kw, fuel_type, status
PowerConnection   PK id, source_type ENUM(generator/ups/panel/circuit/pdu/pdu_outlet),
                  source_id UUID, target_type ENUM(ups/panel/circuit/pdu/pdu_outlet/equipment),
                  target_id UUID, feed_label (A/B/single), IDX(source_type, source_id),
                  IDX(target_type, target_id)
```

`PowerConnection` is a typed edge table rather than a rigid FK chain (Utility→Generator→UPS→Panel→Circuit→PDU→Outlet→Equipment). Real sites have redundant A/B feeds and non-linear topologies (§14); a fixed chain of FKs can't represent that, while a generic edge table with typed endpoints can, and still supports the "trace the path from equipment to utility" query via a recursive CTE. This is AD3.

### 6.6 Spatial (§67.3 — single source of truth)

```text
FloorPlan        PK id, FK→Room UQ (one active plan per room), background_asset_id FK→Attachment NULL,
                 calibration_scale_mm_per_px, origin_x_px, origin_y_px, units (mm/m), status
SpatialLayer      PK id, FK→FloorPlan, name (racks/walls/containment/sensors/annotations), z_order,
                 visible_by_default BOOL
SpatialObject     PK id, FK→SpatialLayer, object_type ENUM(rack/wall/door/crac/ups/panel/pdu/sensor/
                 cable_tray/containment/custom), object_ref_id UUID NULL (FK to Rack/UPS/etc. when
                 object_type maps to a real entity), x_mm, y_mm, z_mm DEFAULT 0, rotation_deg,
                 width_mm, depth_mm, height_mm, geometry JSONB NULL (for non-rectangular custom shapes),
                 IDX(spatial_layer_id), IDX(object_type, object_ref_id)
```

`SpatialObject.x_mm/y_mm/rotation_deg` **is** the rack's position — there is no second copy of coordinates anywhere. The 2D canvas renders `SpatialObject` rows scaled by `FloorPlan.calibration_scale_mm_per_px`. The 3D scene renders the same rows converted to meters (`/1000`). Moving a rack in either view issues the same API call (`PATCH /api/v1/spatial-objects/{id}`), which updates this one row and appends a `RackPlacementHistory` entry. This answers §31 directly: one authoritative position, two renderers.

```text
FloorPlanImportJob        PK id, FK→FloorPlan, source_format (svg/dxf/pdf/png/jpg/vsdx), status,
                          uploaded_by FK→User, created_at
FloorPlanImportCandidate  PK id, FK→FloorPlanImportJob, extracted_geometry JSONB, candidate_type,
                          confidence NUMERIC(5,2), reviewed_by FK→User NULL, decision
                          (confirmed/rejected/reclassified), resulting_spatial_object_id FK NULL
```

### 6.7 Cabling / Connections

```text
Cable        PK id, UQ cable_id, cable_type (copper/fiber/power), length_m, status
Connection   PK id, FK→Cable NULL, source_type, source_id, target_type, target_id, connection_type
             (network/power), IDX(source_type, source_id), IDX(target_type, target_id)
```

### 6.8 Integration Framework (§67.2)

```text
IntegrationAdapterType   PK id, UQ code (snmp/icmp/ssh/sftp/rest/modbus/bacnet/mqtt/opcua),
                         label, config_schema JSONB (JSON Schema describing required config fields)
Integration              PK id, FK→IntegrationAdapterType, name, target_type (rack/pdu/room/equipment/
                         custom), target_id UUID, polling_interval_s, timeout_s, retry_count, enabled
IntegrationEndpoint       PK id, FK→Integration, host, port, path NULL
IntegrationCredential     PK id, FK→Integration, credential_type (community/username_password/
                         api_key/oauth2/ssh_key), encrypted_payload BYTEA (Fernet, key from env/KMS),
                         never selected by default ORM query (explicit opt-in load only)
MetricMapping             PK id, FK→Integration, source_key (e.g. OID "1.3.x.x.x" or JSON path),
                         metric_id FK→TelemetryMetric, unit, scaling_factor
PollLog                   PK id, FK→Integration, started_at, finished_at, status (success/timeout/
                         auth_failure/error), error_detail, IDX(integration_id, started_at)
```

Adapters implement one interface; see §9 for the interface contract. `MetricMapping` is exactly the "configurable metric/OID mapping system" §17 requires — generic adapter code never contains a vendor OID.

### 6.9 Telemetry (§67.5, hypertable-ready)

```text
TelemetryMetric    PK id, UQ code (e.g. "pdu.input_power"), label, unit, data_type (numeric/boolean/
                   string), category (power/environmental/network/custom)
TelemetryReading    PK (id, ts), object_type, object_id, metric_id FK→TelemetryMetric, ts TIMESTAMPTZ,
                   value NUMERIC, quality ENUM(GOOD/BAD/STALE/UNKNOWN/ESTIMATED/SIMULATED),
                   source (integration_id FK NULL), IDX(object_type, object_id, metric_id, ts DESC)
TelemetryAggregateHourly  same shape, value = avg/min/max, generated by scheduled job or
                         Timescale continuous aggregate once enabled
TelemetryAggregateDaily    same, longer retention
RetentionPolicy    PK id, FK→TelemetryMetric NULL (NULL = default), raw_days, hourly_days, daily_days
Sensor              PK id, UQ asset_tag, FK→Room, sensor_type (temperature/humidity/airflow/
                   pressure/door/leak), FK→SpatialObject NULL
SensorReading        PK (id, ts), FK→Sensor, ts, value, quality
```

`TelemetryReading`'s composite PK `(id, ts)` and the fact that `ts` is the only time column, with no update-in-place semantics (append-only), is exactly what TimescaleDB requires to convert a table to a hypertable with zero schema change (`SELECT create_hypertable('telemetry_reading', 'ts')`). Phase 1–8 run this as a normal, monthly-partitioned Postgres table (`pg_partman` or native declarative partitioning); Phase 9 flips on the Timescale extension. This is AD4 — deferring an infrastructure dependency without designing around it.

### 6.10 Alarms & Events

```text
AlarmRule    PK id, name, metric_id FK→TelemetryMetric, scope_type (object/tag/site/global),
             scope_id UUID NULL, operator (gt/lt/eq/gte/lte/ne), threshold NUMERIC,
             for_duration_s, severity (info/warning/critical), enabled
Alarm         PK id, FK→AlarmRule, object_type, object_id, state (active/acknowledged/cleared/
             suppressed), raised_at, acknowledged_at, acknowledged_by FK→User NULL, cleared_at,
             current_value, IDX(state), IDX(object_type, object_id)
Event         PK id, ts, source (system/user_id FK NULL), object_type, object_id, event_type,
             severity, description, correlation_id NULL, IDX(object_type, object_id), IDX(ts)
```

Every `Alarm` state transition writes an `Event` row — `Event` is the append-only ledger; `Alarm` is the current-state projection. Deleting/decommissioning a Rack or Equipment never deletes its Events (soft-delete strategy, §6.11).

### 6.11 Cross-cutting

```text
Tag            PK id, UQ name, color
TaggedObject   PK (tag_id FK→Tag, object_type, object_id)
CustomField    PK id, object_type, field_key, UQ(object_type, field_key), field_type, label
Attachment     PK id, object_type, object_id, filename, content_type, storage_path, uploaded_by,
              uploaded_at, IDX(object_type, object_id)
```

**Soft-delete strategy:** `Rack`, `Equipment`, `PDU`, `Site`, `Building`, `Floor`, `Room` are never hard-deleted once they have any telemetry, event, or audit history referencing them. `status = 'decommissioned'` / `'removed'` plus `decommissioned_at` marks lifecycle end; the row stays queryable for history. Pure config/lookup rows (`Tag`, `CustomField`, unused `RackModelRevision`) can hard-delete. This is what makes "can historical relationships be preserved?" (§69) true by construction.

---

## 7. API Architecture

- Versioned: all routes under `/api/v1/`.
- Resource-oriented REST: `GET/POST /api/v1/racks`, `GET/PATCH/DELETE /api/v1/racks/{id}`, nested reads where natural (`GET /api/v1/racks/{id}/elevation`, `GET /api/v1/rooms/{id}/floor-plan`).
- Pagination: `limit`/`offset` + `X-Total-Count` header for list endpoints; cursor-based (`ts`+`id`) for `telemetry` endpoints where offset pagination breaks down at scale.
- Filtering: query params map to indexed columns (`?status=active&room_id=...`); complex filters (tag, custom field) via `?filter=` structured query param, documented per-endpoint.
- Errors: `application/problem+json` (RFC 7807) — `{type, title, status, detail, instance}`.
- Auth: `Authorization: Bearer <JWT>`, obtained via `POST /api/v1/auth/login`, refreshed via httpOnly-cookie refresh token.
- Docs: OpenAPI auto-generated by FastAPI at `/api/v1/openapi.json`, human docs at `/api/docs`. This OpenAPI schema doubles as the contract a future AI assistant consumes (§14, §19).
- Real-time: single `WS /api/v1/ws` channel, client subscribes to topics (`rack:{id}`, `alarms`, `site:{id}:telemetry`) after auth handshake; used for live dashboard/alarm/telemetry updates, not for control actions.

Representative endpoint list (illustrative, not exhaustive): `sites`, `buildings`, `floors`, `rooms`, `racks`, `rack-models`, `rack-models/{id}/revisions`, `equipment`, `equipment-models`, `pdus`, `power-connections`, `floor-plans`, `spatial-objects`, `floor-plan-imports`, `integrations`, `telemetry`, `alarms`, `alarm-rules`, `events`, `capacity/racks`, `capacity/power`, `reports/{type}`, `audit-log`, `users`, `roles`.

---

## 8. Authentication / RBAC

```text
Login (email+password)
   → Argon2 verify
   → issue Access JWT (15 min, claims: user_id, roles, permissions)
   → issue Refresh token (7 days, rotating, httpOnly+Secure cookie, stored hashed in DB for revocation)

Every protected route
   → dependency: decode JWT → load permission set (cached per-request)
   → require_permission("resource:action") → 403 if missing
```

Roles ship as seed data: Administrator, DCIM Manager, Engineer, Operator, Viewer — each a set of `Permission` rows (resource × action, e.g. `rack:create`, `alarm:acknowledge`, `integration:configure`, `floor_plan:import`, `user:manage`). Custom roles are supported (`Role` is a normal table, not an enum) — an admin composes new roles from the same permission catalog without code changes. Authorization is checked in the backend on every mutating and every sensitive read route; frontend hiding of unauthorized actions is a UX convenience only, never the enforcement point (§39).

Site-scoped role assignment (e.g., "Operator, but only for Dubai sites") is designed for but not built in Phase 1 — `UserRole` gets an optional `scope_site_id` column reserved for this; see Open Questions §17.

---

## 9. Integration Architecture (§67.2)

```python
class IntegrationAdapter(Protocol):
    async def connect(self, endpoint: IntegrationEndpoint, credential: DecryptedCredential) -> None: ...
    async def poll(self) -> list[RawReading]: ...          # one call per scheduled interval
    async def disconnect(self) -> None: ...
    def normalize(self, raw: list[RawReading], mappings: list[MetricMapping]) -> list[TelemetryReading]: ...
```

- Each protocol (SNMP, ICMP, SSH, SFTP, REST, Modbus, BACnet, MQTT, OPC-UA) implements this Protocol once, registered in an `ADAPTER_REGISTRY: dict[str, type[IntegrationAdapter]]` keyed by `IntegrationAdapterType.code`.
- Celery Beat enqueues one poll task per `Integration` row at its configured interval; each task run is independent — one device's timeout/auth failure cannot block or crash another's task (§48). Per-device state machine: N consecutive failures → mark `Integration.enabled_effective = false` for a backoff window (circuit breaker), logged as an `Event`, auto-retried after backoff.
- Credentials decrypt only inside the worker process handling that specific poll, immediately before `connect()`; never logged, never serialized into `PollLog.error_detail` verbatim (secrets redacted).
- SSH adapter executes only pre-defined command templates from an allowlist tied to the `Integration` config — never arbitrary strings from the web UI (§19).
- File-based (SFTP) integrations land files in a staging area, run through a per-format parser (CSV/JSON/XML) with Pydantic validation before any row reaches the database (§20).
- Not every adapter ships in Phase 8; SNMP, ICMP, and REST are the priority three (device coverage + implementation cost). SSH and SFTP follow. Modbus/BACnet/MQTT/OPC-UA are registry-ready but explicitly `NOT IMPLEMENTED` until a phase after 8 — their config schema and adapter interface exist so adding one is additive, not architectural surgery.

---

## 10. Telemetry Architecture

```text
Adapter.poll() → RawReading[] → normalize() (apply MetricMapping.scaling_factor, unit) →
TelemetryReading rows (quality=GOOD, source=integration_id) → Postgres (partitioned/hypertable) →
  ├── Alarm Evaluator (Celery task, triggered on insert batch) → Alarm/Event
  └── read path: GET /api/v1/telemetry?object=...&metric=...&range=... → chart data
```

- Stale detection: a scheduled task compares `now() - last_reading.ts` against `Integration.polling_interval_s × 3`; if exceeded, the latest served value is annotated `quality: STALE` in API responses (value itself unchanged) rather than silently served as current (§49).
- Historical ranges (1h/6h/24h/7d/30d/90d/custom) select raw vs hourly vs daily aggregate table based on requested span, to keep chart queries fast without the caller knowing about aggregation internals.
- `quality` enum (`GOOD/BAD/STALE/UNKNOWN/ESTIMATED/SIMULATED`) is mandatory on every reading — this is what lets a future CFD/thermal layer distinguish measured from estimated from simulated (§32, §50) without a schema change.
- Retention is driven by `RetentionPolicy` rows, read by a nightly Celery task that drops/rolls up old raw rows — not hardcoded constants (§53).

---

## 11. 2D Spatial Architecture

- Canonical units: millimeters, integer, stored on `SpatialObject`. Calibration (`FloorPlan.calibration_scale_mm_per_px`) converts to on-screen pixels only at render time — never stored as the source value.
- Calibration workflow: user draws a reference line over a known real-world distance (e.g., a 10 m wall) on an imported/background image; system computes `mm_per_px` and stores it once per `FloorPlan`.
- Canvas: SVG for typical room sizes (up to ~500 objects); switches to Canvas2D/Konva rendering + viewport virtualization (only draw objects within the visible/zoomed viewport) above that, per §56.
- Interactions: pan, zoom, grid with configurable spacing, snap-to-grid, rotation in 90° or free increments (rack placement snaps to 90° by default, sensors/annotations don't), multi-select, grouping, per-layer visibility toggle (`SpatialLayer`).
- Every drag/rotate action issues `PATCH /api/v1/spatial-objects/{id}` — optimistic UI update, server is authoritative, history recorded via `RackPlacementHistory`/`EquipmentPlacementHistory` for the linked physical object.

---

## 12. 3D Digital Twin Architecture

- Reads the same `SpatialObject` rows as 2D, converted mm→m.
- Rendering: `@react-three/fiber`, `InstancedMesh` for repeated geometry (rack boxes, equipment boxes) to keep draw calls low at 10,000+ rack scale (§55–§56); level-of-detail — distant racks render as simplified boxes, near racks render with elevation detail.
- Rack/equipment geometry generated procedurally from `RackModelRevision`/`EquipmentModelRevision` dimensions — no per-model 3D asset authoring required for Phase 7; a `model_asset_url` field is reserved on `RackModelRevision` for later custom-mesh support.
- Selecting an object in 3D dispatches the same "open detail panel" action as clicking it in 2D or in a list — one selection handler, three entry points.
- Status color-coding (alarm state, online/offline) is computed client-side from the same `status`/`Alarm` data the dashboard uses, not a separate 3D-only data source.

---

## 13. Floor Plan Import Architecture (§67.4)

```text
Upload → detect format (svg/dxf/pdf/png/jpg/vsdx)
   → format-specific extractor:
        SVG/DXF  → vector paths/shapes + text labels (high-fidelity extraction)
        PDF      → attempt vector extraction (pdfplumber/pymupdf); if the PDF is a raster scan,
                   fall back to "calibrated background image only, no auto-detection"
        PNG/JPG  → calibrated background image only; NOT IMPLEMENTED: automatic shape detection
                   on raster (explicitly out of scope — stated per §60 rather than faked)
        VSDX     → unzip OOXML, parse shape/connector/text XML (python-vsdx or custom parser)
   → geometry normalizer → candidate shapes
   → classifier: rectangle aspect-ratio + size compared against known RackModelRevision footprints
     → FloorPlanImportCandidate{candidate_type, confidence}
   → review queue UI: user confirms / reclassifies / rejects each candidate
   → confirmed candidates → SpatialObject (+ new Rack if candidate_type=rack and user approves
     creating an instance, otherwise linked to an existing Rack by asset tag match)
```

No candidate becomes a database `Rack`/`SpatialObject` without an explicit confirm action — batch "confirm all above 90% confidence" is allowed as a UI convenience, but the default is per-item review (§28, §60: never auto-create hundreds of racks unattended).

---

## 14. Rack Designer Architecture

The Rack Model Designer is a form/canvas UI writing directly to `RackModel`/`RackModelRevision` — no source-code change required to add a new rack type (§8). Physical features with fixed, queryable meaning (height_u, width_mm, depth_mm, weight_kg) are typed columns; features that vary by vendor in shape rather than presence (mounting rail offsets, PDU mount slot geometry) live in the `mounting`/`features`/`pdu_mount_positions` JSONB columns per §52's guidance to reserve JSONB for genuinely variable structure. The designer renders a live preview (front/rear elevation, top view) from the same fields that get saved — the preview is not a separate drawing tool.

---

## 15. Equipment Architecture

Mirrors §14: `EquipmentModel`/`EquipmentModelRevision` designer, typed physical/electrical columns, `port_config` JSONB for port layout. Equipment placement enforces non-overlapping U ranges per rack via a Postgres exclusion constraint (§6.4) — this is a database-level guarantee, not just an API-level check, so it holds even under concurrent imports.

---

## 16. Power Architecture

Covered in §6.5/§6.6/AD3. Power path tracing (equipment → outlet → PDU → circuit → panel → UPS → generator) is a recursive CTE over `PowerConnection`, exposed as `GET /api/v1/equipment/{id}/power-path`. Dual-corded/redundant equipment is modeled by an equipment instance having two `PDUOutlet` assignments with different `feed_label` (A/B) — no schema special-casing needed.

---

## 17. Alarm Architecture

Covered in §6.10. Evaluation runs as a Celery task triggered after each telemetry insert batch (not a fixed-interval sweep, to keep alarm latency close to polling latency) plus a periodic reconciliation sweep (catches `for_duration_s` threshold breaches and clears missed by the event-driven path). State machine: `active → acknowledged → cleared`, or `active/acknowledged → suppressed` during a maintenance window (maintenance windows are a Phase-11 addition, flagged as scope for that phase, not Phase 1).

---

## 18. Reporting Architecture

Reports (rack inventory, equipment inventory, power, alarms, capacity — §45) are read-only query modules against the same tables, parameterized (site/date range/filters), rendered to CSV/JSON always, PDF where a report is genuinely document-shaped (not implemented for every report type in Phase 12 — scoped per report during that phase). No separate reporting datastore in Phase 1–12; a read replica is the scaling answer if report queries start contending with operational traffic (§21 scalability strategy).

---

## 19. AI-Readiness Architecture

No chatbot in Phase 1–13 (§51). What ships instead: a complete, versioned OpenAPI schema covering location/rack/equipment/power/telemetry/alarm/event/capacity/audit reads, with consistent filtering — this is the substrate a future AI assistant queries directly rather than scraping the UI. Phase 14 is scoped to adding any query ergonomics an assistant specifically needs (e.g., a `/api/v1/query` endpoint accepting structured filters across joined resources) — deferred until the platform itself is reliable, per §51's explicit ordering.

---

## 20. Security Architecture

- Passwords: Argon2id.
- Integration credentials: Fernet-encrypted at rest, key from environment/secret manager, never logged, decrypted only in-worker at point of use.
- Transport: TLS terminated at reverse proxy (Nginx/Traefik) in front of the API; internal service-to-service traffic on a private Docker network.
- Input validation: every request body/query param validated by Pydantic before touching the service layer; all DB access through SQLAlchemy parameterized queries (no raw string SQL) — closes SQLi.
- Frontend: no credentials or secrets ever shipped to frontend JS; JWT stored in memory (access) + httpOnly cookie (refresh) — not localStorage, to reduce XSS token theft surface.
- Least privilege: RBAC as §8; database role used by the app has no superuser/DDL grants outside migrations.
- Audit: every create/update/delete on a tracked entity writes an `AuditLog` row (who/when/what/before/after) — §41.
- Secrets: `.env` files git-ignored from commit one; `.env.example` documents required keys with placeholder values only.

---

## 21. Scalability Strategy

Target: 10+ sites, 100+ buildings, 1,000+ rooms, 10,000+ racks, 100,000+ equipment, thousands of telemetry metrics (§55).

- Every FK column is indexed; every list endpoint paginates server-side; no endpoint returns an unbounded result set.
- Telemetry is the highest-volume table — partitioned from day one, hypertable-converted in Phase 9, aggregates pre-computed so chart queries never scan raw data for long ranges.
- API is stateless — horizontal scaling behind a load balancer is a deployment change, not a code change.
- Redis caches hot read paths (dashboard summary counts, capacity rollups) with short TTL, invalidated on relevant writes.
- Background workers scale independently of the API (separate Celery worker pool, scaled by queue depth).
- Frontend: virtualized lists (`@tanstack/react-virtual`) for any table/list that can exceed ~200 rows; spatial canvas virtualization per §11/§56.

---

## 22. Backup / Recovery Strategy

- PostgreSQL: nightly `pg_dump` plus continuous WAL archiving for point-in-time recovery — this is an infra/ops configuration (documented in `DEPLOYMENT.md` in Phase 1), not application code.
- Attachments/imported floor-plan files: stored on a volume/object store backed up on the same schedule as the database, referenced by `Attachment.storage_path` (never stored as DB blobs).
- Recovery drills and specific RPO/RTO targets are an open question pending ops input (§Open Questions).

---

## 23. Testing Strategy

| Layer | Tooling | Scope |
|---|---|---|
| Backend unit | pytest | services, adapters, normalizers, alarm evaluation logic |
| Backend API | pytest + httpx AsyncClient | route-level, auth/RBAC enforcement, pagination/filtering |
| Backend DB | pytest + a disposable test Postgres (via testcontainers) | migrations apply cleanly, constraints (exclusion constraint on U overlap, etc.) actually hold |
| Integration adapters | pytest + mock SNMP agent (`snmpsim`), mock SSH/REST servers | poll/timeout/retry/backoff behavior, no real devices required |
| Frontend components | Vitest + React Testing Library | rack elevation renders correctly from given inventory data, forms validate |
| Frontend spatial | Playwright | drag/drop/rotate/snap on the 2D canvas, calibration flow |
| E2E | Playwright | the full §64 acceptance workflow, city → alarm-on-dashboard |

Every phase (§59, §72) ships with the tests for that phase's functionality — testing is not deferred to a later phase.

---

## 24. Deployment Architecture

```text
docker-compose.yml (dev/staging)
  postgres:16
  redis:7
  backend        (FastAPI, uvicorn)
  celery-worker
  celery-beat
  frontend       (vite dev server in dev / nginx serving built assets in prod-like compose)
  nginx           (reverse proxy, TLS termination in prod)
```

Configuration via environment variables + `.env` (git-ignored), with `.env.example` as the documented template. Alembic migrations run as an explicit step (`alembic upgrade head`) in the deploy sequence, never auto-run silently against production. No developer-workstation-specific paths or assumptions anywhere in the stack (§4).

---

## 25. Risks

1. **Floor-plan import fidelity** (Visio/PDF) is inherently unreliable — confidence-based review mitigates but does not eliminate false positives/negatives. Raster (PNG/JPG) auto-detection is explicitly out of scope rather than faked.
2. **SNMP OID coverage** — the mapping framework is generic, but building out mapping profiles for many vendors is ongoing effort, not a one-time task; Phase 8 ships the framework plus a small number of seed profiles, not universal coverage.
3. **TimescaleDB availability** — if the target Postgres hosting cannot install extensions, Phase 9's hypertable conversion doesn't happen; the fallback (native partitioning, already the Phase 1–8 design) still works but with less mature compression/continuous-aggregate tooling.
4. **3D performance at full target scale** (10,000+ racks) needs instancing/LOD validated with a realistic dataset early, not assumed — recommend a Phase 7 load test with synthetic 10k-rack data before considering Phase 7 done.
5. **RBAC scope granularity** — global-only roles in Phase 1 may prove insufficient sooner than planned if multi-site operators need day-one isolation; the schema reserves for it (`scope_site_id`) but it isn't built.
6. **Exclusion-constraint approach for U-overlap** requires Postgres `btree_gist` extension — confirm it can be enabled in the target environment during Phase 1.

---

## 26. Architectural Decisions (ADR summary)

| # | Decision | Rationale | Reversibility |
|---|---|---|---|
| AD1 | Discrete relational location tables + `LocationType` registry, not a generic polymorphic tree | Query integrity/performance at 10k+ rack scale; frontend still hierarchy-agnostic via the registry | Medium — migrating to fully generic would need data migration |
| AD2 | `RackModel`/`EquipmentModel` revisioned, instances pin a specific revision | Prevents catalog edits from corrupting installed assets (§67.1 hard requirement) | Low-risk, additive |
| AD3 | `PowerConnection` generic typed-edge table instead of a fixed FK chain | Real topologies (A/B feeds) aren't a strict chain | Low-risk |
| AD4 | Telemetry schema hypertable-ready from Phase 1, Timescale extension enabled Phase 9 | Avoids an infra dependency before it's needed, zero-rework path to scale | Low-risk |
| AD5 | Celery + Redis for all background work, not in-request polling or bare cron | Required for §18/§48 (isolation of per-device failure, retry/backoff) at target scale | Medium — swapping brokers later is possible but not free |
| AD6 | JWT access+refresh, backend-enforced RBAC, no server-side session store for the API | API-first design serving web + future mobile/AI clients uniformly | Low-risk |

---

## 27. Alternatives Considered

- **Graph database for spatial/power topology** — rejected. Postgres recursive CTEs over typed edge tables cover the required traversal queries at target scale without adding an operational dependency.
- **Single polymorphic `Asset` table for Rack/Equipment/PDU** — rejected. Their attribute sets diverge enough that a shared table would need excessive nullable columns or JSONB overuse, undermining indexing and the "normalize where appropriate" requirement (§52).
- **GraphQL API** — rejected for v1. REST is simpler to secure, paginate, and cache with the team's stated backend stack; revisit only if frontend query-shaping complexity becomes a measured problem.
- **Document store (Mongo-style) for equipment custom attributes** — rejected in favor of Postgres JSONB columns on otherwise-relational tables, keeping one database technology and one transaction boundary for the whole system.

---

## 28. Open Questions

1. Is this deployment single-tenant (one `Organization` per installation) or must one database serve multiple isolated organizations? Assumed single-tenant for Phase 1 — confirm before Phase 2 schema lock-in.
2. Is site-scoped RBAC (an Operator restricted to specific sites) needed at Phase 1, or can it wait? Schema reserves for it; build timing needs a decision.
3. Which SNMP-capable device vendors/models are the real near-term priority? Needed to seed concrete `MetricMapping` profiles in Phase 8 rather than guessing.
4. Can the target Postgres hosting install extensions (`timescaledb`, `btree_gist`)? Affects AD4's Phase 9 plan and the U-overlap exclusion constraint.
5. Is native `.vsdx` (Visio) parsing a hard requirement, or is "export Visio to SVG/PDF, then import that" an acceptable workaround for v1? Materially changes Phase 6 cost.
6. Does an existing asset-tag/barcode numbering convention need to be matched, or does the DCIM own ID generation from scratch (§44)?
7. What are the actual RPO/RTO targets for backup/recovery (§22)? Needed from ops before that section can move past "documented approach" to a concrete SLA.
8. Confirm the 10,000-rack / 100,000-equipment figure (§55) is the right near-term scale target — it determines whether Celery/Redis/partitioning are needed from Phase 1 or can be introduced later.

---

## 29. Phase-by-Phase Implementation Plan

Unchanged from the master prompt's sequencing (§58); restated here as the plan this architecture supports, with each phase's own review gate per §71/§72 — no phase begins until the prior one is reviewed and approved.

| Phase | Scope |
|---|---|
| 0 | Architecture (this document) — **current phase, awaiting approval** |
| 1 | Foundation: repo structure, Docker Compose, DB + Alembic, auth/RBAC skeleton, logging, CI |
| 2 | Location & inventory CRUD: Organization→Room, Rack, Equipment, search/filter |
| 3 | Rack Model Designer: RackModel/RackModelRevision, dimensions, visualization, rack instance creation |
| 4 | Equipment models + rack elevation (computed view), drag/drop U placement |
| 5 | 2D spatial engine: FloorPlan, SpatialObject, grid/snap/calibration, rack placement |
| 6 | Floor plan import: SVG/image/PDF, Visio best-effort, confidence review queue |
| 7 | 3D digital twin: Three.js scene reading the same SpatialObject data, sync with 2D |
| 8 | Integration engine: adapter framework, ICMP, SNMP, REST (SSH/SFTP as capacity allows) |
| 9 | Telemetry: metrics, readings, historical storage/charts, polling wire-up, TimescaleDB |
| 10 | Power & EMS: PDU/outlets/power paths, UPS, circuits, power visualization |
| 11 | Alarms & events: rules, thresholds, state machine, acknowledgement, maintenance windows |
| 12 | Capacity & reporting: U/power/equipment capacity, report generation, exports |
| 13 | Advanced ops: QR/barcode, advanced search, heat-map/environmental visualization layers |
| 14 | AI readiness: query ergonomics on top of the already-complete API surface |

---

## Appendix A — Architecture Quality Check (§69)

| Check | Answer |
|---|---|
| Can every physical asset be uniquely identified? | Yes — `asset_tag` UNIQUE on Rack/Equipment/PDU |
| Can assets move between rooms/racks? | Yes — updates the current row; `*PlacementHistory` preserves the trail |
| Can historical relationships be preserved? | Yes — soft-delete + placement history + append-only `Event` |
| Can administrators create new rack models without code changes? | Yes — Rack Model Designer writes `RackModel`/`RackModelRevision` |
| Can different rack dimensions coexist? | Yes — dimensions live per-revision, not hardcoded |
| Does rack elevation derive from actual inventory? | Yes — computed from `Equipment` rows at request time, never stored separately |
| Can imported plans be calibrated? | Yes — §13/§11 calibration workflow |
| Can 2D and 3D stay synchronized? | Yes by construction — both read `SpatialObject` |
| Can new integration protocols be added without touching inventory code? | Yes — `IntegrationAdapter` interface + registry |
| Are credentials secure? | Encrypted at rest, decrypted only in-worker, never logged |
| Can failed devices be isolated? | Yes — per-task isolation + circuit breaker, §9/§48 |
| Can millions of telemetry readings be stored and queried efficiently? | Yes — partitioned/hypertable-ready schema + aggregates |
| Can stale data be identified? | Yes — `quality` enum + interval-based staleness check |
| Can alarms be linked to physical assets and traced from dashboard to rack to equipment? | Yes — `Alarm.object_type/object_id` + spatial/elevation views share the same IDs |
| Can an AI assistant query the system through APIs? | Yes, by design — not yet built (Phase 14, intentionally deferred) |
| Can thermal/CFD visualization be added later? | Yes — spatial layer model + quality-tagged telemetry support it without a redesign |

---

**Architecture status: READY FOR REVIEW**

This document is the complete Phase 0 deliverable. Per §70, no Phase 1 work (repository scaffolding, database implementation, backend/frontend code, authentication, integrations, 2D/3D engines) begins until this architecture is explicitly approved or revised.
