# Issue #102: protection, capacity rollups, history, forecasting and reports

Document version: 2 (implementation record; version 1 was the planning note)

Base: `main` at `dd22830` (merged into this branch), migration head `0042_power_protection_reports`.

Everything extends the one existing power graph (`PowerNode`, `PowerConnection`, `PowerCapacity`). There is no
second topology.

## A. Protection devices

A protection device is a `PowerNode` with `node_type = 'protection_device'`. Its extension row
(`protection_device`) holds site, type (`breaker`, `fuse`, `switch`, `disconnect`), rating (A), voltage (V),
poles, phase configuration, `state` (`closed`, `open`, `tripped`, `unknown`), `status` (`in_service`,
`maintenance`, `out_of_service`) and a version for `If-Match`. `owning_asset_id` is the generator, UPS, panel or
PDU that houses it. Upstream and downstream links are ordinary `PowerConnection` rows, so cycle checks, the
advisory mutation lock, traversal and impact simulation apply unchanged.

Validation (`app/application/power_protection.py`, database CHECK constraints behind it):

| Rule | Where |
| --- | --- |
| Rating 0 to 6300 A, voltage 24 to 1000 V, poles match phase (single: 1 or 2, three: 3) | schema CHECK and API 422 |
| Housing asset exists and sits in `site_id` when its site is known | create |
| A link may not join two different known sites | connection create and update |
| Connection rated current may not exceed the device rating; connection phase must match; voltage within 50% | connection create and update |
| Lowering a rating or changing phase below an attached connection is refused | device update |
| Self links and cycles | existing connection checks |
| Retired devices cannot change state | state endpoint |

Site resolution: generators carry a site, UPSs and panels a room, PDUs and equipment a current placement.
Nodes with no resolvable site (utility intake, unplaced PDUs) are treated as unknown and never as "any site".

Endpoints (`/api/v1/power/protection-devices`): `POST`, `GET` (list, filter by site and state), `GET /{id}`,
`PATCH /{id}` (If-Match), `POST /{id}/state` (If-Match). Retire uses the existing `POST /power/nodes/{id}/retire`.
Every mutation writes an audit log row and an outbox event. Recording a state changes the record only; nothing is
switched in the field.

## B. Rollups and A/B rules

`app/application/power_rollup.py` is a pure function. `power_rollup_loader.py` builds its inputs for one site.

Quantities: `rated_kw` (nameplate; device rating for breakers), `capacity_kw` (configured, else rated), `load_kw`
(measured, last-known when stale, nameplate estimate when never measured), `allocated_kw` (nameplate demand, A/B
by max as in `equipment_power_summary`), `headroom_kw` (capacity minus load, negative kept).

An equipment item has one demand. It splits over its live inlets (50/50, or proportional to inlet capacity when
the inlets differ) and each share flows up the graph, splitting again over live parents. Racks, rooms and sites
add each served item once. Scenarios:

| Scenario | Meaning | Load placement |
| --- | --- | --- |
| `normal_dual_feed` | both inlets live, equal capacity | half on each side |
| `asymmetric_feeds` | both live, capacities differ | proportional to inlet capacity |
| `one_feed_failed` | a path exists but an open or tripped device, out-of-service device, retired node or non-active connection cuts it | full demand on the survivor |
| `one_feed_missing` | the second inlet has no active upstream connection | full demand on the connected feed |
| `single_feed` | one inlet only | full demand on it |
| `unserved` | no live inlet | reported as `unserved_kw`, not as load |
| `no_power_modeled` | no inlets | not counted |

Data quality per node and scope: `measured`, `stale` (last reading older than 15 minutes), `estimated`, `mixed`,
`missing`. `unknown` protection state never interrupts a path and raises a warning. Scope capacity is the sum of
the highest tier present (UPS, else generator, panel, PDU); a 2N tier counts half.

The regression tests (`tests/unit/test_power_rollup.py`, `tests/api/test_power_analytics.py`) assert the site total
equals the server demand once. Removing the split makes three of them fail.

## C. Snapshots

