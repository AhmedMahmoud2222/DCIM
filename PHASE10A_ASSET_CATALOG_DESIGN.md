# Phase 10A — Versioned Administrator Asset Catalog Designer: Design Specification

**Status:** Design and planning only. No product code, migrations, APIs, or UI are implemented by this
document or this branch. Every schema, endpoint, and component described below is a proposal for a
future implementation phase.

## 0. Verification record

| Item | Value |
|---|---|
| Repository | `AhmedMahmoud2222/DCIM` |
| Starting branch | `codex/commercial-ui-uplift-v1` |
| Starting SHA (verified via `git rev-parse`) | `224f122d950ee2631180940b6b8a429f1379b266` |
| PR #9 merge commit present at that SHA | Confirmed — `git log` shows `224f122` as `Merge pull request #9 from AhmedMahmoud2222/codex/implement-network-topology-and-3d-layout`, HEAD of the fetched branch |
| Design branch | `claude/phase10a-asset-catalog-design`, created from `224f122d950ee2631180940b6b8a429f1379b266` exactly (`git checkout -b ... 224f122d...`) |
| Alembic current head (verified by walking `down_revision` chain across all 17 files in `backend/migrations/versions/`) | `0016_network_runtime_defaults` |
| Backend stack | FastAPI (async, Python 3.11) + PostgreSQL 16 + Redis/Celery — modular monolith |
| Frontend stack | React 18 + TypeScript + Vite + Tailwind + TanStack Query v5, no form/dialog library |

---

## 1. What already exists (grounding)

This is not a greenfield feature. A catalog already exists and Phase 10A must extend it, not replace it.

### 1.1 Current catalog domain (`backend/app/domain/catalog/models.py`)

```text
RackModel            PK id, manufacturer, model_name, UNIQUE(manufacturer, model_name)
RackModelRevision     PK id, rack_model_id FK→RackModel RESTRICT, height_u NOT NULL,
                      width_mm NOT NULL, depth_mm NOT NULL, weight_capacity_kg NULL
EquipmentModel        PK id, manufacturer, model_name, UNIQUE(manufacturer, model_name)
EquipmentModelRevision PK id, equipment_model_id FK→EquipmentModel RESTRICT,
                      height_u/width_mm/depth_mm/weight_kg all NULL
```

`Rack.model_revision_id` and `Equipment.model_revision_id` are `NOT NULL, ondelete="RESTRICT"` foreign
keys (`backend/app/domain/physical/models.py:22-24,44-46`) — every installed Rack/Equipment permanently
pins a specific revision. `backend/app/api/v1/catalog.py`'s own module docstring states the binding
invariant this phase must preserve: *"a `*ModelRevision` is immutable once created... there is no update
endpoint for a revision."* That immutability today is convention only (no update/delete endpoint exists),
never a DB-level guard — Phase 10A's explicit requirement to enforce immutability "at application and
database levels" is new work, not a restatement of what already exists.

**Gaps this phase must close, confirmed by direct inspection, not assumed:**

- No lifecycle state at all (no draft/published/retired) — a row is either "doesn't exist yet" or
  "permanent." There is no way to prepare a revision before it is usable, and no way to signal a
  revision should no longer be chosen for new installations.
- No physical properties beyond four columns; no electrical/thermal, PSU, port, graphics, or monitoring
  data anywhere in the schema.
- No `Manufacturer` entity — `manufacturer` is a free-text `String(128)` duplicated on every `*Model` row.
- **No dedicated `catalog:*` permission.** The existing endpoints gate on `rack:manage`/`equipment:manage`
  (`backend/app/api/v1/catalog.py:59,86,156,185`), which `DEFAULT_ROLE_PERMISSIONS`
  (`backend/app/application/rbac.py`) also grants to the **Engineer** role, not just Administrator/DCIM
  Manager. Today, an Engineer can mint new catalog rows. This directly conflicts with product principle
  8 ("Only administrators can create, edit, publish, retire, import, or migrate catalog definitions") and
  is addressed explicitly in §9.
- **No audit or outbox writes on any existing catalog endpoint** — `catalog.py`'s four handlers call
  `db.add()`/`db.commit()` only, never `write_audit_log()`/`write_outbox_event()`, unlike every other
  domain module in `app/application/`. This is a real gap against product principle 10, closed in §8/§10.
- No file/image storage mechanism exists anywhere in the backend (§1.3).
- No JSON import/export mechanism exists anywhere in the backend (§1.4).
- On the frontend, catalog authoring is not a standalone surface at all: `RacksPage.tsx` and
  `EquipmentPage.tsx` each mint a brand-new `*Model`+`*ModelRevision` pair inline on every rack/equipment
  creation (`RacksPage.tsx:32-49`, `EquipmentPage.tsx:34-42`) — there is no picker to reuse an existing
  catalog entry. `RacksPage.tsx:104-106` contains an explicit code comment admitting this gap. Types and
  thin `api.ts` client functions for the four existing endpoints already exist
  (`frontend/src/types/index.ts:40-74`) and are reused, not duplicated, by this design.

### 1.2 Architecture document's own forward-looking notes

`ARCHITECTURE_REVIEW.md` §28 ("Thermal / CFD Readiness") already reserves a `ThermalProfile` table
*"optionally-attached"* to `equipment_model_revision_id`/`rack_model_revision_id` via nullable FKs,
explicitly so thermal data can be "added... without touching the core `EquipmentModelRevision`/
`RackModelRevision` tables at all." This is direct precedent for Phase 10A's own additive-child-table
strategy (§4).

§20 ("Integration Layering") sketches a `Protocol → Driver → VendorProfile → DeviceProfile →
MetricMapping` chain where `DeviceProfile.model_name` is a **plain string**, not an FK into
`EquipmentModelRevision` — a parallel, string-keyed catalog that was never built
(`PHASE8_ARCHITECTURE_CLARIFICATION.md` confirms only `Protocol`/`Driver` exist; `VendorProfile`/
`DeviceProfile`/persistent `MetricMapping` do not). Phase 10A **supersedes that sketch** for the
monitoring-template piece: reusable metric definitions attach directly, by real FK, to the same
`CatalogModelRevision` that already owns physical/electrical data, not to a second string-matched
`DeviceProfile` table. §14 explains why.

AD2 (`ARCHITECTURE_REVIEW.md` §46) — *"`RackModel`/`EquipmentModel` revisioned; instances pin a specific
revision... Prevents catalog edits from corrupting installed assets"* — is the one architectural decision
this entire design exists to extend, never to weaken.

### 1.3 File/image storage: does not exist

The only upload path in the codebase is floor-plan import (`app/api/v1/floor_plans.py`,
`app/application/svg_sanitizer.py`). Critically, **the raw uploaded bytes are never durably stored** —
they are read into memory, sanitized into an abstract shape list (SVG) or magic-byte-validated only
(raster), base64-encoded into a Celery task argument, and discarded once the import job finishes; only
`source_file_name`/`source_file_hash`/`source_format` persist on `FloorPlan`
(`app/domain/floorplan_import/models.py:21-33`, `app/infrastructure/tasks/floorplan_import.py`). There is
no S3/MinIO dependency, no local-disk write path, nothing in `Settings` for an object-store endpoint.
Phase 10A's requirement for "revision-safe asset storage" that can be **re-displayed later** cannot reuse
this pattern as-is — it needs new, genuinely persistent storage (§7).

What **is** directly reusable: `app/application/svg_sanitizer.py`'s content-sniffing, size-cap-before-parse,
and `validate_raster_image()` magic-byte checks (PNG `\x89PNG\r\n\x1a\n` / JPEG `\xff\xd8\xff`), and the
per-endpoint `ApiError(413, ...)` pattern (`floor_plans.py:203`) — there is no global request-size
middleware in this codebase; every upload endpoint checks its own cap.

### 1.4 Import/export: does not exist

No JSON import/export endpoint exists anywhere. The closest structural precedent is the floor-plan
import **job pattern**: `FloorPlanImportJob` (async, status-tracked) → `FloorPlanImportDiagnostics`
(why an import produced partial results, never a bare failure) → `FloorPlanImportCandidate` rows a human
explicitly accepts/rejects, never silently promoted. `ARCHITECTURE_REVIEW.md` §10a states the governing
principle directly: *"No candidate becomes an authoritative... object without an explicit confirm
action."* Phase 10A's JSON import (§13) follows the same **job + preview + explicit-apply** shape, scaled
down from per-shape candidates to per-catalog-item preview rows, since a JSON document is already
structured (no shape-detection/ambiguity step is needed).

### 1.5 RBAC, audit, outbox, and concurrency conventions to follow exactly

- Permission codes are `resource:action`, checked for **exact match** — `manage` never implies `read`
  (`app/application/rbac.py:161-163` comment). Every mutating route depends on
  `Depends(require_permission("code"))`.
- `write_audit_log()` (`app/application/audit_service.py`) and `write_outbox_event()`
  (`app/application/outbox_service.py`) are called **synchronously, in the same transaction, before
  commit** — never deferred to the outbox for the primary record. Representative outbox event types
  already anticipated in `ARCHITECTURE_REVIEW.md` §22 include `RackModelRevisionCreated`; audit action
  naming is `"{resource}.{verb}"` snake_case (`rack.create`, `power.connection.create`, …); outbox event
  naming is PascalCase past-tense (`RackCreated`, `PowerNodeRetired`, …).
- `reason` on `AuditLog` is **required by the service layer for guarded actions** — §30 names *"re-pointing
  a `Rack` to a different `RackModelRevision`"* as its own worked example. Phase 10A's installed-asset
  migration endpoint requires `reason` for exactly this cause.
- Optimistic concurrency is a `version: int` column + `If-Match` header, enforced via
  `app/application/concurrency.py`'s `require_if_match`/`ConflictError` (409) /
  `ApiError(428, "Precondition Required")` pattern, already used by Room/Rack/Equipment/PowerConnection/etc.
- `PowerNode` (`app/domain/power/models.py:145-165`) sets *either* `managed_asset_id` *or*
  `owning_asset_id`, enforced by a CHECK constraint, never both — the established precedent this design
  reuses for every "exactly one of several nullable FKs" case below (§4.5, §4.6).
- Every DB invariant the ORM can't express directly (partition DDL, exclusion constraints, the
  `trg_managed_asset_replacement_acyclic` cycle guard) is a hand-written trigger in raw SQL inside the
  Alembic migration, not application-only enforcement — the established precedent for the
  immutability trigger required in §5.4.

---

## 2. Product principles: how this design honors each one

