# Dashboard Control Room API contract (Issue #365)

Status: implementation contract for UI-DASH-02. Source of truth is the code once merged; this
document records the intent, the shapes and the assumptions the tests pin.

## Principles

- Every number shown on the dashboard is computed on the server. The browser never derives a
  global count from a truncated list.
- Alarm **severity** (`critical | warning | info`) is a property of the alarm and is independent of its
  **workflow status** (`ACTIVE | ACKNOWLEDGED | CLEARED`). No endpoint conflates them.
- Infrastructure exceptions (`GET /dashboard/exceptions`) are derived, not persisted, and are never
  reclassified as alarms.
- Scope: `alarm:read` and `dashboard:read` are not scope-aware permissions, so a site-restricted user is
  denied (403) today. The new queries nevertheless apply the caller's `AccessScope` as a second layer, so
  they stay correct if those permissions ever become scope-aware. `location:read` is scope-aware.

## `GET /api/v1/alarms/overview`

Permission: `alarm:read`.

Query parameters

| name | type | default | notes |
| --- | --- | --- | --- |
| `severity` | `critical \| warning \| info` | absent (= all) | Filters the **list** only. Never changes `counts`. Any other value is 422. |
| `include_acknowledged` | bool | `false` | `false` = status `ACTIVE`; `true` = `ACTIVE` and `ACKNOWLEDGED`. `CLEARED` is never included. |
| `limit` | int 1..50 | `7` | Size of `items`. The dashboard always uses the default. |

Response `AlarmOverviewOut`

```json
{
  "scope": { "include_acknowledged": false, "severity": null },
  "counts": { "critical": 0, "warning": 0, "info": 0, "total": 0 },
  "items": [ /* AlarmOut, at most `limit` */ ],
  "matching_total": 0,
  "limit": 7,
  "truncated": false,
  "as_of": "2026-11-18T10:00:00Z"
}
```

- `counts` are exact `COUNT(*)` totals over the whole table for the current acknowledgement scope,
  grouped by severity, independent of `severity` and of `limit`. `total` is their sum.
- `matching_total` is the exact number of alarms matching severity **and** acknowledgement scope;
  `truncated` is `matching_total > len(items)`.
- `items` are filtered in SQL **before** the limit and ordered `opened_at DESC, id DESC`.
- `items` reuse the existing `AlarmOut` shape (severity and status remain separate fields).
- Counts and items are read in one transaction snapshot so they cannot disagree with each other.

`AlarmOut.managed_asset_type` (additive, nullable)

- Every endpoint returning `AlarmOut` (`GET /alarms`, `GET /alarms/history`, `GET /alarms/overview`,
  `POST /alarms/{id}/acknowledge`) now includes `managed_asset_type: string | null`, the `ManagedAsset.asset_type`
  of the alarm's managed asset (`rack`, `equipment`, `pdu`, `ups`, `generator`, `power_panel`, `sensor`, `cable`,
  `crac`, `crah`, `chiller`). It is `null` when the alarm has no managed asset or the asset no longer exists.
- It is resolved with one extra query per response over the managed-asset ids of the alarms already returned,
  so it adds no rows, changes no counts, ordering or limits, and follows exactly the same visibility as the
  alarm itself. Older clients that ignore the field are unaffected.
- Link rules in the UI (dashboard alarm rows and the Events detail panel): `equipment` links to
  `/equipment/<id>` when the user holds `equipment:read`; `rack` links to `/racks/<id>` when the user holds
  `rack:read`. Any other type, a missing or unknown type, or a missing permission shows a non-clickable reference
  (for example "Rack asset 1a2b3c4d" or "Asset 1a2b3c4d"); the UI never guesses a route. An alarm without a
  managed asset shows no asset reference.

## `GET /api/v1/dashboard/geography`

Permission: `location:read` (scope-aware). Visibility uses `country_visible_clause`,
`city_visible_clause` and `site_visible_clause`; hidden entities are never counted.

Response `GeographyOut`

```json
{
  "totals": { "countries": 0, "cities": 0, "sites": 0, "sites_with_coordinates": 0, "sites_without_coordinates": 0 },
  "coordinates": "included",
  "countries": [ { "id": "uuid", "name": "", "iso_code": null, "city_count": 0, "site_count": 0 } ],
  "cities":    [ { "id": "uuid", "country_id": "uuid", "name": "", "site_count": 0 } ],
  "sites":     [ { "id": "uuid", "code": "", "name": "", "city_id": "uuid", "latitude": null, "longitude": null } ],
  "sites_truncated": false,
  "as_of": "2026-11-18T10:00:00Z"
}
```

- `latitude`/`longitude` are returned only when both are present and within `[-90, 90]` / `[-180, 180]`;
  otherwise both are `null` and the site counts under `sites_without_coordinates`. Coordinates are never
  inferred, defaulted or geocoded.
- Counts are `COUNT(DISTINCT)` over visible rows. A city with several sites counts once.
- `sites` is bounded (`MAX_GEOGRAPHY_SITES = 2000`, ordered by country name, city name, site code, id);
  `totals` stay exact and `sites_truncated` says when the list was cut.

