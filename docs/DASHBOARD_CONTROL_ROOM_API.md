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

## `GET /api/v1/dashboard/geography`

Permission: `location:read` (scope-aware). Visibility uses `country_visible_clause`,
`city_visible_clause` and `site_visible_clause`; hidden entities are never counted.

Response `GeographyOut`

```json
{
  "totals": { "countries": 0, "cities": 0, "sites": 0, "sites_with_coordinates": 0, "sites_without_coordinates": 0 },
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

## `GET /api/v1/dashboard/exceptions` (unchanged)

Still returns every exception unbounded. The dashboard shows a compact preview with severity counts and an
explicit "N more" link; the full list lives in **Events → Infrastructure**.

## Frontend contract

- `/` Dashboard: header + freshness, alarm panel (counters, severity filter, acknowledged toggle, 7 rows),
  geographic coverage panel, Events & exceptions preview, collectors, inventory/rack/power capacity.
- `/events?view=infrastructure` opens the Infrastructure tab. Other existing Events tabs are unchanged.
- The map is SVG, dependency-free and tile-free; a keyboard-reachable list with the same sites is always
  rendered. No external tile provider is contacted, so there is no tile-failure mode to recover from.