| # | Principle | How this design satisfies it |
|---|---|---|
| 1 | Model definitions and installed assets are separate concepts | `CatalogModel(Revision)` (new) is never referenced by `Rack`/`Equipment` directly; see the legacy-bridge design in §4.7 |
| 2 | Reusable definitions belong to a manufacturer/model catalog | New `Manufacturer` entity, §4.1 |
| 3 | Installed assets reference a specific published revision | Preserved unchanged — `Rack.model_revision_id`/`Equipment.model_revision_id` keep pointing at legacy revision rows, now populated at publish time from a `CatalogModelRevision` (§4.7) |
| 4 | Published revisions are immutable | DB trigger + application check, §5.4 |
| 5 | Changes require a new draft → publish | §5.1–§5.3 |
| 6 | Installed assets stay pinned until explicit migration | §6, §5.9 |
| 7 | Lifecycle: draft / published / retired | `catalog_model_revision.lifecycle_status`, §4.2 |
| 8 | Only administrators mutate catalog definitions | New `catalog:*` permission family, admin-only; **existing** `rack:manage`/`equipment:manage` catalog-authoring capability is explicitly removed, §9.1 |
| 9 | Read access may be broader | `catalog:read` granted to every existing role that already holds `rack:read`/`equipment:read` |
| 10 | Audit + outbox for material actions | §8, §10 — closes an existing gap (§1.1) |
| 11 | No credentials/secrets in catalog definitions | Nothing in the schema references `Integration`, `credential_ciphertext`, or any secret-bearing table; monitoring templates hold protocol/OID/unit only (§4.6) |
| 12 | Import/export excludes credentials, secrets, and embedded image binaries | §13 — export carries graphic *references* (`storage_key`, `mime_type`, dimensions), never bytes |
| 13 | No topology editing, full 3D, collector execution, or discovery automation | None of those are touched; monitoring templates are inert data until an admin/operator seeds an `IntegrationMetricMapping` at install time (§6.2) — this phase never talks to a collector or driver |

---

## 3. Architectural decision: schema shape

### 3.1 Options considered

**Option A — Extend the existing `RackModelRevision`/`EquipmentModelRevision` tables directly.**
Add every new column (physical unit fields, electrical/thermal, lifecycle status, etc.) plus category-
specific child tables (`rack_network_port_template`, `equipment_network_port_template`, …) onto each of
the two existing tables independently, duplicating the draft/publish/retire machinery, the API surface,
and the admin UI once per category.

**Option B — A unified `CatalogModel`/`CatalogModelRevision` aggregate with typed, category-scoped child
tables, non-breaking toward the existing tables via a bridge.** One identity table, one revision table,
shared lifecycle/versioning/audit/outbox/API/UI code across every category; category-specific detail
(ports, PSUs, monitoring, graphics) lives in child tables that are simply empty for categories that don't
use them. The two legacy tables are kept, physically unchanged, and bridged from the new revision at
publish time (§4.7) so `Rack.model_revision_id`/`Equipment.model_revision_id` never need to change.

**Option C — A single mutable catalog table with a JSONB "spec" blob per category.** Rejected outright:
the prompt's own instruction ("avoidance of an unstructured JSON dumping ground") and product principle 4
(published revisions immutable, which a live-editable JSONB blob cannot express without bolting the exact
same lifecycle/versioning machinery back on anyway) both rule it out. It also loses every DB-level
constraint (positive dimensions, enum membership, FK integrity on port/PSU/monitoring rows) that Options
A and B get for free.

### 3.2 Evaluation

| Criterion | Option A | Option B (chosen) |
|---|---|---|
| Compatibility with existing `RackModelRevision`/`EquipmentModelRevision` FKs | Requires altering both tables' shape under live `Rack`/`Equipment` FKs | Legacy tables are **never altered**; bridged additively (§4.7) |
| Migration complexity/risk to existing data | Two parallel schema migrations touching tables 8 production FK relationships already depend on | One new aggregate (purely additive tables) + two nullable bridge columns on the legacy tables |
| Type safety / DB constraints | Good, but duplicated twice (and again for every future category — network device, PDU, sensor, …) | Good, written once; category-specific requirements enforced by a `category` CHECK plus application-layer publish validation (§5.2), not duplicated schema |
| API clarity | Two near-identical endpoint families now, a third/fourth/fifth later | One endpoint family, `category` is a field/path segment, not a fork in the codebase |
| UI complexity | Two (eventually N) near-identical designer flows to build and keep in sync | One designer flow; category only toggles which optional sections render |
| Future categories (network device, PDU, cooling, sensor, modular equipment) | Combinatorial: full lifecycle/port/PSU/monitoring/graphics machinery re-built per category | Additive: a new `category` CHECK value plus (if needed) a new typed child table; no change to lifecycle/versioning/audit/API shape |
| Avoids unstructured JSON dumping ground | Yes | Yes |