Precise coordinates (`coordinates: "included" | "withheld"`)

- Site coordinates are confidential operational data. They are `"included"` only when the caller's scope is
  unrestricted (global access) or the deployment sets `GEOGRAPHY_RESTRICTED_COORDINATES_ENABLED=true`
  (setting `geography_restricted_coordinates_enabled`, default `false`). Enabling it is an explicit operator
  authorization and requires the owner's approval.
- Default for restricted callers is `"withheld"`: every site has `latitude: null, longitude: null`, and
  `totals.sites_with_coordinates` / `totals.sites_without_coordinates` are `null` (the counts would disclose
  how many sites have coordinates). The coordinate columns are not selected from the database at all in
  that case. Everything else (scope filtering, the other totals, lists, `iso_code`, ordering, truncation) is
  unchanged, and the setting never widens which sites, cities or countries are returned.
- Alternative (owner option, not implemented): a dedicated role-based permission for precise coordinates,
  giving per-role control instead of one deployment-wide switch. It needs a permission-catalog change.
- The API never serves basemap files and has no map-upload endpoint. The basemap is static, public reference
  geometry shipped with the frontend and holds no DCIM data; see [OFFLINE_MAP_DATA.md](OFFLINE_MAP_DATA.md).

## `GET /api/v1/dashboard/exceptions` (unchanged)

Still returns every exception unbounded. The dashboard shows a compact preview with severity counts and an
explicit "N more" link; the full list lives in **Events → Infrastructure**.

## Frontend contract

- `/` Dashboard: header + freshness, alarm panel (counters, severity filter, acknowledged toggle, 7 rows),
  geographic coverage panel, Events & exceptions preview, collectors, inventory/rack/power capacity.
- `/events?view=infrastructure` opens the Infrastructure tab. Other existing Events tabs are unchanged.
- The map is SVG, dependency-free and tile-free; a keyboard-reachable list with the same sites is always
  rendered. Country boundaries come from a locally packaged basemap ([OFFLINE_MAP_DATA.md](OFFLINE_MAP_DATA.md));
  no external tile, CDN or geocoding provider is contacted.

## Owner decisions required (not made in this PR)

1. **Site-restricted access to alarms and the dashboard.** `alarm:read` and `dashboard:read` are not in
   `SCOPE_AWARE_PERMISSIONS`, so a site-restricted user receives 403 on `/alarms/overview`, `/dashboard/summary`
   and `/dashboard/exceptions` (the dashboard then shows each source as "Access restricted", never as zero).
   `alarm_visible_clause` is already in place and tested, so making `alarm:read` scope-aware is mostly a policy
   decision: add the permission to `SCOPE_AWARE_PERMISSIONS`, add its routes to the reviewed inventory in
   `tests/api/test_user_groups_authz.py`, and decide how alarms with no managed asset are treated for restricted
   callers (today: hidden). `dashboard:read` summary and exceptions are computed globally (power graph) and
   would need their own scoping work, which is out of scope here.
2. **Index for the alarm overview.** The overview filters on `alarm.status` and orders by `opened_at DESC, id DESC`;
   the existing `ix_alarm_integration_status_opened` starts with `integration_id`, so it does not serve this query
   and large alarm tables may be scanned. A partial index such as
   `(opened_at DESC, id DESC) WHERE status IN ('ACTIVE','ACKNOWLEDGED')` would serve it. It needs an Alembic
   migration, which is held with 0057 under Issue #214, so none is added. Measure on representative data first.

   Follow-up record (no migration in this PR):
   - Proposed index: partial index on `alarm (opened_at DESC, id DESC) WHERE status IN ('ACTIVE','ACKNOWLEDGED')`.
   - Validation plan: on a representative-size alarm table, capture `EXPLAIN (ANALYZE, BUFFERS)` of the overview
     statement (both `include_acknowledged` values, with and without `severity`) before and after the index;
     compare the plans (index scan instead of a sequential scan plus sort, buffers read, timing); and measure the
     write overhead the index adds on alarm open, acknowledge and clear.
   - Sequencing: it must wait until the migration-0057 hold under Issue #214 is lifted, and then ships as its own
     reviewed Alembic migration.
3. **Site coordinates for restricted roles.** Withholding is now the default: `GET /dashboard/geography` returns
   precise coordinates only to callers with an unrestricted scope, and restricted callers get `coordinates:
   "withheld"` (the map then highlights countries and the list shows every authorized site). Returning
   coordinates to restricted roles is the operator's explicit decision via
   `GEOGRAPHY_RESTRICTED_COORDINATES_ENABLED`; per-role control would need a dedicated permission (see above).

## Known CI limitation

`frontend-suite` runs `npm audit --audit-level=high` before Vitest, typecheck, lint and build. That step fails on
the shared lockfile (picomatch GHSA-fc5q-39xc-vjpf, no patched release; Issue #364 / PR #366) independently of this
change, which modifies no dependency file. Dependency and audit-policy changes are under owner hold in #364.
