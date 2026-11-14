# MVP operational UI checkpoint

## Starting point and scope

Starting SHA: `2dcf92b10c2b6ae19ee8d13c8eb752f53f59ac95` on
`codex/dcim-mvp-v0.1-signed`.  This checkpoint reuses the existing React UI and
authoritative API domains; it does not introduce a parallel inventory, telemetry,
alarm, or power model.

## Reused routes and APIs

Existing Dashboard (`/`), rack list/detail/elevation (`/racks`), equipment detail
(`/equipment/:equipmentId`), room floor-plan (`/floor-plans/room/:roomId`), power,
collectors, integrations, and discovery remain the canonical screens.  New
`/infrastructure` and `/sites/:siteId` routes are thin navigators over `/sites` and
`/rooms?site_id=` and lead to the existing room floor-plan and rack/equipment pages.

The dashboard continues to reuse `/dashboard/summary` and `/dashboard/exceptions` and
adds bounded existing alarm reads.  It deliberately links to collector/integration
screens instead of inventing synthetic source-health values.

## ManagedAsset and telemetry

`IntegrationMetricMapping.managed_asset_id` is the authoritative bridge from a
configured source identifier to a `ManagedAsset`.  Ingestion copies that identity into
`TelemetryReading` and its durable series key.  The equipment screen reads telemetry
by `managed_asset_id`; it never matches a hostname, asset tag, or external identifier
in the browser.  Migration `0013_metric_mapping_managed_asset` adds the nullable FK
and index without guessing mappings for historical data.

The migration file is named `0013_metric_mapping_managed_asset.py`; its Alembic
revision is the PostgreSQL-compatible `0013_metric_asset` (the version table's
established `VARCHAR(32)` limit is part of migration compatibility).

## Operations views

Equipment now shows current telemetry, occurred and (where different) received time,
an equipment-scoped raw/daily history feed, ACTIVE/ACKNOWLEDGED/CLEARED alarm history,
and an acknowledgement action backed by the existing `alarm:manage` authorization,
audit, and outbox path.  Rack elevation entries navigate directly to equipment; the
existing power-context panels remain visible on both equipment and racks.

Freshness is calculated in the UI as two times the integration-specific polling
interval supplied by the bounded `/telemetry/latest` query.  It is therefore not a
global five-minute assumption.  A missing interval is presented as timestamped data
rather than falsely labelled current.

History requests are bounded at the existing API limit and display the API-provided
`raw` or `daily` resolution; daily rows expose average/range/sample count.  The current
MVP control provides common ranges through one year.  The API remains capable of a
custom multi-year range; a custom range picker is a follow-up usability limitation.

## Validation and limitations

Local static validation: `python -m compileall -q backend/app` and targeted Ruff
completed.  Local PostgreSQL-dependent tests could not connect because PostgreSQL is
not running in this disposable Windows workspace; authoritative CI is required for
migration and browser build validation.  Local npm is also unavailable, so frontend
typecheck/lint/build are run by the existing GitHub Actions frontend job.

The UI intentionally does not claim a collector outage proves equipment failure.
Collector and integration health remain separate views.  Dashboard alarm counts use a
bounded open-alarm result pending a dedicated aggregate read model if deployments need
counts beyond the current API cap.  The demo remains the existing seeded/simulator path;
this checkpoint adds no production-domain demo shortcut.

## GitHub CI validation

GitHub Actions CI run `35423397272` completed successfully for commit
`5b85849fd410d8e92652e0666d15a3f5ef497c99`.  Its PostgreSQL 16 backend job executed
`alembic upgrade head`, confirmed exactly one Alembic head, and completed the migration
round trip `head -> 0010_mvp_alarms -> head`; this explicitly downgraded
`0013_metric_asset` to `0012_retention_series_identity` and re-upgraded it.

Executed results: Ruff passed, mypy passed for 99 source files, PostgreSQL retention
validation passed (6 tests), and the remaining backend suite passed (531 tests, 14
warnings).  The frontend job passed TypeScript typecheck, ESLint, and production build.
The only GitHub annotation was the platform Node 20 deprecation notice from GitHub
Actions dependencies; it is not an application test failure.
