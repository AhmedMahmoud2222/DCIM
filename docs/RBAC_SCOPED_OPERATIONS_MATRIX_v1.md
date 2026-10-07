# Scoped RBAC route matrix (v1)

Generated 2026-10-07 from `main` 7364e68 by introspecting every `APIRouter` under `backend/app/api/v1/` (permission dependency, `is_scope_independent()` result and scope helpers used in the handler source). Paths are shown with the `/api/v1` prefix.

* **Active for restricted?** `yes` means a user without a `global` role assignment keeps the permission (it is in `SCOPE_AWARE_PERMISSIONS` or is `user:*`/`group:*`). `NO` means `load_effective_access()` moves it to `inactive_permissions` and `require_permission()` returns 403, so the endpoint fails closed. `n/a` means the route has no `require_permission` dependency (collector-signed, login, health, or an inline check described below).
* **Scope guard** lists the scope helpers found in the handler (`ensure_*_access`, `*_visible_clause`, `ctx.scope`). `-` means none.

Inline-checked routes shown as `n/a`: bulk-import job routes call `_check_import_permission()` / `_assert_job_visible()` (`bulk_import.py:50-72`; restricted callers see only jobs they uploaded, covering SEC-RBAC-59-04); catalog revision, compare and graphic routes call `has_permission()` after loading the revision (`catalog_designer.py:951-952`); collector `heartbeat`, `ingest` and `telemetry` authenticate with the collector HMAC signature, not a user token.

## Locations / organization

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/buildings` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/buildings` | `location:manage` | NO | - |
| GET | `/api/v1/cities` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/cities` | `location:manage` | NO | - |
| GET | `/api/v1/countries` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/countries` | `location:manage` | NO | - |
| GET | `/api/v1/floors` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/floors` | `location:manage` | NO | - |
| GET | `/api/v1/organizations` | `organization:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/organizations` | `location:manage` | NO | - |
| GET | `/api/v1/organizations/{organization_id}` | `organization:read` | yes | _visible_clause,ctx.scope |
| GET | `/api/v1/rooms` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/rooms` | `location:manage` | NO | - |
| GET | `/api/v1/rooms/{room_id}` | `location:read` | yes | ctx.scope,ensure_room_access |
| PATCH | `/api/v1/rooms/{room_id}` | `location:update` | NO | - |
| GET | `/api/v1/sites` | `location:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/sites` | `location:manage` | NO | - |
| GET | `/api/v1/sites/{site_id}` | `location:read` | yes | ctx.scope,ensure_site_access |

## Racks

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/racks` | `rack:read` | yes | _visible_clause,ctx.scope,rack_ids_in_site_query |
| POST | `/api/v1/racks` | `rack:manage` | yes | ctx.scope,ensure_room_access |
| POST | `/api/v1/racks/import-jobs` | `rack:import` | NO | - |
| GET | `/api/v1/racks/import-template` | `rack:read` | yes | - |
| GET | `/api/v1/racks/{rack_id}` | `rack:read` | yes | ctx.scope,ensure_rack_access |
| PATCH | `/api/v1/racks/{rack_id}` | `rack:manage` | yes | ctx.scope,ensure_rack_access |
| GET | `/api/v1/racks/{rack_id}/elevation` | `rack:read` | yes | ctx.scope,ensure_rack_access |
| POST | `/api/v1/racks/{rack_id}/move` | `rack:place` | yes | ctx.scope,ensure_rack_access,ensure_room_access |
| POST | `/api/v1/racks/{rack_id}/retire` | `rack:place` | yes | ctx.scope,ensure_rack_access |

## Equipment

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/equipment` | `equipment:read` | yes | _visible_clause,ctx.scope |
| POST | `/api/v1/equipment` | `equipment:manage` | NO | - |
| POST | `/api/v1/equipment/import-jobs` | `equipment:import` | NO | - |
| GET | `/api/v1/equipment/import-template` | `equipment:read` | yes | - |
| POST | `/api/v1/equipment/instantiate` | `equipment:manage` | NO | - |
| GET | `/api/v1/equipment/{equipment_id}` | `equipment:read` | yes | ctx.scope,ensure_equipment_access |
| PATCH | `/api/v1/equipment/{equipment_id}` | `equipment:manage` | NO | - |
| POST | `/api/v1/equipment/{equipment_id}/move` | `equipment:place` | NO | - |
| GET | `/api/v1/equipment/{equipment_id}/ports` | `equipment:read` | yes | ctx.scope,ensure_equipment_access |
| POST | `/api/v1/equipment/{equipment_id}/ports/connect` | `equipment:manage` | NO | - |
| POST | `/api/v1/equipment/{equipment_id}/retire` | `equipment:place` | NO | - |

