# G10 immutable registry snapshots — draft implementation

Authority: Issue #128 comment 6108192722 and design comment 6100092674.
Integration baseline: cloud main bd12390544164132df90002c3ea40119198db2cb.

Version 1 is frozen, including the eleven existing metric definitions, all unit
definitions and aliases. Every subsequent definition change requires another
version. The public default remains version 1 for compatibility. `UNITS`,
`UNIT_ALIASES` and `METRIC_REGISTRY` are immutable aliases to that snapshot.
Snapshots copy their input dictionaries, and their definitions are frozen dataclasses.

The canonical snapshot encoding is documented by `snapshot_text`; it includes
the unit factors, offsets, aliases, metric dimension, canonical and presentation
units, precision, rounding and Decimal context. The committed UTF-8 golden file
and SHA-256 constant pin that encoding. Import refuses changed definitions.
The v1 content hash is
`d29e5d41a996e5b081583e87f8e6a270b4040b682432c16aaad7b0f22f9ac6d4`.

Conversion and mapping-contract hashing use an explicit 28-digit, half-even
Decimal context. Scaling before canonical conversion occurs inside that context.
The G1 mapping hash format is unchanged; its Fahrenheit golden vector remains
`86044c6d3f4ad702dfd5f5166342b1f57aa668050a9a1aaf547bfcf802ba320e`.
The G1 drift tests now replace an entire test-only snapshot instead of mutating
an immutable map. Drift refusal remains covered rather than removed.

Alarm comparison uses the reading snapshot for its source and the rule snapshot
for its threshold unit. Unversioned explicit legacy rules use frozen v1.
History already passes stored versions. Environmental presentation and catalog
extraction's final projection now pass their stored conversion version explicitly.
Power rollups select each sample's unit and stored version, normalize into the
consumer's fixed v1 contract, and choose timestamp ties after normalization.
Hourly history groups source sums/counts by version and unit before normalization
and averaging; its v1 output stamp now describes the output meaning. Thermal and
spatial consumers validate canonical admission against the source snapshot before
normalizing into their fixed v1 display/analysis contract. No consumer rescales
the original reading or mutates its provenance. These cross-version consumer
paths still require PostgreSQL integration validation before G5 enables v2.

No migration, data backfill, production profiling or registry database ledger
is included. The database snapshot ledger remains part of the first v2 deliverable.
Revert code to roll back; historical data is untouched.

## Current remediation and gates

The published Draft captured mojibake degree aliases. Live main preserves Unicode °c and °f. The corrected golden snapshot and digest restore those shipped definitions; they do not redefine production v1. Regressions preserve Celsius aliases and 68°F → 20°C. The Python 3.14 CI failure came from an unrelated randomized spatial test fixture: a generated RACK-03... tag accidentally matched the imported RACK-03 label. Only the failing test uses deterministic unrelated tags; its assertions and production reconciliation remain unchanged.

G4-R is merged. This worktree cleanly integrated main with an uncommitted merge. Alembic has one head, 0049_telemetry_replay_ledger. Current focused snapshot, conversion, consumer and alarm tests: 62 passed (database-independent run), with Ruff and diff checks passing. Cross-version consumers and the repaired spatial API fixture still require actual PostgreSQL execution. Published CI run38155961069 fails on the old fixture; deployment run38155961068 succeeds. Those runs do not validate unpublished fixes.

Git HTTPS push authentication and authorized signing remain unavailable. No new commit or PR update is published. All work must remain Draft; signed exact-head review, full CI/deployment validation and owner release authorization remain gates. No merge or deployment is authorized.
