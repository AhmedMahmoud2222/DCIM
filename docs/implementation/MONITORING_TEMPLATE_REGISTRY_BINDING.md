# G9b: optional monitoring-template registry binding

Issue #128 design: https://github.com/AhmedMahmoud2222/DCIM/issues/128#issuecomment-6100092674

This scope is an unpublished implementation prepared for a separate **Draft PR**.
It depends on G5 registry-v2 semantics and G10 immutable registry snapshots
(G10 PR #138, published head `3347d375f2197f15ed60a69ee52f844d0ebeb62e`).
The integration baseline is merged G4-R main
`bd12390544164132df90002c3ea40119198db2cb`. No G5/G9b implementation commit
or independent exact-head acceptance exists yet. Do not merge a dependency to
unblock development, retarget another PR, or mark this scope ready.

## Authored contract

Templates may optionally specify `registry_metric`, `registry_version` and
`threshold_unit`. A bound template must name an explicit supported registry
version and compatible source and threshold units. Neither unit text nor a
suggested threshold is rewritten. `binding_status` in API output is `bound` or
`unbound`; it does not claim that a runtime mapping or alarm rule has been seeded.

`scale` represents sensor encoding and must be finite and fit NUMERIC(18,8).
The registry alone performs physical-unit conversion. Bound templates reject
nonzero/nonfinite offsets and any transform other than `none`. Zero offset and
absent offset are identity. The authored threshold unit is required even when
there is no suggested threshold, so a later suggestion cannot inherit an
implicit unit. Existing unbound templates retain their free-text units and
legacy transform behavior.

For example, a sensor sample of 950 with scale 0.1 and source unit degF means
95 degF. A downstream mapping must pass that raw sample, scale, source unit and
pinned registry version to the registry exactly once. A suggestion of 95 degF
is authored in its own unit and must not receive the sensor scale. Both are
35 degC under v1 and v2; G9b itself performs neither conversion.

Create and partial-update routes validate the complete candidate template
under the existing draft revision lock and optimistic version check. Invalid
updates roll back the revision increment. Existing permissions and published
revision immutability still apply. Clone preserves all binding metadata;
revision comparison includes the three binding fields. No mapping seeder,
alarm seeder, template UI editor, physical projection change, or historical
backfill is introduced in this scope. G9a publication policy is independent.

## Migration and rollback

`0052_monitoring_template_binding` adds three nullable columns to
`monitoring_metric_template`, without defaults or backfill. Existing rows stay
unbound. Its current parent is `0049_telemetry_replay_ledger`; this isolated
worktree has exactly one Alembic head, `0052_monitoring_template_binding`.
G8 0050, G6 0051 and the G5 registry snapshot ledger 0053 are sibling Draft migrations. Their final accepted order
must be reconciled before integration; the combined branch must have exactly
one head. The provisional numbering is not evidence that the other migrations
have landed.

Downgrade refuses if **any** of the three binding columns has evidence,
including partial metadata, so rollback cannot silently discard authoring
provenance. It acquires `ACCESS EXCLUSIVE` on the template table **before**
checking evidence, so concurrent authoring cannot commit between the guard
and dropping columns. Empty/unbound rows may downgrade and upgrade without changing
authored units. No production replay horizon or retention setting changes.

## Dependency overlay

Only the following G5/G10 domain files were copied byte-for-byte from the local
G5 dependent worktree. They are prerequisites, not additional G9b scope, and
must be reconciled with the final signed G5 head before publication:

| Dependency file | SHA-256 |
|---|---|
| `backend/app/domain/telemetry/registry.py` | `1a3a71088b76f81f9a6da96ae699f5f716e5322952d489142e6ef2d34450fb6e` |
| `backend/app/domain/telemetry/contract.py` | `b4863414be4914af84ba06b816dbb4c0b8b7e97ed014c78d0c916342bc667830` |
| `backend/app/domain/telemetry/metric_semantics.py` | `3101e39f43e852dd8c486fe721ecedf7cffb731379e3bffaa34ece664418df6b` |
| `backend/app/domain/telemetry/registry_v1.snapshot.txt` | `d29e5d41a996e5b081583e87f8e6a270b4040b682432c16aaad7b0f22f9ac6d4` |
| `backend/app/domain/telemetry/registry_v2.snapshot.txt` | `2f9bd2d613f32baed8322fc244d5f33d74437d0fb10f53bc047fff2ce8e8c147` |

The defaults remain v1. G9b cannot enable v2 ingestion or silently migrate
existing mappings; those decisions and consumer propagation belong to G5.
The copied domain subset is sufficient for local binding tests; the published
dependent branch must include the complete accepted G5 prerequisite, including
its snapshot-ledger migration and startup agreement checks. This worktree's
five-file subset must not be deployed as a standalone v2 activation.

## Validation evidence and remaining gates

Executed locally: 19 pure binding tests and two migration lock-order assertions
pass; binding plus immutable registry and conversion-contract suites total
41 passing tests. Scoped Ruff passes.
Mypy passes for the four G9b source files. Alembic graph inspection finds one
head. Fourteen API/migration/concurrent-edit tests collect successfully.

Actual API/PostgreSQL execution stops during fixture setup because PostgreSQL
at localhost:5432 refuses connections. The fourteen tests have **not passed**.
They cover create/read, invalid edits and transactional rollback, permissions,
unbound compatibility, published cloning, concurrent optimistic edits, empty
and populated unbound migration round trips, evidence-bearing downgrade
refusal, and a concurrent binding writer versus guarded downgrade. Independent
source review identified the downgrade race; the lock correction and contention
test were re-reviewed as resolving that source finding. Runtime PostgreSQL
acceptance and published exact-head acceptance remain pending. Fresh full-chain
PostgreSQL upgrade/downgrade, full backend CI,
deployment validation and independent exact-head review remain required.

Authorized signing and Git push credentials are unavailable in this runtime.
No unsigned implementation commit or publication was attempted. Publish only
in an authorized signing-enabled runtime, as a separate Draft PR with its exact
G5/G10 dependency and the PostgreSQL/CI/review limitations stated explicitly.