## Managed assets

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/managed-assets` | `managed_asset:read` | NO | - |
| POST | `/api/v1/managed-assets` | `managed_asset:manage` | NO | - |
| GET | `/api/v1/managed-assets/{asset_id}` | `managed_asset:read` | NO | - |
| POST | `/api/v1/managed-assets/{asset_id}/lifecycle-transition` | `managed_asset:update_lifecycle` | NO | - |

## Floor plans / spatial

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/floor-plans` | `floor_plan:read` | NO | - |
| POST | `/api/v1/floor-plans` | `floor_plan:manage` | NO | - |
| GET | `/api/v1/floor-plans/import-jobs/{job_id}` | `floor_plan:read` | NO | - |
| GET | `/api/v1/floor-plans/import-jobs/{job_id}/candidates` | `floor_plan:read` | NO | - |
| POST | `/api/v1/floor-plans/import-jobs/{job_id}/candidates/{candidate_id}/accept` | `floor_plan:manage` | NO | - |
| POST | `/api/v1/floor-plans/import-jobs/{job_id}/candidates/{candidate_id}/reject` | `floor_plan:manage` | NO | - |
| GET | `/api/v1/floor-plans/import-jobs/{job_id}/diagnostics` | `floor_plan:read` | NO | - |
| GET | `/api/v1/floor-plans/{floor_plan_id}` | `floor_plan:read` | NO | - |
| POST | `/api/v1/floor-plans/{floor_plan_id}/activate` | `floor_plan:manage` | NO | - |
| GET | `/api/v1/floor-plans/{floor_plan_id}/import-jobs` | `floor_plan:read` | NO | - |
| GET | `/api/v1/floor-plans/{floor_plan_id}/objects` | `floor_plan:read` | NO | - |
| POST | `/api/v1/floor-plans/{floor_plan_id}/upload` | `floor_plan:import` | NO | - |
| GET | `/api/v1/spatial/rooms/{room_id}/view` | `spatial:read` | NO | - |

## Power / capacity

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/power/capacity-exceptions` | `capacity:read` | NO | - |
| GET | `/api/v1/power/connections` | `power:read` | NO | - |
| POST | `/api/v1/power/connections` | `power:manage` | NO | - |
| PATCH | `/api/v1/power/connections/{connection_id}` | `power:manage` | NO | - |
| POST | `/api/v1/power/connections/{connection_id}/disconnect` | `power:manage` | NO | - |
| POST | `/api/v1/power/equipment-feeds` | `power:manage` | NO | - |
| GET | `/api/v1/power/equipment/{equipment_asset_id}/power-summary` | `power:read` | NO | - |
| POST | `/api/v1/power/generators` | `power:manage` | NO | - |
| GET | `/api/v1/power/nodes` | `power:read` | NO | - |
| GET | `/api/v1/power/nodes/{node_id}` | `power:read` | NO | - |
| GET | `/api/v1/power/nodes/{node_id}/capacity` | `capacity:read` | NO | - |
| PUT | `/api/v1/power/nodes/{node_id}/capacity` | `capacity:manage` | NO | - |
| GET | `/api/v1/power/nodes/{node_id}/downstream` | `power:read` | NO | - |
| POST | `/api/v1/power/nodes/{node_id}/retire` | `power:manage` | NO | - |
| GET | `/api/v1/power/nodes/{node_id}/upstream` | `power:read` | NO | - |
| POST | `/api/v1/power/pdu-outlets` | `power:manage` | NO | - |
| POST | `/api/v1/power/pdus` | `power:manage` | NO | - |
| POST | `/api/v1/power/power-panels` | `power:manage` | NO | - |
| POST | `/api/v1/power/upses` | `power:manage` | NO | - |
| POST | `/api/v1/power/utility-intakes` | `power:manage` | NO | - |

## Telemetry

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/settings/monitoring` | `telemetry:read` | NO | - |
| PUT | `/api/v1/settings/monitoring` | `telemetry:manage` | NO | - |
| GET | `/api/v1/telemetry/bindings` | `telemetry:read` | NO | - |
| POST | `/api/v1/telemetry/bindings` | `telemetry:manage` | NO | - |
| GET | `/api/v1/telemetry/history` | `telemetry:read` | NO | - |
| GET | `/api/v1/telemetry/latest` | `telemetry:read` | NO | - |
| GET | `/api/v1/telemetry/mappings` | `telemetry:read` | NO | - |
| POST | `/api/v1/telemetry/mappings` | `telemetry:manage` | NO | - |
| GET | `/api/v1/telemetry/metric-registry` | `telemetry:read` | NO | - |
| POST | `/api/v1/telemetry/port-status/ingest` | `telemetry:manage` | NO | - |
| GET | `/api/v1/telemetry/port-status/latest` | `telemetry:read` | NO | - |

