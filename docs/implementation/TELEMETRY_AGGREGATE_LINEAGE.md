# G6 aggregate lineage and source evidence lifetime (Draft)

Depends on merged G4-R #137/main bd12390544164132df90002c3ea40119198db2cb.
Migration 0051 follows 0049 on this isolated draft. G8 has independent migration
0050 also following 0049. These are sibling drafts: integration must reparent the
later migration onto the accepted earlier chain and re-run single-head/migration
checks. Neither draft may be merged just to unblock implementation.

Raw retention is the existing individual-source-evidence expiry policy. Actual
expiry occurs when compaction or a late merge transfers a sample into a daily
aggregate and deletes its raw row, atomically. Changing retention does not restore
already consumed source values. Original numerals, values and attributes are not
copied to lineage. No raw archive, purge job, compaction schedule or destructive
aggregate retention is introduced. No production replay horizon is changed.

Lineage retains revision id, evidence class, source unit/scale, count and earliest/
latest contributing event timestamps for the lifetime of its aggregate. A hashed,
normalized contract tuple prevents NULL/empty unit collisions and makes scale
1 and 1.00000000 identical. Revision references RESTRICT deletion; deleting a daily
aggregate cascades its lineage. Multiple revision ids alone do not mean different
evidence classes: two pinned revisions remain class pinned, visibly separate in
lineage. Classification is not certification of a sensor or historical precision.

`lineage_sample_count` is NULL on prelineage aggregates and is never backfilled.
Coverage is none for NULL, partial for a positive count below sample_count, and
complete only when counts match. New contributions to prelineage aggregates have
partial coverage; historic unknown contributions participate in summary evidence
class as unverified. `first_compacted_at` means the first consumption observed
under G6, not the unknown original compaction time of a historic aggregate.
`raw_retention_days_at_compaction` records that first observed policy; it does not
assert the same policy governed all later folds.

Compaction first claims G4-R keys and selects only freshly claimed readings. A
locked existing aggregate (or unique-index-protected new aggregate), its exact
arithmetic, lineage upserts, ledger claims and consumed raw deletion share the
caller transaction. Duplicate deliveries add no lineage. First-aggregate contention
still requires caller rollback/retry; failed lineage persistence rolls claims and
arithmetic back too. Lineage counts are checked before committing.

Replay tombstones are a separate evidence tier: hashed deduplication identity,
without values or individual provenance. H remains NULL by default, so ledger
storage grows without pruning. Existing finite-H mechanics are unchanged. Neither
tombstones nor lineage can reconstruct deleted raw evidence; pre-ledger and
prelineage gaps remain unknown.

The nullable `resolved_hold_retention_days` policy defaults to 365 days after
resolution. Purging resolved/expired holds is deferred; the setting does not activate
expiry or delete holds. Aggregate retention deletion and archive policy require
separate owner approval. Restore aggregate, lineage, revisions, raw and replay
ledger consistently as one database snapshot; partial-table restores are unsupported.

Upgrade is additive and never backfills history. Nullable aggregate columns have
no defaults; checks are added NOT VALID, then validated after releasing the metadata
DDL's exclusive locks. Every pre-commit step is rerunnable after an interrupted
upgrade. Downgrade locks the evidence tables and refuses when lineage or summary
evidence exists; operational rollback stops writes while retaining schema.

Representative production profiling is blocked pending an authorized reachable
source. Test fixtures and storage estimates are synthetic, never production evidence.


## Local validation, 2026-10-12

- `ruff check app tests migrations/versions/0051_aggregate_lineage.py`: passed.
- `mypy app`: passed, 241 source files.
- `python scripts/check_alembic_single_head.py`: passed; sole head `0051_aggregate_lineage`.
- Pure domain/contract tests: 14 passed from an unchanged temporary copy outside the
  repository's database-autouse test harness. This does not substitute for normal CI.
- PostgreSQL integration tests: 16 collected (12 G6 plus four existing G4 migration
  regressions). PostgreSQL execution remains pending: the configured localhost:5432
  database refused connection. Collection is not a migration/concurrency pass.
- The G4 migration regressions assert the original four named constraints and named
  NULL arithmetic columns; later lineage columns cannot obscure those assertions.

Pending before publication/acceptance: run fresh and populated PostgreSQL migrations,
interrupted-upgrade/DDL-lock probes, guarded downgrade, replay/compaction races and
rollback tests; full CI; deployment validation; independent review of the exact signed
published HEAD. No remote CI, signed commit or Draft PR publication is claimed here.
