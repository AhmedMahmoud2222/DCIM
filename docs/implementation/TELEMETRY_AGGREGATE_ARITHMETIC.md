# Daily telemetry aggregate arithmetic (Issue #128 / G4-A)

Scope: how `compact_eligible_raw` and `merge_late_reading` compute `daily_telemetry_aggregate` rows. Out of scope and
unchanged: retention cutoffs and history reads (G2), replay/dedup of consumed readings (G4-R, design only), evidence
lifetime (G6), API trust labels (G7). Nothing in this repository schedules compaction.

## Contract

Every stored sample is a `NUMERIC(18, 8)`, an exact integer multiple of 1e-8. Aggregates therefore use:

| Quantity | Computation |
|---|---|
| sum | Python integer count of 1e-8 units, stored as `sum_value NUMERIC(38, 8)` |
| count | `sample_count` (INTEGER, at most 2,147,483,647) |
| min / max | `Decimal` comparison, no arithmetic |
| average | `round_half_even(sum / count)` to 8 decimals, one division, symmetric about zero |

The arithmetic uses integers only (`app/domain/telemetry/aggregate_math.py`). It does not depend on the process-wide
`decimal` context, which a test proves by running with a 6-digit, round-down context. For any set of samples the sum,
count, min, max and average are identical however the samples were batched, ordered or merged.

## `arithmetic_basis`

| Value | Meaning |
|---|---|
| `NULL` | Computed before G4-A with float arithmetic. `sum_value` is NULL. Precision unverified. Never rewritten. |
| `decimal_exact` | Built only from stored `Decimal` samples. `sum_value` is the exact sum. |
| `legacy_seeded` | A `NULL` row that later received samples. `sum_value` was seeded as `average_value * sample_count` from the old rounded average, then every later sample was added exactly. `legacy_sample_count` is the count at seeding time and never changes. |

A legacy row is seeded on its first fold (late merge or compaction). No numeric error bound is claimed for legacy rows:
a row that already absorbed several float merges may carry more than the 5e-9 per-sample rounding of one compaction, and the
merge count was not recorded. Seeding stops further drift; it does not repair what was stored. Compacted days cannot be
recomputed because their raw samples are deleted, so migration 0048 backfills nothing.

## Constraints (migration 0048, added NOT VALID then VALIDATEd)

- `arithmetic_basis` is NULL, `decimal_exact` or `legacy_seeded`.
- `arithmetic_basis IS NULL` exactly when `sum_value IS NULL`.
- `legacy_sample_count` is set exactly for `legacy_seeded` rows and lies between 1 and `sample_count`.
- For `decimal_exact` rows, `count * min <= sum <= count * max`.

Downgrade refuses while any row has a `sum_value`.

## Failure behaviour

If a sum would exceed `NUMERIC(38, 8)` or the count would exceed the INTEGER column, `AggregateArithmeticError` is raised
before any row is changed. The caller-owned transaction aborts, so raw readings are never deleted on failure and the
aggregate is unchanged. In the telemetry route the record's savepoint is discarded and the record is acknowledged as
`rejected` with `AGGREGATE_ARITHMETIC_LIMIT`. That code is not in the collector's permanent set, so a conforming collector
keeps the record queued; valid peers in the batch persist. Both limits are far beyond realistic data (the count limit needs
2.1 billion samples in one series and day).

## Locking

Raw rows stay locked `FOR UPDATE SKIP LOCKED`, then the aggregate is read `FOR UPDATE` with `populate_existing`. Without
`populate_existing` a session that had already loaded the aggregate would overwrite a concurrent writer's sum with its own
stale copy (the race test fails with a lost update when the option is removed). Late merge locks the aggregate only.

## What this does not fix

A reading that is redelivered after compaction or a late merge has already deleted its row is still counted again, because the
dedup key lives only on `telemetry_reading`. G4-A makes the sum exact for the samples it is given; it does not make the sample
set duplicate-free. That is G4-R, which is design only. Until it exists, do not schedule compaction.