## Alarms / events

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/alarms` | `alarm:read` | NO | - |
| GET | `/api/v1/alarms/history` | `alarm:read` | NO | - |
| POST | `/api/v1/alarms/rules` | `alarm:manage` | NO | - |
| POST | `/api/v1/alarms/{alarm_id}/acknowledge` | `alarm:manage` | NO | - |

## Dashboard

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/dashboard/exceptions` | `dashboard:read` | NO | - |
| GET | `/api/v1/dashboard/summary` | `dashboard:read` | NO | - |

## Impact simulation

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| POST | `/api/v1/impact/simulate` | `power:read` | NO | - |

## Collectors

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/collectors` | `collector:read` | NO | - |
| POST | `/api/v1/collectors` | `collector:manage` | NO | - |
| GET | `/api/v1/collectors/{collector_id}` | `collector:read` | NO | - |
| POST | `/api/v1/collectors/{collector_id}/assignments` | `collector:assign` | NO | - |
| GET | `/api/v1/collectors/{collector_id}/capabilities` | `collector:read` | NO | - |
| POST | `/api/v1/collectors/{collector_id}/capabilities` | `collector:manage` | NO | - |
| POST | `/api/v1/collectors/{collector_id}/heartbeat` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/collectors/{collector_id}/ingest` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/collectors/{collector_id}/poll-now` | `collector:manage` | NO | - |
| POST | `/api/v1/collectors/{collector_id}/telemetry` | `(none/auth-only)` | n/a | - |

## Integrations

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/integrations` | `integration:read` | NO | - |
| POST | `/api/v1/integrations` | `integration:manage` | NO | - |
| GET | `/api/v1/integrations/{integration_id}` | `integration:read` | NO | - |
| PATCH | `/api/v1/integrations/{integration_id}` | `integration:manage` | NO | - |

## Discovery

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/discovery/devices` | `discovery:read` | NO | - |
| GET | `/api/v1/discovery/reconciliation` | `discovery:read` | NO | - |
| POST | `/api/v1/discovery/reconciliation/{diff_id}/accept` | `discovery:reconcile` | NO | - |
| POST | `/api/v1/discovery/reconciliation/{diff_id}/reject` | `discovery:reconcile` | NO | - |

## Bulk import

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/import-jobs/{job_id}` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/import-jobs/{job_id}/cancel` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/import-jobs/{job_id}/commit` | `(none/auth-only)` | n/a | - |
| GET | `/api/v1/import-jobs/{job_id}/report` | `(none/auth-only)` | n/a | - |
| GET | `/api/v1/import-jobs/{job_id}/rows` | `(none/auth-only)` | n/a | - |

