# Issue 128: G7 readings provenance (draft)

This phase derives reading provenance from existing stored columns. No migration,
backfill, conversion, rescaling, source precision rewrite or permission change.
G1/G2/G3 fields and the single bounded history SQL statement remain in place.

History adds series_key, mapping_revision_id, contract_evidence and provenance_status.
registry_version and exact decimal/source fields remain unchanged. All five reading
statuses prioritize recorded contract evidence. Pinned, inferred and operator_resolved
require a stored revision plus matching evidence, including legal checked legacy
revisions whose registry_version is null. Registry version describes conversion
semantics; it does not invalidate recorded contract provenance. Without recognized
revision evidence, absent registry is unverified_legacy (historical pre-registry
rows); a present registry is unverified_registry. Evidence with no recorded revision
never fabricates a contract-backed status.
Pinned means Central checked the collector's contract claim; it does not certify a
physical sensor or guarantee source precision.

Optional series_key and registry_version filters affect raw and daily series before
the shared response limit. Repeatable provenance_status filters accept the five
reading status values and select readings only; daily rows have no derived reading
status. Unknown status strings return 422. Without filters, the same raw/daily result
set and order is returned with additive fields. No aggregate lineage is fabricated.

Client groups by series identity, status, canonical unit, registry version,
presentation unit and resolution. Every group has a separate chart and min/max;
this conservative separation satisfies the cross-era comparison rule. No chart spans
an unverified group and a contract-backed group. The existing whole-response
mixed-presentation-unit chart guard remains. Single-point and unverified groups are
visible with labels. Missing series identity cannot justify joining independent rows.
Daily groups display 'Aggregate provenance pending', never a reading contract label.
The history list shows the provenance label beside every point.

No telemetry-reading CSV exporter exists in the current client; the Events CSV
exports alarms, so its unrelated contract is unchanged. Aggregate provenance and
aggregate filter vocabulary wait for G6 after G4-R merges. Operational acceptance,
PostgreSQL integration tests and independent review are still required before release.
All PRs remain draft until G4 completion and owner release authorization.

Raw history grouping also separates recorded mapping revision IDs, conservatively
finer than the minimum series/status grouping contract. Same-status readings from
different revisions cannot share a trend or min/max range. Missing revision IDs
remain unknown; no revision is inferred or conversion changed. Daily rows retain
their separate aggregate summary contract.