`power_utilization_snapshot` holds one row per closed UTC hour and scope (`site`, `power_node`), unique on
`(granularity, bucket_start, scope_type, scope_id)`. Rows carry metric `power_kw`, unit `kW` (CHECK enforced),
registry version, load, load basis, allocated, capacity, headroom, utilization, sample count, expected samples,
coverage ratio, quality, source window and method version. Readings come from `telemetry_reading` rows the #99
registry already normalised; no conversion code was added. Writes use `INSERT .. ON CONFLICT DO NOTHING`, so a
rerun changes nothing and raw readings are never touched. Scope ids have no foreign key, so history survives
retirement.

The topology used for a bucket is the topology at compute time (`computed_at`). The beat task
`snapshot_power_utilization` runs hourly on the `maintenance` queue with a 3 hour lookback.

## D. Forecast

Method `linear_ols_v1`: least squares over daily means of `measured` hourly snapshots in a 30 day window. States:
`good`, `flat` (including beyond the 365 day horizon), `already_over_capacity`, `stale`, `sparse` (under 7 measured
days or under 50% coverage), `missing`, `estimated_mixed` (over 20% non-measured). Every result carries metric,
unit, current load, capacity, headroom, window, bucket and sample counts, coverage, slope, R squared, exhaustion
date, confidence and, when it declines, a reason. Same snapshots and `now` give the same answer.

## E. Reports

`POST /power/analytics/reports` (`power:manage`) queues a job and dispatches `generate_power_report` to the
`reports` queue (workers in both compose files and the CI E2E job now consume it; a dispatch failure marks the job
failed and returns 502). The worker claims the job with one atomic UPDATE (queued, or running with an expired
lease), builds the report from latest snapshots, the forecast, device states and rollup scenarios, and stores
bounded JSON on the row. CSV is rendered from that JSON with formula characters neutralised. Failure codes are
fixed (`GENERATION_FAILED`, `RESULT_TOO_LARGE`, `ATTEMPTS_EXHAUSTED`); exception text is never stored or returned.
A beat task re-dispatches stuck jobs. Sections: utilization, exceptions and overloads, redundancy, protection state,
headroom, forecast risk, data quality.

Jobs belong to their requester. Another user's job answers 404 on read, download and list.

## F. Failure impact

`simulate_power_node_failure` stays read-only and downstream-only. A surviving feed that an open, tripped or
out-of-service protection device already cuts off no longer counts as a survivor, so the result becomes
`power_loss` and the message names the blocking device. `unknown` is not a cut. A feed with no evidence of
interruption still counts, which keeps earlier behaviour for unmodeled paths.

## G. UI

`/power/analytics` (linked from the topology page): capacity and data quality, protection state with a per-device
state control, history with forecast status, report generation, polling and download. Tabs use the tablist pattern,
tables have captions and header scope, errors use `role="alert"`, quality is text not colour alone.

## H. Authorization

All endpoints use `power:read` (reads) or `power:manage` (mutations and report creation). Those codes are not
scope-aware, so a site-restricted user holds neither and every endpoint answers 403 for existing and missing ids
alike. No permission codes were added. `tests/api/test_power_analytics.py` covers anonymous, read-only, restricted
and cross-user access, direct IDs, report enumeration and download.

## I. Migration 0042

Additive. Replaces the `power_node.node_type` CHECK (adds `protection_device`) and creates the three tables with
their constraints and indexes. Nothing is backfilled, because no existing row maps to a protection device.
Downgrade refuses while any of the three tables hold rows or protection nodes exist, otherwise drops them and
restores the old CHECK. `tests/integration/test_power_protection_migration.py` proves a populated upgrade,
constraint rejection, refused downgrade, clean downgrade and re-upgrade.

## Known limits

* Snapshots use the topology at compute time; a late backfill does not reconstruct historic topology.
* Per-feed measurement does not exist, so the A/B split is a documented assumption (equal, or by inlet capacity).
* The `-Q` string in `tests/api/test_catalog_extraction.py` changed to include `reports`; that test pins the worker
  command and the worker must consume the new queue.
