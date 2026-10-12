# G7 aggregate provenance — dependent implementation draft

Dependency snapshots: G6 `0051_aggregate_lineage` and PR #141 readings provenance.
Neither dependency is merged or committed by this task. Publication must wait for exact
signed dependency commits so the review diff can exclude these snapshot copies.

Daily history exposes recorded aggregate evidence class, coverage (`none`, `partial`,
`complete`), nullable lineage count, first compaction time and retention days at that
compaction. It keeps reading contract fields null. Complete lineage means recorded
contribution coverage, not sensor certification or surviving source numerals. A nullable
pre-lineage count is preserved. Partial aggregates retain historical uncertainty.

Raw evidence is marked expired only when recorded first-compaction metadata exists;
pre-lineage expiry metadata remains unknown. No evidence expiry date or source numeral
is reconstructed. No raw archival, production horizon, retention setting, aggregate
mutation, migration or replay-ledger behavior is introduced by the G7 layer.

Client charts separate evidence class, coverage and raw evidence status in addition to
the reading grouping keys. Daily points show evidence-loss and incomplete-history labels.
The additive contract exposes summaries only; contributor detail browsing/export and
aggregate-specific filtering remain pending independent review/design confirmation.

Validation: 5 serializer regressions and all 286 frontend tests (34 files) passed; frontend
TypeScript and ESLint passed (two existing fast-refresh export warnings). The PostgreSQL
HTTP regression exercises mixed contract evidence through G4-R compaction and asserts
complete recorded lineage without fabricating a source numeral. Browser fixture coverage
checks complete, partial and pre-lineage labels and evidence expiry. These PostgreSQL
and browser tests have been written but not executed: PostgreSQL/Docker and a Chromium
binary are unavailable here. Full cloud CI, deployment validation, independently reviewed
signed heads and publication remain pending. This implementation is not accepted.

Contributor revision IDs, original source units, source scale and occurrence ranges are
persistent in the G6 `daily_telemetry_aggregate_lineage` table. This G7 history response
exposes the evidence summary and lineage sample count, not that contributor detail table.
Consumers must not infer a unique revision or original source unit from the canonical
aggregate unit or evidence class. A detail API/export and aggregate-specific filters are
explicitly outside this draft; the limitation requires independent review.

Raw groups include their stored mapping revision ID, so even equally pinned
readings cannot combine a min/max range across contract revisions. This is finer
than the minimum series/status grouping requirement and changes no conversion.
Daily grouping uses the aggregate summary and does not invent a revision ID.
