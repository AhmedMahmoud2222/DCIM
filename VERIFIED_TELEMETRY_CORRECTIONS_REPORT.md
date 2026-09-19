# Verified Telemetry Corrections

## Scope

This checkpoint is intentionally limited to late-reading retention integrity and
historical `ManagedAsset` association.  It does not change the previously corrected
`/telemetry/latest` query, the Edge queue fail-closed policy, alarm cursors, power
traversal, or REST target/redirect policy.

## Late-reading exact-once behavior

`merge_late_reading()` updates an already locked daily aggregate, but it deliberately
does not own deletion or commit boundaries.  The authoritative caller is
`ingest_reading()`: after a successful merge it deletes the newly ingested raw row in
the same caller-owned transaction.  A rollback restores both the aggregate update and
the raw insert; a commit persists both the aggregate update and raw deletion.

The regression covers an existing aggregate, an ingested late sample, and a subsequent
`compact_eligible_raw()` call.  It verifies exact count/average/minimum/maximum and
that no raw row remains for compaction to count twice.  No production retention change
was necessary because the suspected defect is not present in the actual caller chain.

## Historical association strategy

Migration `0014_telemetry_asset_backfill` is forward-only and conservative.

- Raw readings are attributable only when their immutable `mapping_id` resolves to an
  asset-associated mapping and their stored key proves the prior unmanaged canonical
  identity.  The migration writes both `managed_asset_id` and the matching canonical
  asset `series_key`.
- Daily aggregates lack `mapping_id`; they are attributable only when exactly one
  asset-associated mapping has the same durable integration, canonical metric, and
  unit.  A row that would collide with an existing asset-key/day aggregate remains
  unmanaged rather than being merged or discarded.
- Multiple mapping candidates are **ambiguous**.  No candidate is **unattributable**.
  Both kinds remain intact and unmanaged for global/integration history; they are not
  guessed into asset-scoped history.

The test fixture exercises two deterministically attributable rows (one raw and one
daily), one ambiguous daily row, and two unattributable rows (one raw and one daily).
It reruns the exact migration SQL to prove the null-only predicates are retry-safe and
calls the asset-scoped history API to prove the attributable daily row becomes visible.

`series_key` changes only for deterministically attributed rows because asset identity
is part of the repository's canonical series identity.  Leaving an unmanaged key after
populating `managed_asset_id` would produce two identities for the same historical
series and break latest/retention continuity.  The migration never rewrites a key for
ambiguous or unattributable telemetry.

## Downgrade

The migration's downgrade is intentionally non-destructive: it leaves additive
historical enrichment in place.  Downgrading `0013` can remove the mapping column, but
the telemetry tables already support nullable asset association and retain their
historical evidence.  Re-upgrade is safe because only unmanaged rows are candidates.

## Validation status

Local static checks run in this disposable Windows workspace: Ruff, mypy, Python
compilation, and focused pytest collection.  PostgreSQL was not available locally, so
the PostgreSQL execution of the hostile retention, migration, latest-series, and full
backend suites must be established by the repository CI run for this commit.  The CI
backend service runs PostgreSQL 16 and performs migration round-trip validation.
