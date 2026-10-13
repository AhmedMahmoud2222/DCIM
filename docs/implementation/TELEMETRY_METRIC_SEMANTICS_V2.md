# Issue 128 G5: explicit registry-v2 semantics (Draft)

Dependency: [G10 Draft PR 138](https://github.com/AhmedMahmoud2222/DCIM/pull/138), published source HEAD `3347d375f2197f15ed60a69ee52f844d0ebeb62e`, integrated with merged G4-R main `bd12390544164132df90002c3ea40119198db2cb` and the coordinated G10 degree-alias remediation. The corrected frozen-v1 hash is `d29e5d41a996e5b081583e87f8e6a270b4040b682432c16aaad7b0f22f9ac6d4`; v2 is `2f9bd2d613f32baed8322fc244d5f33d74437d0fb10f53bc047fff2ce8e8c147`. G10 must receive independent acceptance and owner release approval before G5 can merge. G5 remains Draft. No historical backfill, revision mutation endpoint, or production replay setting change is included.

Migration `0053_registry_version_snapshot` creates the approved append-only database ledger at first v2 activation, with literal v1/v2 canonical texts and hashes independent of runtime imports. UPDATE, DELETE and TRUNCATE are blocked. API lifespan startup reads both ledger rows, verifies their text digests and exact agreement with code, and refuses missing, unknown or changed contracts without modifying the database. A Celery worker bootstep independently checks the same ledger before its Consumer step starts, using a private nonpooled async engine that is disposed on both success and failure. Bootstep exceptions abort startup; beat scheduling and collector behavior are unchanged. Pure bootstep success/missing/mismatch and cleanup tests do not establish actual Celery/PostgreSQL startup acceptance. Downgrade locks all version-bearing consumers and refuses while a v2 mapping, revision, reading, daily aggregate or rule exists. With only seeded ledger rows and existing legacy/v1 consumers, downgrade/upgrade is permitted. The provisional parent is merged G4-R `0049_telemetry_replay_ledger`; G5/G6/G8 sibling migrations require coordinated reparenting before integration, never an unapproved PR retarget or merge.

Registry 1 remains the default and its conversion/hash golden bytes stay unchanged. Version 2 copies the immutable version-1 physical conversions and adds immutable metric semantics to the complete snapshot digest. Existing mappings, revisions, readings and rules retain their stored version. No automatic upgrade occurs.

New mapping and rule requests may explicitly supply `registry_version: "2"`; omitting it creates version 1. The registry discovery endpoint accepts `?version=2` and exposes kinds, canonical bounds as decimal strings, permitted source units, aggregations and rule types. Internal append_mapping_revision already validates the selected version; public revision mutation remains separately reviewed work. Unknown versions return 422.

| Metrics | Kind | Canonical validity | Aggregation | Rules |
|---|---|---|---|---|
| temperature, supply, return | gauge | at least -273.15 degC | avg/min/max/last | high/low |
| humidity | bounded_ratio | 0–100 percent | avg/min/max/last | high/low |
| power, cooling output | gauge | nonnegative kW | avg/min/max/last | high/low |
| load | gauge | nonnegative percent, no upper cap | avg/min/max/last | high/low |
| availability | state | exactly 0 or 1 | avg/min/max/last | unavailable |
| airflow volume and speed | gauge | nonnegative | avg/min/max/last | high/low |
| differential pressure | gauge | signed, no bound beyond storage | avg/min/max/last | high/low |

Availability's daily average is the fraction of contributing observed states available; it is a summary, not an instantaneous binary sensor state. No counter metric is introduced. The counter kind is reserved; introducing one requires a distinct definition, reset policy and sum/last aggregate implementation. G5 declares the present arithmetic policy; G4's existing daily avg/min/max/count storage remains intact (count is the represented sample count).

Source unit allowlists are explicit per metric and normalized through the selected version's immutable aliases. Source scale applies exactly once before conversion. G3 quantizes source evidence; canonical bounds are evaluated after canonical half-even quantization and numeric storage validation, before persistence. An impossible representable v2 reading raises VALUE_OUT_OF_RANGE, rejected inside its per-record savepoint. Valid peers persist; retries receive the same permanent classification. Nonfinite and numeric-storage overflow retain INVALID_TELEMETRY_VALUE, including values that also violate the v2 physical domain. Collector acknowledgment drops deterministic impossible samples.

V2 threshold rules validate compatible rule type and canonical bounds; unavailable-state rules forbid thresholds. Version-2 rules evaluate only version-2 readings. A version mismatch skips evaluation and never rejects telemetry; it cannot open or clear a v2 alarm. Existing v1 rules keep their authored-unit conversion compatibility. New version-2 mapping hashes use the mapping-conversion.v2 encoding and include the entire registry snapshot hash, so changes to kinds, bounds or policy cause contract drift. Existing v1 and legacy hash encodings remain unchanged.

Owner-recommended Draft choices from issue comment 6108192722 are implemented: state availability; nonnegative power/cooling; uncapped nonnegative load; permanent rejection. These are not production release approval. Physical net/export power needs its own metric. The metric-name database CHECK is unchanged; adding keys remains a separately approved migration.

Validation must include the provided PostgreSQL API tests plus existing G1 pin/replay, G3 exact-number, G4 compaction/concurrency, and G10 snapshot tests. Pure unit/collector passes do not establish PostgreSQL or deployment acceptance. Signed publication, hosted full CI/deployment validation and non-author exact-head review remain required.