**Decision: Option B.** It is the only option that both avoids duplicating the lifecycle/versioning/audit
machinery per category (a direct requirement: *"future support for network, power, cooling, sensors, and
modular equipment"*) and leaves the existing `Rack`/`Equipment` FK relationships completely untouched,
which is the safer migration by a wide margin given those FKs are `NOT NULL RESTRICT` on live production
tables today.

---

## 4. Domain model

### 4.1 Identity: `Manufacturer`, `CatalogModel`

```text
Manufacturer          PK id, name UNIQUE NOT NULL, status ENUM(active/deprecated) DEFAULT active,
                      created_at/updated_at

CatalogModel           PK id, manufacturer_id FK→Manufacturer RESTRICT, category CHECK IN
                      ('rack','equipment','network_device','pdu','ups','power_panel','sensor'),
                      subtype VARCHAR(64) NULL (free text — "server","switch","blade-chassis"; classification
                      only, never drives schema/behavior),
                      model_name VARCHAR(128) NOT NULL, model_number VARCHAR(128) NULL,
                      description TEXT NULL, tags JSONB NOT NULL DEFAULT '[]',
                      status ENUM(active/deprecated) DEFAULT active,
                      UNIQUE(manufacturer_id, model_name), created_at/updated_at
```

`category` is deliberately an extensible CHECK list, mirroring `ManagedAsset.ASSET_TYPES`'s own precedent
(*"a plain, extensible string column... so a future subtype can be added by inserting a new allowed
value"*). **Phase 10A implements the full designer/lifecycle/legacy-bridge workflow only for `category IN
('rack', 'equipment')`** — the two categories with an existing legacy revision table to bridge to.
Creating a `CatalogModel` with any other `category` value is rejected at the API layer in this phase (a
deliberate, disclosed scope guard, not a partially-working feature) — the schema is shaped so a later
phase can lift that guard for `network_device`/`pdu`/`ups`/`power_panel`/`sensor` by adding their own
`model_revision_id` FK on the corresponding instance table, with zero change to this aggregate.

`CatalogModel.status = 'deprecated'` is an **identity-level** signal ("this whole model line is no longer
recommended for new drafts") distinct from a revision's `lifecycle_status` — it never blocks reading
existing published revisions or installed assets.

### 4.2 `CatalogModelRevision` — the immutable-once-published aggregate root

```text
CatalogModelRevision   PK id, catalog_model_id FK→CatalogModel RESTRICT, revision_number INT NOT NULL,
                       UNIQUE(catalog_model_id, revision_number),
                       lifecycle_status CHECK IN ('draft','published','retired') DEFAULT 'draft',

                       -- Physical (nullable at the DB level; category-appropriate required-ness is an
                       -- application-layer publish-time validation, §5.2, so a draft can be saved
                       -- incrementally without every field present)
                       dimension_unit CHECK IN ('mm','in') NULL,
                       width_value NUMERIC(10,3) NULL, height_value NUMERIC(10,3) NULL,
                       depth_value NUMERIC(10,3) NULL,
                       rack_unit_height INT NULL CHECK (rack_unit_height IS NULL OR rack_unit_height > 0),
                       weight_unit CHECK IN ('kg','lb') NULL, weight_value NUMERIC(10,3) NULL,
                       mounting_orientation VARCHAR(32) NULL,
                       supported_placement_types JSONB NULL   -- subset of EquipmentPlacement's own
                                                               -- placement_type vocabulary: rack_mounted /
                                                               -- floor_standing / wall_mounted /
                                                               -- ceiling_mounted / other — reused, not
                                                               -- redefined, so an installed EquipmentPlacement
                                                               -- can be validated against it later
                       airflow_direction CHECK IN ('front_to_rear','front_to_top','side_to_side','other') NULL,

                       -- Electrical/thermal nameplate summary (per-PSU voltage/frequency/connector detail
                       -- lives on PowerSupplyTemplate, §4.4 — not duplicated here)
                       rated_power_w NUMERIC(10,2) NULL CHECK (rated_power_w IS NULL OR rated_power_w >= 0),
                       typical_power_w NUMERIC(10,2) NULL CHECK (typical_power_w IS NULL OR typical_power_w >= 0),
                       max_power_w NUMERIC(10,2) NULL CHECK (max_power_w IS NULL OR max_power_w >= 0),
                       heat_dissipation_btu_hr NUMERIC(10,2) NULL,
                       power_redundancy_mode CHECK IN ('single','1+1','n+1') NULL,

                       -- Lifecycle bookkeeping
                       cloned_from_revision_id UUID FK→CatalogModelRevision RESTRICT NULL,  -- provenance
                                                                                              -- when authored
                                                                                              -- via "clone", §5.5
                       created_by_user_id FK→User RESTRICT NOT NULL,
                       published_at TIMESTAMPTZ NULL, published_by_user_id FK→User RESTRICT NULL,
                       retired_at TIMESTAMPTZ NULL, retired_by_user_id FK→User RESTRICT NULL,
                       retirement_reason VARCHAR(1000) NULL,
                       allow_installation_when_retired BOOLEAN NOT NULL DEFAULT false,  -- explicit override,
                                                                                          -- §5.6

                       -- Legacy bridge (exactly one populated, matching CatalogModel.category, from the
                       -- moment lifecycle_status first becomes 'published'; both NULL while draft) — §4.7
                       legacy_rack_model_revision_id UUID FK→rack_model_revision RESTRICT UNIQUE NULL,
                       legacy_equipment_model_revision_id UUID FK→equipment_model_revision RESTRICT UNIQUE NULL,

                       version INT NOT NULL DEFAULT 1,   -- optimistic concurrency while status='draft' only;
                                                          -- frozen forever the instant status leaves 'draft'
                       created_at/updated_at
```

Every numeric physical/electrical column is nullable at the database level. This is deliberate, not an
oversight: a draft is explicitly allowed to be incomplete while an admin is filling it in (requirement:
"editing drafts"), and the required-for-*this*-category check is a **publish-time application validation**
(§5.2), not a DB constraint — otherwise an admin could never save partial work. What the DB *does* enforce
unconditionally, regardless of draft/published state: positive dimensions/weights/power values, enum
membership, and the legacy-bridge XOR (below). `CHECK (NOT (legacy_rack_model_revision_id IS NOT NULL AND
legacy_equipment_model_revision_id IS NOT NULL))` — reusing `PowerNode`'s own "exactly one of two nullable
FKs" precedent (§1.5) rather than a polymorphic type+id pair.

### 4.3 `NetworkPortTemplate`

```text
NetworkPortTemplate    PK id, catalog_model_revision_id FK→CatalogModelRevision CASCADE,
                       stable_key VARCHAR(64) NOT NULL, UNIQUE(catalog_model_revision_id, stable_key),
                       display_name VARCHAR(128) NOT NULL, numbering_pattern VARCHAR(64) NULL,  -- e.g. "Gi0/{n}"
                       media_type CHECK IN ('copper','fiber','other') NOT NULL,
                       supported_speeds_mbps JSONB NOT NULL,   -- e.g. [1000, 10000]; ordered, admin-entered
                       connector_type VARCHAR(32) NOT NULL,    -- "rj45","sfp+","qsfp28",...  free text,
                                                                -- validated against a small known-good list
                                                                -- at the API layer, not a DB enum (new
                                                                -- connector types appear faster than a
                                                                -- migration cadence should have to track)
                       role CHECK IN ('uplink','access','management','stack','other') NOT NULL DEFAULT 'other',
                       side CHECK IN ('front','rear') NOT NULL,
                       module_group VARCHAR(64) NULL,          -- e.g. groups 4 ports sharing one SFP cage
                       sort_order INT NOT NULL DEFAULT 0,
                       created_at/updated_at
```

`stable_key` is the durable component identity across revisions required by the versioning section
(§5.10) — an admin assigns it once (e.g. `"eth0"`) and it is expected to be reused verbatim across the
model's future revisions so installed-asset overrides and monitoring seeds can track "the same logical
port" even as a display name or speed changes revision-to-revision. It is scoped to the revision, not
globally unique, since each revision is a fresh row set (§5.10 covers the cross-revision matching rule).

### 4.4 `PowerSupplyTemplate`

Covers both "number and type of power supplies" and "power-connector templates" from the prompt — a PSU
bay and its inlet connector are one physical thing on the equipment, not two.

```text
PowerSupplyTemplate    PK id, catalog_model_revision_id FK→CatalogModelRevision CASCADE,
                       stable_key VARCHAR(64) NOT NULL, UNIQUE(catalog_model_revision_id, stable_key),
                       label VARCHAR(128) NOT NULL,             -- "PSU 1"
                       quantity INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
                       redundancy_mode CHECK IN ('single','1+1','n+1') NOT NULL DEFAULT 'single',
                       connector_type VARCHAR(32) NOT NULL,     -- "C14","C20","NEMA5-15",...
                       rated_voltage_min NUMERIC(6,1) NULL, rated_voltage_max NUMERIC(6,1) NULL,
                       rated_frequency_hz NUMERIC(5,1) NULL,
                       rated_current_a NUMERIC(6,2) NULL,
                       hot_swappable BOOLEAN NULL,
                       sort_order INT NOT NULL DEFAULT 0,
                       created_at/updated_at
```

This is model-catalog metadata only — it never creates a `PowerNode`/`PowerConnection` row itself (those
remain purely installed-asset concepts, §6.2). `CatalogModelRevision.power_redundancy_mode` is the
nameplate-summary value shown on the model overview; a given `PowerSupplyTemplate.redundancy_mode` can
differ per PSU group on complex equipment (e.g. a chassis with independently redundant fan and PSU bays)
— the two are intentionally not the same column reused twice.

### 4.5 Graphics: `CatalogGraphic`, `CatalogGraphicMarker`

```text
CatalogGraphic         PK id, catalog_model_revision_id FK→CatalogModelRevision CASCADE,
                       side CHECK IN ('front','rear') NOT NULL, UNIQUE(catalog_model_revision_id, side),
                       storage_key VARCHAR(128) NOT NULL,   -- content-addressed opaque key, §7 — never a
                                                             -- user-supplied filename
                       original_filename VARCHAR(255) NOT NULL,   -- display-only, never used to derive a path
                       mime_type CHECK IN ('image/png','image/jpeg') NOT NULL,
                       file_size_bytes INT NOT NULL, width_px INT NOT NULL, height_px INT NOT NULL,
                       uploaded_by_user_id FK→User RESTRICT NOT NULL, uploaded_at TIMESTAMPTZ NOT NULL,
                       created_at/updated_at

CatalogGraphicMarker   PK id, catalog_graphic_id FK→CatalogGraphic CASCADE,
                       marker_type CHECK IN ('network_port','power_supply','module','other') NOT NULL,
                       network_port_template_id FK→NetworkPortTemplate CASCADE NULL,
                       power_supply_template_id FK→PowerSupplyTemplate CASCADE NULL,
                       CHECK (
                         (marker_type = 'network_port' AND network_port_template_id IS NOT NULL AND power_supply_template_id IS NULL) OR
                         (marker_type = 'power_supply' AND power_supply_template_id IS NOT NULL AND network_port_template_id IS NULL) OR
                         (marker_type IN ('module','other') AND network_port_template_id IS NULL AND power_supply_template_id IS NULL)
                       ),
                       label VARCHAR(128) NULL,             -- required for module/other markers, since
                                                             -- they have no backing template row to name them
                       marker_x NUMERIC(6,5) NOT NULL CHECK (marker_x BETWEEN 0 AND 1),
                       marker_y NUMERIC(6,5) NOT NULL CHECK (marker_y BETWEEN 0 AND 1),
                       sort_order INT NOT NULL DEFAULT 0,
                       created_at/updated_at
```

Marker position lives **only** here, normalized 0..1 against the graphic's own `width_px`/`height_px` — a
port/PSU template row never stores its own x/y, avoiding two coordinate stores that could drift. A marker
that targets a component removed from a later draft is handled in §5.11.

Non-graphical fallback: every `NetworkPortTemplate`/`PowerSupplyTemplate` is independently readable,
labeled, and fully editable through the structured designer sections (§11.4) with no dependency on a
graphic existing — a model with zero uploaded images is complete and usable, satisfying the "accessible
non-graphical fallback" requirement by construction (markers are an optional annotation layer on top of
already-complete structured data, never the only place a component's identity/spec lives).

### 4.6 Monitoring: `MonitoringMetricTemplate`

```text
MonitoringMetricTemplate  PK id, catalog_model_revision_id FK→CatalogModelRevision CASCADE,
                          stable_key VARCHAR(64) NOT NULL, UNIQUE(catalog_model_revision_id, stable_key),
                          protocol CHECK IN ('snmp','other') NOT NULL,
                          protocol_other_label VARCHAR(64) NULL CHECK (
                            (protocol = 'other' AND protocol_other_label IS NOT NULL) OR
                            (protocol <> 'other' AND protocol_other_label IS NULL)
                          ),
                          metric_name VARCHAR(128) NOT NULL,          -- human-readable, e.g. "Inlet Temperature"
                          oid VARCHAR(255) NULL,                      -- required when protocol='snmp',
                                                                       -- enforced at publish-time validation
                                                                       -- (not DB, since 'other' protocols
                                                                       -- legitimately have none)
                          value_type CHECK IN ('integer','float','string','boolean','counter','gauge') NOT NULL,
                          unit VARCHAR(32) NULL,
                          scale NUMERIC(18,8) NOT NULL DEFAULT 1,
                          transform CHECK IN ('none','scale','offset','scale_and_offset') NOT NULL DEFAULT 'none',
                          offset NUMERIC(18,8) NULL,
                          default_collection_interval_seconds INT NULL CHECK (
                            default_collection_interval_seconds IS NULL OR default_collection_interval_seconds > 0
                          ),                                           -- NULL = inherit MonitoringPolicy's
                                                                        -- org-wide default_poll_interval_seconds
                          default_warning_threshold NUMERIC(18,4) NULL,
                          default_critical_threshold NUMERIC(18,4) NULL,
                          sort_order INT NOT NULL DEFAULT 0,
                          created_at/updated_at
```

**Explicit correctness rule, called out because getting it wrong would be a real defect:**
`default_warning_threshold`/`default_critical_threshold` are *suggested seed values only*. Alarm
evaluation (`app/application/drivers/*`, the future `AlarmRule` evaluator) must **never** read this table
at evaluation time — it is dereferenced exactly once, when an operator installs an asset against this
revision and chooses to seed an `AlarmRule`/`IntegrationMetricMapping` from it (§6.2). This keeps a
(structurally immutable, but still semantically "just a template") catalog value from ever silently
changing live alarm behavior, and keeps monitoring templates out of the credential/live-execution path
entirely (product principle 13).

Duplicate `stable_key` within a revision is a hard DB rejection (`UNIQUE` above). Duplicate `oid` within a
revision under *different* `stable_key`s is legitimate in rare vendor cases (two logical metrics reusing
one OID with different scale/transform) — the publish-time validator (§5.2) flags this as a **warning** in
the validation summary, never a hard reject.

### 4.7 The legacy bridge — how this stays non-breaking

```text
ALTER TABLE rack_model_revision      ADD COLUMN bridged_from_catalog_revision_id UUID NULL UNIQUE
                                       FK→catalog_model_revision RESTRICT;
ALTER TABLE equipment_model_revision ADD COLUMN bridged_from_catalog_revision_id UUID NULL UNIQUE
                                       FK→catalog_model_revision RESTRICT;
```

Both are purely additive, nullable columns — zero change to any existing row, existing query, existing
`NOT NULL RESTRICT` FK on `Rack`/`Equipment`, or existing test. `rack_model`/`rack_model_revision`/
`equipment_model`/`equipment_model_revision` are **never dropped, renamed, or altered in shape** by this
design.

**Publish-time bridge creation** (§5.3), inside the same transaction as the `draft → published`
transition, for `category IN ('rack', 'equipment')` only:

1. Find-or-create a legacy `RackModel`/`EquipmentModel` row keyed on `(manufacturer.name, catalog_model.model_name)`
   — reusing that table's own existing `UNIQUE(manufacturer, model_name)` constraint.
2. Insert one new legacy `RackModelRevision`/`EquipmentModelRevision` row, copying the shared physical
   columns (`height_u`, `width_mm`, `depth_mm`, `weight_capacity_kg`/`weight_kg`), unit-converting to
   mm/kg if the draft was authored in inches/pounds.
3. Set `catalog_model_revision.legacy_{rack,equipment}_model_revision_id` to the new legacy row's id, and
   set the new legacy row's own `bridged_from_catalog_revision_id` back to this `CatalogModelRevision.id`
   (a real two-way FK pair, not a one-directional pointer, so either table can be joined from the other).

This happens **exactly once**, at first publish — a `CatalogModelRevision` never re-publishes (§5, product
principle 5: a further change is always a new draft/new revision), so the legacy row is created once and
never touched again, consistent with its own pre-existing immutability convention.

Consequence: **installing** a Rack/Equipment against a Phase-10A-authored model, and **migrating** an
already-installed one to a newer revision (§5.9), both resolve entirely in terms of
`CatalogModelRevision.id` at the API/UI layer and only translate to the legacy `*_model_revision_id`
internally, in the same service call that writes `Rack.model_revision_id`/`Equipment.model_revision_id`.
The legacy id is never exposed to the new admin-facing API or UI.

---

## 5. Versioning and migration workflow

### 5.1 Creating and editing a draft

`POST /catalog/models/{model_id}/revisions` creates a new `CatalogModelRevision` with
`lifecycle_status='draft'`, `revision_number = max(existing) + 1` (starting at 1), and all optional fields
`NULL`/empty. Every subsequent `PATCH` to the revision, or `POST`/`PATCH`/`DELETE` on its child rows
(ports, PSUs, graphics, markers, monitoring templates) requires `lifecycle_status = 'draft'` — enforced by
both the API service layer and the DB trigger in §5.4 (defense in depth, matching this repo's existing
posture of enforcing invariants at more than one layer where the invariant is safety-critical, e.g. the
`AuditLog` append-only REVOKE). A draft's `version` column protects concurrent edits via the existing
`If-Match`/`ConflictError` convention (§1.5) — two admins editing the same draft race exactly like two
admins editing the same `Room`.

A draft may be deleted outright (`DELETE /catalog/revisions/{id}`, draft-only) — cascades to its child
template/graphic rows. This is the only delete operation this feature ever offers; published/retired
revisions are never deleted.

### 5.2 Validation before publication

`POST /catalog/revisions/{id}/validate` (idempotent, side-effect-free) runs the full required-field/
consistency check for the revision's `category` and returns a structured summary:

```json
{
  "valid": false,
  "errors": [
    {"field": "width_value", "code": "required_for_category", "message": "Width is required for rack models."},
    {"field": "network_ports[2].oid", "code": "missing_oid_for_snmp", "message": "..."}
  ],
  "warnings": [
    {"field": "monitoring[3].oid", "code": "duplicate_oid", "message": "Same OID as 'Outlet 4 Load' (different stable_key)."}
  ]
}
```

Rules enforced (non-exhaustive, representative):

- `category='rack'` requires `dimension_unit`, `width_value`, `height_value`, `depth_value`,
  `rack_unit_height`, `weight_unit`, `weight_value`.
- `category='equipment'` requires `dimension_unit`, `width_value`, `height_value`, `depth_value`,
  `weight_unit`, `weight_value`; `rack_unit_height` is required only if `supported_placement_types`
  includes `"rack_mounted"`.
- Every `MonitoringMetricTemplate` with `protocol='snmp'` requires a non-empty `oid`.
- At least one `PowerSupplyTemplate` is required whenever `rated_power_w`/`typical_power_w`/`max_power_w`
  is set (a nameplate power figure with no described PSU is treated as an incomplete draft).
- No two `NetworkPortTemplate`/`PowerSupplyTemplate`/`MonitoringMetricTemplate` rows may share a
  `stable_key` (already DB-enforced; surfaced here too so the summary is the one place an admin looks).

`POST /catalog/revisions/{id}/publish` **re-runs this exact check server-side** and rejects with 422 (the
same error shape) if it fails — the standalone `/validate` endpoint exists purely so the UI can show the
summary before the admin commits to publishing, never as the only enforcement.

### 5.3 Publishing

On success, in one transaction: `lifecycle_status → 'published'`, `published_at`/`published_by_user_id`
set, `version` frozen, the legacy bridge created (§4.7), `write_audit_log(action="catalog.revision.publish",
entity_type="catalog_model_revision", after={...}, reason=<admin-supplied optional note>)`, and
`write_outbox_event(event_type="CatalogModelRevisionPublished", aggregate_type="catalog_model_revision",
payload={...})`. From this point, every mutating endpoint on this revision and its children returns 409
(`ConflictError`, message: "This revision is published and immutable.").

### 5.4 Immutability enforcement — application and database

**Application:** every mutating service function for a revision or its child rows loads the parent
revision's `lifecycle_status` first and raises `ConflictError` if it is not `'draft'`, before touching the
database.

**Database:** a single trigger function, reused by every table in the aggregate (mirroring this repo's
existing one-function/many-`CREATE TRIGGER` style, e.g. `trg_managed_asset_replacement_acyclic`):

```sql
CREATE FUNCTION fn_reject_write_on_non_draft_revision() RETURNS trigger AS $$
DECLARE
  v_status TEXT;
  v_revision_id UUID := COALESCE(NEW.catalog_model_revision_id, OLD.catalog_model_revision_id);
BEGIN
  SELECT lifecycle_status INTO v_status FROM catalog_model_revision WHERE id = v_revision_id;
  IF v_status <> 'draft' THEN
    RAISE EXCEPTION 'catalog_model_revision % is not a draft (status=%): child rows are immutable', v_revision_id, v_status
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- BEFORE INSERT OR UPDATE OR DELETE on network_port_template, power_supply_template, catalog_graphic,
-- catalog_graphic_marker, monitoring_metric_template — each: EXECUTE FUNCTION fn_reject_write_on_non_draft_revision()
```

`catalog_model_revision` itself gets a second, narrower trigger allowing only the specific columns a
lifecycle transition is permitted to touch after leaving `draft` (`lifecycle_status` draft→published→
retired, `published_at`/`published_by_user_id`, `retired_at`/`retired_by_user_id`/`retirement_reason`,
`allow_installation_when_retired`, and the two `legacy_*_revision_id` bridge columns, exactly once each) —
any other column changing on a non-draft row raises the same exception. This closes the literal DB-level
enforcement gap called out as a hard requirement, without touching the ORM's declarative mapping (raw SQL
in the Alembic migration, matching every other cross-cutting DB invariant in this codebase).

### 5.5 Cloning a published revision into a new draft

`POST /catalog/models/{model_id}/revisions/clone?from_revision_id={id}` — deep-copies every field and
every child row (ports/PSUs/monitoring templates keep their exact `stable_key`s; graphics are copied by
reference, §7.4) from a published (or retired) revision into a brand-new `draft` row with
`revision_number = max(existing) + 1` and `cloned_from_revision_id` set for provenance. This is the
expected day-to-day path for "the vendor issued a corrected spec" — an admin never hand-re-enters an
entire port map to fix one column.

### 5.6 Retiring a revision

`POST /catalog/revisions/{id}/retire` (`published` → `retired` only; `reason` required, per §1.5's
guarded-action convention). Never touches installed `Rack`/`Equipment` rows — their `model_revision_id`
FKs are untouched, still `RESTRICT`-protected, still resolvable. `allow_installation_when_retired` defaults
`false`; an admin may set it `true` at retire time (or later, via a narrow follow-up PATCH the immutability
trigger's column allowlist permits) to intentionally keep a retired revision selectable for new
installations — satisfying "preventing new installations... unless explicitly allowed" precisely.

### 5.7 Comparing revisions

`GET /catalog/revisions/compare?left={id}&right={id}` (both must belong to the same `catalog_model_id`) —
a pure read, field-by-field diff across the revision's own columns and a `stable_key`-matched diff of each
child collection (added/removed/changed, matched by `stable_key` so a renamed-but-otherwise-identical port
shows as "changed," not "removed + added"). No mutation, no permission beyond `catalog:read`.

### 5.8 Identifying affected installed assets (impact preview)

`GET /catalog/revisions/{id}/installed-assets` — resolves the revision's legacy bridge id and returns
every `Rack`/`Equipment` row whose `model_revision_id` currently equals it, joined through
`ManagedAsset`/`RackPlacement`/`EquipmentPlacement` for `asset_tag`, current location, and
`lifecycle_status`. This is also the endpoint the migration workflow (§5.9) calls first, always, before
offering a migration action.

### 5.9 Explicit migration workflow

Migration means: repoint one or more installed `Rack`/`Equipment` rows from their current (legacy) revision
to a **different, published** `CatalogModelRevision`'s bridged legacy revision. It is never automatic and
never happens as a side effect of publishing a new revision (product principle: *"Existing installed assets
must not be silently repointed to a newer revision"*).

1. **Preview** — `POST /catalog/revisions/{target_id}/migration-preview` with a list of target
   `managed_asset_id`s (or "all assets currently on revision X"). Returns, per asset, a compatibility
   verdict: `compatible`, `compatible_with_warnings` (e.g. target has fewer network ports than the asset's
   current `NetworkInterface` count — meaning at least one existing interface will lose its template
   linkage, §5.11), or `blocked` (e.g. target `category` mismatch, which cannot happen through the API
   since the picker only ever offers same-`catalog_model_id` targets, but is still checked server-side,
   never trusted from the request).
2. **Validate + apply, per-asset or bulk** — `POST /catalog/revisions/{target_id}/migrate` with the same
   asset list, `reason` (required — §1.5), and `if_match_version` **per asset** (the caller must have
   fetched each `Rack`/`Equipment`'s current `version` from the preview call, closing the same race the
   existing `If-Match` pattern closes everywhere else). Runs the compatibility check again server-side
   (never trusts the client's earlier preview), then, **one asset per sub-transaction inside one overall
   request**:
   - `UPDATE rack/equipment SET model_revision_id = <new legacy id>, version = version + 1 WHERE id = ? AND version = ?`
   - `write_audit_log(action="rack.migrate_revision"/"equipment.migrate_revision", entity_id=asset_id,
     before={"model_revision_id": old}, after={"model_revision_id": new}, reason=<required>)`
   - `write_outbox_event(event_type="RackModelRevisionMigrated"/"EquipmentModelRevisionMigrated", ...)`
3. **Bulk rollback semantics:** the overall request processes each asset independently and returns a
   per-asset result list (`migrated` / `skipped_conflict` / `skipped_blocked`) rather than an all-or-
   nothing transaction across every asset — a stale `version` on asset #47 of 200 must never roll back the
   46 that already succeeded. Each **individual** asset's migration is atomic (its DB update + audit +
   outbox share one sub-transaction and either all commit or all roll back together) — never a partial
   write on a single asset. This mirrors the existing bulk-tolerant pattern already used for reconciliation
   decisions (`ReconciliationDiff`, processed one at a time with individual outcomes) rather than inventing
   a new bulk-transaction shape.

### 5.10 Stable component identities across revisions

`stable_key` (on `NetworkPortTemplate`, `PowerSupplyTemplate`, `MonitoringMetricTemplate`) is the answer to
*"is this the same logical port across a clone/new revision, or a different one?"* — an admin assigns it
once when first authoring a component and a clone (§5.5) preserves it verbatim. Cross-revision consumers
(the migration compatibility check in §5.8/§5.9, and any future "does the installed override for `eth0`
still make sense on the new revision" check) match components by `stable_key` within the same
`catalog_model_id`'s revision history, never by row `id` (which is, correctly, different on every revision)
and never by display name (which is expected to change — e.g. a corrected label).

### 5.11 Treatment of instance overrides when a template component is removed or renamed

A **rename** (same `stable_key`, different `display_name`/`label`) is transparent — any installed-asset
override keyed by `stable_key` (§6.1) keeps resolving correctly.

A **removal** (a `stable_key` present in the asset's currently-installed revision but absent from the
migration target) is flagged as a `compatible_with_warnings` case in the migration preview (§5.9, step 1)
and never silently drops data: the installed `NetworkInterface`/`IntegrationMetricMapping` row that was
tracking the removed template keeps existing exactly as it is (it is never deleted by a migration), but its
`port_template_id`/equivalent linkage is set to `NULL` (an orphaned-but-intact override, §6.1) and the
migration preview lists it explicitly so the admin can decide, per §6's inheritance rule, whether to keep
it as a fully-manual local field or remove it separately through the existing equipment/network editing UI.
Migration itself never deletes an installed instance row.

---

## 6. Installed-asset responsibilities and inheritance

Everything in this list stays exactly where it already lives today — on `Rack`/`Equipment`/`NetworkDevice`/
`NetworkInterface`/`Integration`/`IntegrationMetricMapping`/`PowerNode` — never duplicated onto the
catalog:

- Management IPs, hostnames, site addressing — `Equipment.ip_address`/`hostname`,
  `NetworkDevice`/`NetworkInterface` fields, unchanged.
- Credential references — `Integration.credential_ciphertext`, unchanged; the catalog never gains a
  credential-shaped column anywhere (product principle 11).
- Collector/integration assignment — `CollectorAssignment`, unchanged.
- Enabled/disabled metrics, polling overrides — `IntegrationMetricMapping`, unchanged.
- Installation-specific thresholds — a future `AlarmRule`, unchanged; never read from
  `MonitoringMetricTemplate` at evaluation time (§4.6).
- Local physical/electrical overrides — `Equipment.custom_attributes` JSONB (already exists precisely for
  this), unchanged.
- Operational state/observed values — `ManagedAsset.lifecycle_status`, `TelemetryReading`, unchanged.

### 6.1 Inheritance and override representation

At **installation time** (creating a `Rack`/`Equipment` against a published `CatalogModelRevision`, or
migrating one — §5.9), the service layer **seeds** `NetworkInterface` rows from
`NetworkPortTemplate`s:

```text
NetworkInterface gains one new nullable column: port_template_id FK→NetworkPortTemplate SET NULL
```

Each seeded `NetworkInterface` copies `display_name → name`, `media_type`, `connector_type`,
`supported_speeds_mbps[0] (or explicit choice) → speed_mbps`, and sets `port_template_id` to the source
template's id. Every field remains a normal, independently-editable column on `NetworkInterface` exactly as
today — "inherited" only describes *where the initial value came from*, never a live read-through: editing
the template later (impossible post-publish anyway) or migrating the asset to a new revision (§5.11) never
retroactively changes an already-seeded `NetworkInterface` row.

**Distinguishing inherited vs. overridden in the UI:** a `NetworkInterface` whose current field values still
exactly match its `port_template_id`'s current template values renders with an "Inherited" badge; any field
the operator has since changed renders with an "Overridden" badge and a "reset to template default" action.
This is computed at read time by the API (a diff between the instance row and its `port_template_id` join),
never stored as a separate boolean-per-field — avoiding a second source of truth for "is this overridden."
An `NetworkInterface` with `port_template_id IS NULL` (manually created, or orphaned per §5.11) renders with
no inheritance badge at all — it is unambiguously fully local data.

The same seed-then-diverge pattern applies to a `MonitoringMetricTemplate` → `IntegrationMetricMapping` at
the moment an operator wires up an `Integration` for the asset (an explicit, operator-initiated action —
never automatic, since it requires choosing which `Integration` polls the device, entirely outside this
phase's scope): `IntegrationMetricMapping` gains a nullable `metric_template_id FK→MonitoringMetricTemplate
SET NULL` for the same provenance/badge purpose, pre-filling `source_identifier (OID)`, `canonical_metric`
mapping guidance, `unit`, and `scale` from the template as a starting point the operator can freely edit or
delete.

`PowerSupplyTemplate` seeds informational-only `PowerNode` rows of type `equipment_power_input` (already an
existing `POWER_NODE_TYPES` value, §1) at install time, matching `quantity` — actual `PowerConnection` wiring
to a PDU/UPS remains a fully separate, manual, installed-asset-only action exactly as it is today.

### 6.2 What Phase 10A explicitly does not do here

No collector is contacted, no `IntegrationMetricMapping` is required to exist, no `Integration` row is
created by any catalog action. Seeding is a one-time, admin/operator-triggered convenience at install/
migrate time, not a live binding — reinforcing product principle 13.

---

## 7. Graphics and file storage

### 7.1 New infrastructure required (none of this exists today, §1.3)

```text
app/infrastructure/storage/catalog_image_storage.py
  class CatalogImageStorage(Protocol):
      async def save(self, content: bytes, *, content_hash: str, mime_type: str) -> str: ...  # -> storage_key
      async def read(self, storage_key: str) -> bytes: ...
      async def delete(self, storage_key: str) -> None: ...

  class LocalDiskCatalogImageStorage(CatalogImageStorage):
      # writes to settings.catalog_image_storage_dir, content-addressed:
      #   {sha256[:2]}/{sha256}.{png|jpg}
      # — the path is derived entirely from the file's own hash, never from the client-supplied filename,
      # closing path traversal by construction (the same property svg_sanitizer.py's own docstring already
      # calls out for the *temp-file* path Starlette generates today).
```

A new `Settings.catalog_image_storage_dir` config value (env-var configured, matching every other
`app/core/config.py` setting). `LocalDiskCatalogImageStorage` is the Phase 10A implementation, consistent
with this repo's current "no object storage" reality; swapping to S3/MinIO later means writing one more
implementation of the same three-method `Protocol` — no other code changes, since the API/domain layer only
ever calls the `Protocol`, never `open()`/`boto3` directly.

Content-addressing means two administrators uploading byte-identical images (e.g. a shared vendor stock
photo across two model revisions) store the file once — `CatalogGraphic.storage_key` for both rows points
at the same on-disk object, and `delete()` is only ever actually invoked once every referencing
`CatalogGraphic` row is gone (a reference count, or — simpler and sufficient at this scale — deletion is a
no-op safety check: "does any other `catalog_graphic.storage_key` still reference this key" before an
actual unlink, run synchronously in the same transaction as the delete for correctness at the (deliberately
small) scale this table operates at).

### 7.2 Upload endpoint and validation

`POST /catalog/revisions/{id}/graphics/{side}` (`side` ∈ `front`/`rear`; draft-only, enforced per §5.4),
`multipart/form-data`, one `UploadFile`:

1. Check `Content-Length` (or a bounded incremental read) against `MAX_CATALOG_IMAGE_SIZE_BYTES` **before**
   reading the body fully — `ApiError(413, ...)`, exact pattern as `floor_plans.py:203`.
2. Content-sniff magic bytes — **PNG or JPEG only, reusing `validate_raster_image()` verbatim** from
   `app/application/svg_sanitizer.py`. SVG is explicitly rejected (§7.3).
3. Parse only the image header (PNG `IHDR` chunk / JPEG `SOF` marker) for `width_px`/`height_px` **without
   a full decode**, and reject if either exceeds `MAX_CATALOG_IMAGE_DIMENSION_PX` or their product exceeds
   a total-pixel cap — a decompression-bomb guard, checked before any full-image decode, mirroring
   `svg_sanitizer.py`'s own "check the cheap thing before the expensive thing" structure.
4. `sha256` the content; `storage.save(...)`.
5. Replace semantics: if a `CatalogGraphic` row already exists for this `(revision_id, side)`, this is a
   **replace** — delete the old row (triggering the reference-counted storage cleanup, §7.1) and insert the
   new one in the same transaction; every `CatalogGraphicMarker` belonging to the old graphic is deleted
   with it (`ON DELETE CASCADE`) — an admin replacing an image is expected to re-place markers on the new
   image, since pixel content (and therefore marker positions) may have changed entirely. This is the
   explicit "behavior when graphics are replaced during draft editing" the requirements call for: **destructive
   to markers, non-destructive to the port/PSU template rows they pointed at** (those live independently and
   are simply unmarked until re-placed).
6. `write_audit_log(action="catalog.graphic.upload", ...)`, `write_outbox_event(event_type=
   "CatalogGraphicUploaded", ...)`.

`MAX_CATALOG_IMAGE_SIZE_BYTES = 8 MB`, `MAX_CATALOG_IMAGE_DIMENSION_PX = 4096` — deliberately smaller than
the floor-plan raster cap (20 MB), since a device front/rear diagram has no legitimate reason to approach
that size; both are named constants in the same module style as `svg_sanitizer.py`'s existing caps, not
magic numbers.

### 7.3 SVG policy: rejected, not sanitized

Catalog graphics accept **PNG/JPEG only**. SVG uploads are rejected outright with a clear 422. Rationale,
stated explicitly per the requirement to disclose this decision: the existing `svg_sanitizer.py` converts
SVG into an abstract `rect`/`circle`/`text` shape list (a *Sanitized Intermediate Representation*) suitable
for floor-plan geometry detection — it deliberately does not preserve gradients, paths, embedded raster
fills, or styling, and was never designed to render a faithful device diagram. Accepting SVG as a
displayable catalog image would require a second, independently-hardened SVG *rendering* sanitizer (still
needing the same XXE/script/external-reference defenses, plus safe CSS/gradient/path handling) — genuinely
new security surface this phase does not need, since PNG/JPEG covers every real vendor-photo/diagram use
case. The marker overlay itself is **never** user-uploaded markup: marker positions are plain numeric
coordinates and labels are plain text, rendered by trusted first-party frontend code as an absolutely-
positioned overlay on top of the `<img>` (React's default escaping covers the label text) — zero XSS surface
regardless of the underlying image format.

### 7.4 Serving graphics

`GET /catalog/graphics/{graphic_id}/file` — looks up the `CatalogGraphic` row, calls
`storage.read(storage_key)`, streams the bytes with the stored `mime_type`, gated by `catalog:read` (the
same broad grant as everything else read-only in this feature, §9.2). Never a static file mount and never a
raw filesystem path derived from request input — access is always DB-row-lookup-then-storage-key, so there
is no path-traversal surface to defend against by construction, and no directory listing is possible since
no path is ever served that wasn't first validated to belong to an existing `CatalogGraphic` row the caller
is authorized to read.

### 7.5 Revision-safe storage

Because `storage_key` is content-addressed and a `CatalogGraphic` row's parent revision is immutable once
published (§5.4), a published revision's images can never change out from under an already-installed asset
— cloning a revision (§5.5) copies the `CatalogGraphic` row (same `storage_key`, new row, new
`catalog_model_revision_id`) rather than sharing a mutable reference, so editing the clone's images never
touches the original published revision's.

---

## 8. Audit and outbox — exact actions and event types

Closing the gap in §1.1 (today's catalog endpoints write neither). Every mutating endpoint below writes
both, synchronously, in the same transaction as its domain mutation, following `write_audit_log`/
`write_outbox_event` exactly as used elsewhere in `app/application/*_service.py`.

| Operation | Audit `action` | Outbox `event_type` | `reason` required? |
|---|---|---|---|
| Create manufacturer | `catalog.manufacturer.create` | `ManufacturerCreated` | no |
| Create model (identity) | `catalog.model.create` | `CatalogModelCreated` | no |
| Create draft revision | `catalog.revision.create_draft` | `CatalogModelRevisionDraftCreated` | no |
| Edit draft revision / child rows | `catalog.revision.update_draft` | `CatalogModelRevisionDraftUpdated` | no |
| Upload/replace graphic | `catalog.graphic.upload` | `CatalogGraphicUploaded` | no |
| Delete a marker/port/PSU/metric row (draft only) | `catalog.revision.component_remove` | `CatalogModelRevisionDraftUpdated` | no |
| Publish revision | `catalog.revision.publish` | `CatalogModelRevisionPublished` | optional note |
| Retire revision | `catalog.revision.retire` | `CatalogModelRevisionRetired` | **yes** |
| Allow installation on retired revision | `catalog.revision.retire_override` | `CatalogModelRevisionRetireOverrideChanged` | **yes** |
| Clone revision into new draft | `catalog.revision.clone` | `CatalogModelRevisionDraftCreated` (with `cloned_from_revision_id` in payload) | no |
| Import preview | `catalog.import.preview` | *(none — read-only, no mutation)* | no |
| Import apply | `catalog.import.apply` | `CatalogImportApplied` | no |
| Migrate installed asset(s) to a new revision | `rack.migrate_revision` / `equipment.migrate_revision` | `RackModelRevisionMigrated` / `EquipmentModelRevisionMigrated` | **yes** |

`before`/`after` on every row follow the existing `AuditLog` redaction rule (`_SENSITIVE_FIELD_NAMES` in
`audit_service.py`) automatically — nothing in this schema introduces a new sensitive-field name, and
nothing here needs to (product principle 11: no credentials ever enter this domain).

---

## 9. Security and data integrity

### 9.1 RBAC — a new, dedicated permission family (deliberate behavior change)

**Decision, stated explicitly because it changes existing behavior:** the existing catalog-authoring
capability implicitly granted via `rack:manage`/`equipment:manage` (held by Administrator, DCIM Manager,
*and* Engineer today, §1.1) is **removed** from the new catalog surface. The legacy `POST /rack-models`,
`POST /rack-models/{id}/revisions`, `POST /equipment-models`, `POST /equipment-models/{id}/revisions`
endpoints (§1.1) are deprecated in favor of this design's endpoints and, if kept at all during a transition
window, have their required permission tightened to the new admin-only codes below — they must not remain a
side door around product principle 8. This is a necessary, disclosed consequence of that principle, not an
oversight: Engineer loses catalog-authoring rights it currently holds.

```text
catalog:read          — Manufacturer, CatalogModel, published/retired CatalogModelRevision + all child
                         data, graphics. Granted to every role that already holds rack:read/equipment:read
                         today (Administrator, DCIM Manager, Engineer, Operator, Viewer) — satisfies
                         "read access may be granted more broadly."
catalog:read_draft     — same, extended to draft revisions. Administrator only (drafts are working
                         material, not yet fit for general inventory/installation workflows).
catalog:manage         — create Manufacturer/CatalogModel, create/edit/delete draft revisions and their
                         child rows, clone. Administrator only.
catalog:publish        — publish a draft. Administrator only.
catalog:retire         — retire a published revision, toggle allow_installation_when_retired.
                         Administrator only.
catalog:import         — JSON import preview + apply. Administrator only.
catalog:migrate        — installed-asset revision migration (preview + apply). Administrator only.
```

Seeded (new migration, §12) into `DEFAULT_ROLE_PERMISSIONS`: `catalog:read` added to every role's existing
list; `catalog:read_draft`/`catalog:manage`/`catalog:publish`/`catalog:retire`/`catalog:import`/
`catalog:migrate` added **only** to `Administrator`. Every mutating endpoint depends on
`Depends(require_permission("catalog:..."))` exactly like every other protected route in this codebase —
no new enforcement mechanism, reusing `app/application/rbac.py` unchanged.

Frontend: a new `RequireCatalogAdmin` wrapper (same UX-convenience-only posture as `ProtectedRoute`,
`frontend/src/components/layout/ProtectedRoute.tsx`) gates the `/admin/catalog/*` route tree on
`useHasPermission("catalog:manage")`, redirecting non-admins with a clear message — never the enforcement
boundary, since the backend `require_permission` dependency is.

### 9.2 File upload validation and storage safety

Covered fully in §7.2–§7.4: magic-byte content sniffing (never filename/declared content-type), size cap
checked before full read, pixel-dimension cap checked before decode, content-addressed storage keys (never
derived from client input), no static file mount, access always through an authorized DB-row lookup.

### 9.3 SVG policy

Rejected outright for catalog graphics (§7.3) — raster (PNG/JPEG) only.

### 9.4 JSON schema validation (import)

The import document (§13) validates against a versioned Pydantic model tree
(`CatalogImportDocumentV1`), the same mechanism every other request body in this codebase already uses —
FastAPI's built-in `RequestValidationError` → 422 with per-field errors, not a hand-rolled parser and not a
generic `dict`-typed endpoint (explicitly avoiding "generic endpoints that accept arbitrary unconstrained
JSON").

### 9.5 Import limits and DoS controls

- Uploaded JSON document size cap: 2 MB (metadata-only content; images are never embedded, §13.3), checked
  before full read, same `ApiError(413, ...)` pattern as §7.2.
- Per-import-document limits: max 50 manufacturers, max 200 models, max 500 revisions, max 5,000 total
  child rows (ports+PSUs+monitoring entries combined) — a single oversized document is rejected outright at
  preview time with a clear count-exceeded error, never partially processed.
- No nested nesting-depth concern exists for JSON the way it does for XML/SVG (no entity expansion, no
  recursive structure attack surface) — standard `json.loads` with Python's default recursion-safe parser
  and the size cap above is sufficient; this is explicitly *not* the same threat model as `svg_sanitizer.py`
  and does not need its nesting-depth machinery.
- Preview and apply are two separate requests (§13.2) — apply never re-parses attacker-controlled bytes; it
  operates only on the already-validated `CatalogImportJob` row.

### 9.6 Uniqueness and lifecycle constraints

Enforced at the DB level throughout §4: `Manufacturer.name` unique, `CatalogModel(manufacturer_id,
model_name)` unique, `CatalogModelRevision(catalog_model_id, revision_number)` unique, every child table's
`(catalog_model_revision_id, stable_key)` unique, `CatalogGraphic(catalog_model_revision_id, side)` unique,
`lifecycle_status` CHECK-constrained to the three valid values, legacy bridge XOR CHECK (§4.2).

### 9.7 Immutable-published-revision enforcement

Both layers, detailed in §5.4: application-layer status check before every write, plus the
`fn_reject_write_on_non_draft_revision()` DB trigger reused across every child table.

### 9.8 Audit redaction

Automatic and unchanged — `audit_service.py`'s existing `_redact()` covers this domain with zero new code,
because nothing in this domain's `before`/`after` payloads is credential-shaped (product principle 11 holds
by construction, not by a redaction list needing to catch a mistake).

### 9.9 Absence of secrets in catalog data and exports

Verified by construction: no table in §4 has a column referencing `Integration`, `Collector`, or any
`*_ciphertext`/credential-shaped field. The export document (§13.3) is generated purely from these tables,
so it inherits the same guarantee — there is no code path by which a secret could reach an export, not just
a redaction step that removes one if present.

### 9.10 Authorization and integrity tests (what a future implementation phase must add)

- Every new endpoint: a 403 test for each permission code with a user who lacks it (mirroring
  `backend/tests/api/test_security.py`'s existing pattern).
- A test that a `DCIM Manager`/`Engineer`/`Operator` role **cannot** create a `Manufacturer`/`CatalogModel`/
  draft revision, publish, retire, import, or migrate — the concrete regression test for §9.1's behavior
  change.
- A test that mutating any child row of a `published`/`retired` revision returns 409 both through the
  service layer and via a raw SQL `UPDATE` against the trigger directly (integration test, real Postgres —
  matching this repo's existing "DB constraints tested against a real database, not mocked" convention,
  `backend/tests/integration/`).
- A test that publishing twice, or retiring a draft, is rejected.
- A raster-upload test suite mirroring `backend/tests/unit/test_svg_sanitizer.py`'s structure: oversized
  file, wrong magic bytes vs. declared type, oversized pixel dimensions, SVG rejected, PNG/JPEG accepted.
- A migration-workflow test: stale `version` on one asset in a bulk migration does not roll back the others
  (§5.9's per-asset-atomic, not all-or-nothing, semantics).
- Import: oversized document, over-limit counts, malformed JSON, valid-but-duplicate manufacturer/model
  (should report as a preview-time conflict, never a 500).

---

## 10. API contracts

Router: `app/api/v1/catalog_designer.py` (new — kept separate from the existing `app/api/v1/catalog.py` so
the legacy create+list-only endpoints are not silently changed in place; the new router is additive, and
the deprecation of the legacy endpoints per §9.1 is a distinct, explicitly-called-out follow-up change to
`catalog.py`, not folded invisibly into this new file). Request/response models: inline Pydantic
`{Entity}In`/`{Entity}Out`, `Page[T]` for lists — same conventions as every other router in this codebase
(§1.5).

| Method & path | Purpose | Permission |
|---|---|---|
| `POST /catalog/manufacturers` | Create manufacturer | `catalog:manage` |
| `GET /catalog/manufacturers` | List/search manufacturers | `catalog:read` |
| `GET /catalog/manufacturers/{id}` | Manufacturer detail | `catalog:read` |
| `POST /catalog/models` | Create model identity | `catalog:manage` |
| `GET /catalog/models` | List models (filter: category, status, manufacturer_id, text search) | `catalog:read` |
| `GET /catalog/models/{id}` | Model detail + revision list | `catalog:read` |
| `POST /catalog/models/{id}/revisions` | Create new draft | `catalog:manage` |
| `POST /catalog/models/{id}/revisions/clone?from_revision_id=` | Clone published/retired → new draft | `catalog:manage` |
| `GET /catalog/revisions/{id}` | Revision detail (draft: `catalog:read_draft`; published/retired: `catalog:read`) | see note |
| `PATCH /catalog/revisions/{id}` | Edit draft scalar fields (`If-Match` required) | `catalog:manage` |
| `DELETE /catalog/revisions/{id}` | Delete draft | `catalog:manage` |
| `POST /catalog/revisions/{id}/network-ports` | Add port template | `catalog:manage` |
| `PATCH /catalog/revisions/{id}/network-ports/{port_id}` | Edit port template | `catalog:manage` |
| `DELETE /catalog/revisions/{id}/network-ports/{port_id}` | Remove port template | `catalog:manage` |
| `POST /catalog/revisions/{id}/power-supplies` | Add PSU template | `catalog:manage` |
| `PATCH /catalog/revisions/{id}/power-supplies/{psu_id}` | Edit PSU template | `catalog:manage` |
| `DELETE /catalog/revisions/{id}/power-supplies/{psu_id}` | Remove PSU template | `catalog:manage` |
| `POST /catalog/revisions/{id}/monitoring-metrics` | Add metric template | `catalog:manage` |
| `PATCH /catalog/revisions/{id}/monitoring-metrics/{metric_id}` | Edit metric template | `catalog:manage` |
| `DELETE /catalog/revisions/{id}/monitoring-metrics/{metric_id}` | Remove metric template | `catalog:manage` |
| `POST /catalog/revisions/{id}/graphics/{side}` | Upload/replace front or rear graphic | `catalog:manage` |
| `DELETE /catalog/revisions/{id}/graphics/{side}` | Remove graphic | `catalog:manage` |
| `GET /catalog/graphics/{graphic_id}/file` | Stream image bytes | `catalog:read` |
| `POST /catalog/graphics/{graphic_id}/markers` | Place a marker | `catalog:manage` |
| `PATCH /catalog/graphics/{graphic_id}/markers/{marker_id}` | Move/relabel a marker | `catalog:manage` |
| `DELETE /catalog/graphics/{graphic_id}/markers/{marker_id}` | Remove a marker | `catalog:manage` |
| `POST /catalog/revisions/{id}/validate` | Validation summary (no mutation) | `catalog:read_draft` |
| `POST /catalog/revisions/{id}/publish` | Publish | `catalog:publish` |
| `POST /catalog/revisions/{id}/retire` | Retire | `catalog:retire` |
| `PATCH /catalog/revisions/{id}/retire-override` | Toggle `allow_installation_when_retired` | `catalog:retire` |
| `GET /catalog/revisions/compare` | Diff two revisions | `catalog:read` |
| `GET /catalog/revisions/{id}/installed-assets` | Impact preview (who's on this revision) | `catalog:read` |
| `POST /catalog/revisions/{target_id}/migration-preview` | Compatibility preview for a migration | `catalog:migrate` |
| `POST /catalog/revisions/{target_id}/migrate` | Apply migration (per-asset atomic, bulk-tolerant) | `catalog:migrate` |
| `POST /catalog/import/preview` | Validate + dry-run diff an import document | `catalog:import` |
| `POST /catalog/import/{job_id}/apply` | Apply a previously previewed import (creates drafts only) | `catalog:import` |
| `GET /catalog/export?model_id=\|manufacturer_id=` | Export a portable JSON document | `catalog:read` |

`GET /catalog/revisions/{id}` permission note: the handler checks `lifecycle_status` after loading the row
and requires `catalog:read_draft` only when it is `'draft'`; `published`/`retired` rows only need
`catalog:read` — this is the one endpoint where the permission check is conditional on row state rather
than a single static `Depends(require_permission(...))`, called out explicitly since it is the one
deliberate exception to this codebase's otherwise-static permission-per-route convention.

---

## 11. Administrator experience

Bounded screens/components, each independently testable — no single enormous form. Route tree:
`/admin/catalog/*`, added to `AppShell.tsx`'s nav as a new **Admin** group (new — no such group exists
today), gated by `useHasPermission("catalog:manage")` for the group's visibility and by
`RequireCatalogAdmin` (§9.1) at the route level.

| Route | Component | Responsibility |
|---|---|---|
| `/admin/catalog` | `CatalogHomePage` | Manufacturer list + model search/filter (category, status, manufacturer), entry point only |
| `/admin/catalog/manufacturers/:id` | `ManufacturerDetailPage` | Manufacturer fields + its models list |
| `/admin/catalog/models/:id` | `ModelDetailPage` | Model identity fields + revision history table (status, published/retired dates, "New Draft"/"Clone" actions) |
| `/admin/catalog/revisions/:id` | `RevisionEditorPage` | Draft editor shell — tabs/sections below; read-only rendering when the loaded revision is published/retired |
| — section | `RevisionIdentitySection` | model_number, description, tags (draft only) |
| — section | `RevisionPhysicalSection` | dimensions, unit, rack_unit_height, weight, mounting, airflow |
| — section | `RevisionElectricalSection` | rated/typical/max power, heat dissipation, redundancy mode |
| — section | `PowerSupplyTemplateEditor` | PSU list (add/edit/remove), each row = connector/voltage/current/redundancy |
| — section | `NetworkPortTemplateEditor` | Port list (add/edit/remove), table view — the non-graphical fallback for port editing (§4.5) |
| — section | `MonitoringTemplateEditor` | Metric list (add/edit/remove), protocol/OID/value-type/scale/transform/thresholds |
| — section | `GraphicsMarkerEditor` | Front/rear image upload + canvas marker placement, keyboard-operable (below) |
| `/admin/catalog/revisions/:id/validate` (modal, not a route) | `ValidationSummaryDialog` | Reuses `components/ui/Dialog.tsx`; lists errors/warnings from §5.2, "Publish" disabled until clean |
| `/admin/catalog/revisions/compare?left=&right=` | `RevisionCompareView` | Read-only diff view, §5.7 |
| `/admin/catalog/revisions/:id/impact` | `InstalledAssetsImpactPanel` | §5.8 list, entry point to migration |
| `/admin/catalog/revisions/:id/migrate` | `MigrationWizard` | Target picker → preview (§5.9 step 1, per-asset verdicts) → confirm with required `reason` → per-asset result list |
| `/admin/catalog/import` | `ImportPage` | File picker → preview table (per-item status) → confirm-apply |
| `/admin/catalog/models/:id/export` (button, not a route) | — | Triggers `GET /catalog/export`, browser download |

### 11.1 `GraphicsMarkerEditor` — accessible, keyboard-operable

The canvas (an `<img>` with an absolutely-positioned overlay, §7.3) supports pointer drag-to-place, but
every marker is **also** editable through a plain form: a table listing every marker with numeric `x`/`y`
input fields (0–1, or a "snap to component" convenience that fills them from a `NetworkPortTemplate`/
`PowerSupplyTemplate` picker), reachable and fully operable via Tab/Enter/Arrow-key without ever touching
the canvas — satisfying "accessible keyboard operation and non-graphical component editing" directly,
rather than as an afterthought bolted onto a canvas-only editor. This mirrors the existing
`components/ui/Dialog.tsx`'s own accessibility bar (focus trap, labelled regions, keyboard-only operability)
already set for this codebase's UI primitives.

### 11.2 Reused, not new, primitives

`PageHeader`, `StatusBadge`, `MetricCard`, `EmptyState`, `SectionTitle` (`components/ui/ProductUi.tsx`) and
`Dialog` (`components/ui/Dialog.tsx`) are reused as-is. No new UI library is introduced — forms stay
`useState` + native `<form>`, matching every existing feature (§1.1); introducing `react-hook-form`/`zod`
here alone, while nothing else in the codebase uses them, would be a stack deviation this design
deliberately avoids.

### 11.3 Fixing the existing gap this phase makes worse otherwise

Per §1.1's finding (`RacksPage.tsx:104-106`), `RacksPage`/`EquipmentPage` currently mint a brand-new catalog
entry on every creation — which, if left alone, actively conflicts with a working Catalog Designer
(operators would keep creating duplicate, un-reviewed catalog rows through the side door instead of using
published revisions). This design requires that follow-up work, before or alongside Phase 10A's rollout,
replace that inline mint with: a "select an existing published model + revision" combobox (calling
`GET /catalog/models`/`GET /catalog/revisions/compare`-adjacent list endpoints, gated by `catalog:read`,
available to every existing role), with the "create new" path fully removed from those non-admin forms.
This is flagged here as required, not optional, because principle 8 is meaningfully violated for as long as
the old mint-on-create path stays reachable by a non-administrator.

---

## 12. Migration sequencing (Alembic)

All new migrations chain from the verified current head, `0016_network_runtime_defaults`. Proposed as
several small, single-concern migrations, matching this repo's own established granularity (Phase 3/8 each
shipped multiple sequential migrations rather than one large one):

1. `0017_catalog_manufacturer_and_model` — `manufacturer`, `catalog_model`, `catalog_model_revision` +
   `fn_reject_write_on_non_draft_revision()` + the narrower `catalog_model_revision`-specific trigger (§5.4).
2. `0018_catalog_component_templates` — `network_port_template`, `power_supply_template`,
   `monitoring_metric_template`, plus their `BEFORE INSERT/UPDATE/DELETE` triggers.
3. `0019_catalog_graphics` — `catalog_graphic`, `catalog_graphic_marker` + triggers;
   `Settings.catalog_image_storage_dir`.
4. `0020_catalog_legacy_bridge` — additive `bridged_from_catalog_revision_id` on `rack_model_revision`/
   `equipment_model_revision`; `legacy_rack_model_revision_id`/`legacy_equipment_model_revision_id` +
   their XOR CHECK on `catalog_model_revision` (added here, not in migration 1, since they reference tables
   that must already exist — ordering matters and is called out explicitly).
5. `0021_catalog_rbac_seed` — new `catalog:*` permissions, `catalog:read` added to every existing role,
   the rest to Administrator only (§9.1) — same `sa.table`/`op.bulk_insert` pattern as migration
   `0002_audit_partitions_retention_and_rbac_seed.py`.
6. `0022_catalog_import_job` — `catalog_import_job` (§13.2).

Each carries a module docstring in this codebase's established style (cites this design document + the
specific `ARCHITECTURE_REVIEW.md`/product-principle section it implements), `revision`/`down_revision` as
short semantic strings (matching `0015`/`0016`'s style, not an autogenerated hash), and a real `downgrade()`
that drops what it created — including the trigger functions and, for migration 5, the exact reverse
`DELETE`s migration `0002`'s own `downgrade()` already demonstrates.

---

## 13. Import/export contract

### 13.1 Design goal

A **versioned, portable JSON document** describing manufacturer + model + revision content, typed
throughout (no free-form blob), explicitly excluding credentials, secrets, and embedded image bytes
(product principle 12) — images travel as *references* an operator must separately have already uploaded
(or will upload after import, via the normal graphics endpoints), never as base64 payloads in the document.

### 13.2 Import flow — job, preview, explicit apply (never silent mutation)

```text
CatalogImportJob   PK id, uploaded_by_user_id FK→User RESTRICT, status CHECK IN
                   ('queued','validating','validated','applying','applied','failed','rejected'),
                   source_filename, file_hash, file_size_bytes, schema_version,
                   preview_result JSONB NULL,   -- per-item validation status, populated by /preview
                   rejection_reason NULL, finished_at NULL, created_at/updated_at
```

1. `POST /catalog/import/preview` — validates the document against `CatalogImportDocumentV1` (§13.3),
   checks every manufacturer/model/revision against existing rows (by name — a name match is reported as
   `conflict`, never silently merged or silently skipped), applies the limits in §9.5, and returns
   (synchronously — the document is JSON metadata only, small enough that this never needs Celery/async
   unlike floor-plan geometry parsing) a per-item table: `new` / `conflict:manufacturer_exists` /
   `conflict:model_exists` / `error:<validation detail>`. Nothing is written to `manufacturer`/
   `catalog_model`/`catalog_model_revision` yet — only the `CatalogImportJob` row itself, `status='validated'`.
2. `POST /catalog/import/{job_id}/apply` — re-validates the stored document is still consistent (rejects if
   a conflicting row was created by someone else in the meantime, a live version of the same conflict
   check), then creates every `new`-marked manufacturer/model/**draft** revision (never a published one —
   an imported revision always lands as `lifecycle_status='draft'`, requiring the normal validate→publish
   flow, so bulk-importing a spec sheet can never bypass publication review) in one transaction, writes one
   audit row + one `CatalogImportApplied` outbox event summarizing counts, sets `status='applied'`.
   `conflict`-marked items are always skipped, never overwritten — re-importing the same document twice is
   safe and idempotent (mirrors `ARCHITECTURE_REVIEW.md` §21's "re-uploading an identical file... is a
   no-op" idempotency principle).

### 13.3 Document schema (`CatalogImportDocumentV1`)

```json
{
  "schema_version": "1.0",
  "manufacturers": [
    {"name": "Acme Networks"}
  ],
  "models": [
    {
      "manufacturer_name": "Acme Networks",
      "category": "equipment",
      "subtype": "switch",
      "model_name": "AC-4800",
      "model_number": "AC4800-48P",
      "description": "48-port PoE+ access switch",
      "tags": ["poe", "access-layer"],
      "revisions": [
        {
          "revision_number": 1,
          "dimension_unit": "mm", "width_value": 440, "height_value": 44, "depth_value": 300,
          "rack_unit_height": 1,
          "weight_unit": "kg", "weight_value": 6.2,
          "mounting_orientation": "horizontal",
          "supported_placement_types": ["rack_mounted"],
          "airflow_direction": "front_to_rear",
          "rated_power_w": 740, "typical_power_w": 410, "max_power_w": 740,
          "heat_dissipation_btu_hr": 2525,
          "power_redundancy_mode": "single",
          "power_supplies": [
            {"stable_key": "psu-1", "label": "PSU 1", "quantity": 1, "redundancy_mode": "single",
             "connector_type": "C14", "rated_voltage_min": 100, "rated_voltage_max": 240,
             "rated_frequency_hz": 50, "rated_current_a": 8, "hot_swappable": false}
          ],
          "network_ports": [
            {"stable_key": "eth0", "display_name": "GigabitEthernet0/1", "numbering_pattern": "Gi0/{n}",
             "media_type": "copper", "supported_speeds_mbps": [1000], "connector_type": "rj45",
             "role": "access", "side": "front", "module_group": null}
          ],
          "monitoring_metrics": [
            {"stable_key": "temp-inlet", "protocol": "snmp", "metric_name": "Inlet Temperature",
             "oid": "1.3.6.1.4.1.9999.1.1.0", "value_type": "float", "unit": "celsius", "scale": 1,
             "transform": "none", "default_collection_interval_seconds": 300,
             "default_warning_threshold": 35, "default_critical_threshold": 45}
          ],
          "graphics": [
            {"side": "front", "reference": "external:acme-ac4800-front-v1"}
          ]
        }
      ]
    }
  ]
}
```

- `schema_version` is checked against a small set of versions this endpoint understands; an unknown/future
  version is rejected with a clear "unsupported schema_version" error rather than a best-effort parse.
- `graphics[].reference` is an **opaque external label only** (e.g. matching a filename the admin will
  separately upload through the normal graphics endpoint after import) — never a `storage_key`, never
  embedded bytes, satisfying product principle 12 exactly. Import never creates a `CatalogGraphic` row by
  itself.
- No field in this document can encode a credential, secret, OID community string, or `Integration`
  reference — the schema simply has no such field, so there is nothing to strip (the same "secure by
  absence" property as §9.9).
- Export (`GET /catalog/export`) produces exactly this same document shape from existing published data
  (and, if requested for a specific model, optionally its draft too — clearly labeled), making
  export→import round-trippable between environments (e.g. staging → production catalog promotion) — a
  deliberate design property, not incidental.
- Determinism: array ordering in an exported document is always `sort_order` (child rows) / `revision_number`
  (revisions) / `model_name` (models) / `name` (manufacturers) — never insertion order or unspecified DB
  order — so two exports of unchanged data are byte-identical, which matters for diffing catalog changes
  between environments in version control.

---

## 14. Explicit deviation from `ARCHITECTURE_REVIEW.md` §20, and why

§20's `Protocol → Driver → VendorProfile → DeviceProfile → MetricMapping` chain sketches `DeviceProfile`
keyed by a plain `model_name` string, entirely decoupled from `EquipmentModelRevision`. Phase 10A does not
build that table. Instead, `MonitoringMetricTemplate` (§4.6) attaches by real FK to the same
`CatalogModelRevision` that already owns physical/electrical/port/PSU data for that exact model — one
aggregate, one lifecycle, one place an admin edits "everything about this model," rather than two parallel,
string-matched catalogs that could silently drift out of sync (a real risk with §20's original sketch: a
`DeviceProfile.model_name = "AC-4800"` string has no referential guarantee it corresponds to any actual
`EquipmentModel` row, or that it stays correct if that model's canonical name changes). This mirrors exactly
how `PHASE8_ARCHITECTURE_CLARIFICATION.md` handled the same layering question for the driver framework
piece: a disclosed, reasoned correction to a prior sketch, not a silent contradiction of it. `VendorProfile`
(driver-level default *connection* config, not model-specific) remains a plausible future concept
orthogonal to this design and is not addressed here — it was never in tension with the catalog schema to
begin with.

---

## 15. Explicitly out of scope (restated from the prompt, confirmed against the design above)

- No topology editing — `PowerConnection`/`NetworkConnection` wiring is untouched; catalog PSU/port
  templates never create a connection, only informational seed rows (§6.1).
- No full spatial 3D — graphics are flat front/rear 2D marker overlays only, unrelated to
  `SpatialObject`/3D layout.
- No collector execution — nothing in this design calls a driver, contacts a collector, or reads live
  telemetry; `MonitoringMetricTemplate` is inert until an operator seeds an `IntegrationMetricMapping`
  (§6.2), which remains a fully separate, existing, manual workflow.
- No discovery automation — `DiscoveredDevice`/`ReconciliationDiff` are untouched; nothing here
  auto-matches a discovered device to a catalog model.

---

## 16. Summary of new/changed tables (quick reference)

**New:** `manufacturer`, `catalog_model`, `catalog_model_revision`, `network_port_template`,
`power_supply_template`, `catalog_graphic`, `catalog_graphic_marker`, `monitoring_metric_template`,
`catalog_import_job`.

**Additively altered (nullable columns only, zero risk to existing rows/FKs):** `rack_model_revision`
(+`bridged_from_catalog_revision_id`), `equipment_model_revision` (+`bridged_from_catalog_revision_id`),
`network_interface` (+`port_template_id`), `integration_metric_mapping` (+`metric_template_id`), `permission`/
`role_permission` (new `catalog:*` rows, no column changes).

**Untouched:** `rack_model`, `equipment_model`, `rack`, `equipment`, `managed_asset`, every power/network
instance table, `integration`, `collector`, every telemetry/alarm table — the entire installed-asset and
acquisition domain keeps working exactly as it does today, unaware this feature exists until an operator
chooses to use it.