## Catalog

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| POST | `/api/v1/catalog/documents` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/documents/{document_id}` | `catalog:read_draft` | NO | - |
| GET | `/api/v1/catalog/documents/{document_id}/extraction-jobs` | `catalog:document_download` | NO | - |
| POST | `/api/v1/catalog/documents/{document_id}/extraction-jobs` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/documents/{document_id}/file` | `catalog:document_download` | NO | - |
| POST | `/api/v1/catalog/extraction-candidates/{candidate_id}/review` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/extraction-jobs/{job_id}` | `catalog:document_download` | NO | - |
| GET | `/api/v1/catalog/extraction-jobs/{job_id}/candidates` | `catalog:document_download` | NO | - |
| POST | `/api/v1/catalog/extraction-jobs/{job_id}/retry` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/import-jobs` | `catalog:import` | NO | - |
| GET | `/api/v1/catalog/import-template` | `catalog:read` | NO | - |
| GET | `/api/v1/catalog/manufacturers` | `catalog:read` | NO | - |
| POST | `/api/v1/catalog/manufacturers` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/manufacturers/{manufacturer_id}` | `catalog:read` | NO | - |
| GET | `/api/v1/catalog/models` | `catalog:read` | NO | - |
| POST | `/api/v1/catalog/models` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/models/{model_id}` | `catalog:read` | NO | - |
| PATCH | `/api/v1/catalog/models/{model_id}` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/models/{model_id}/documents` | `catalog:read_draft` | NO | - |
| POST | `/api/v1/catalog/models/{model_id}/revisions` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/models/{model_id}/revisions/clone` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/revisions/compare` | `(none/auth-only)` | n/a | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/revisions/{revision_id}` | `(none/auth-only)` | n/a | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/revisions/{revision_id}/documents` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/documents` | `catalog:manage` | NO | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}/documents/{document_id}` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/documents/{document_id}` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/revisions/{revision_id}/extraction-applications` | `catalog:document_download` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/extraction-applications` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/graphics/{graphic_id}/markers` | `catalog:manage` | NO | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}/graphics/{graphic_id}/markers/{marker_id}` | `catalog:manage` | NO | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}/graphics/{graphic_id}/markers/{marker_id}` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/graphics/{side}` | `catalog:manage` | NO | - |
| GET | `/api/v1/catalog/revisions/{revision_id}/graphics/{side}/file` | `(none/auth-only)` | n/a | - |
| GET | `/api/v1/catalog/revisions/{revision_id}/graphics/{side}/thumbnail` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/monitoring-metrics` | `catalog:manage` | NO | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}/monitoring-metrics/{metric_id}` | `catalog:manage` | NO | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}/monitoring-metrics/{metric_id}` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/network-ports` | `catalog:manage` | NO | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}/network-ports/{port_id}` | `catalog:manage` | NO | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}/network-ports/{port_id}` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/power-supplies` | `catalog:manage` | NO | - |
| DELETE | `/api/v1/catalog/revisions/{revision_id}/power-supplies/{psu_id}` | `catalog:manage` | NO | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}/power-supplies/{psu_id}` | `catalog:manage` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/publish` | `catalog:publish` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/retire` | `catalog:retire` | NO | - |
| PATCH | `/api/v1/catalog/revisions/{revision_id}/retire-override` | `catalog:retire` | NO | - |
| POST | `/api/v1/catalog/revisions/{revision_id}/validate` | `catalog:read_draft` | NO | - |
| GET | `/api/v1/equipment-models` | `equipment:read` | yes | - |
| POST | `/api/v1/equipment-models` | `catalog:manage` | NO | - |
| GET | `/api/v1/equipment-models/{equipment_model_id}/revisions` | `equipment:read` | yes | - |
| POST | `/api/v1/equipment-models/{equipment_model_id}/revisions` | `catalog:manage` | NO | - |
| GET | `/api/v1/rack-models` | `rack:read` | yes | - |
| POST | `/api/v1/rack-models` | `catalog:manage` | NO | - |
| GET | `/api/v1/rack-models/{rack_model_id}/revisions` | `rack:read` | yes | - |
| POST | `/api/v1/rack-models/{rack_model_id}/revisions` | `catalog:manage` | NO | - |

## User / group admin

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/groups` | `group:read` | yes | ctx.scope |
| POST | `/api/v1/groups` | `group:manage` | yes | - |
| GET | `/api/v1/groups/permission-catalog` | `group:read` | yes | - |
| DELETE | `/api/v1/groups/{group_id}` | `group:manage` | yes | - |
| GET | `/api/v1/groups/{group_id}` | `group:read` | yes | - |
| PATCH | `/api/v1/groups/{group_id}` | `group:manage` | yes | - |
| PUT | `/api/v1/groups/{group_id}/members` | `group:manage` | yes | ctx.scope |
| PUT | `/api/v1/groups/{group_id}/permissions` | `group:manage` | yes | - |
| PUT | `/api/v1/groups/{group_id}/site-access` | `group:manage` | yes | scope= |
| GET | `/api/v1/users` | `user:read` | yes | .scope,ctx.scope |
| POST | `/api/v1/users` | `user:manage` | yes | - |
| DELETE | `/api/v1/users/{user_id}` | `user:manage` | yes | - |
| GET | `/api/v1/users/{user_id}` | `user:read` | yes | - |
| PATCH | `/api/v1/users/{user_id}` | `user:manage` | yes | - |
| GET | `/api/v1/users/{user_id}/effective-access` | `user:read` | yes | .scope,ctx.scope,scope= |

## Auth

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| POST | `/api/v1/auth/login` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/auth/logout` | `(none/auth-only)` | n/a | - |
| GET | `/api/v1/auth/me` | `(none/auth-only)` | n/a | - |
| POST | `/api/v1/auth/refresh` | `(none/auth-only)` | n/a | - |

## Health

| Method | Path | Permission | Active for restricted? | Scope guard |
|---|---|---|---|---|
| GET | `/api/v1/health/live` | `(none/auth-only)` | n/a | - |
| GET | `/api/v1/health/ready` | `(none/auth-only)` | n/a | - |
