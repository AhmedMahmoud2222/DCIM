# G10 immutable registry snapshots — draft implementation

Authority: Issue #128 comment 6108192722 and design comment 6100092674.
Source baseline: cloud main b573aaf3b037b4076db200838cf9c6d657513853.

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
`735fc99734938085cb6ffcadaac2f296e171aa40a4bcf643c8287a9ec6057e27`.

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

Local verification: 56 focused snapshot, G1 hash, physical-unit, cross-version
power and cooling-conversion tests passed; changed-file Ruff and Mypy passed.
Alembic reports single head `0048_daily_aggregate_exact_sum`.
The normal full suite stops at test_login_succeeds_with_correct_credentials:
repository-default dcim_test authentication fails for dcim_app. Full-app Mypy
reports 19 Windows-platform attribute errors in the unchanged Linux-only
catalog extraction sandbox and PDF validation modules; scoped Mypy is clean.
Required PostgreSQL compatibility, full suite, deployment validation, exact-head
CI and independent acceptance are not claimed by these focused results.

Publication is blocked in this session: Git cannot connect to github.com and the
configured SSH signing private key is unavailable. No commit, branch or PR was
published. G4-R PR #137 is open; this deliverable must remain DRAFT even after
publication, until G4 acceptance and separate owner release approval.

Independent review identified source-only v2 alarm unit rejection and dropped
versions in power consumers. The alarm regression failed before correction, then
passed. Three consumer regressions failed before implementing version-aware
canonical projection and hourly normalization; all are green in the focused run.
Ruling: fixed-unit power/thermal/spatial outputs stay in frozen v1 while source
interpretation uses each reading's own snapshot. If wrong, a later caller could
expect a v2 display unit; output contracts would need a separate versioned API.
The review is not acceptance; required integrated checks remain blocked.
