# Telemetry mapping revisions and event-time contract pinning (Issue #128 / G1)

Scope: G1 only. Migration `0046_mapping_revisions`. Retention/history consistency (G2, PR #129), numeric
fidelity (G3/G4) and registry-version isolation (G10) are separate changes.

No representative database has been profiled. Nothing here claims measured production impact; the exposure
described is a contract gap established by code inspection.

## The gap
A telemetry record carried no source unit, scale or revision. Central converted it with whichever mapping was
current at receipt, so a sample acquired under one source contract and delivered after the contract changed
would have been reinterpreted. No shipped code path edits a mapping in place today (the API only creates and
lists mappings), so the exposure is conditional: a mapping-edit feature, a direct database change, or a
collector that queues telemetry.

## What is implemented
| Piece | State |
|---|---|
| `integration_metric_mapping_revision`, append-only, DB trigger rejects UPDATE and DELETE (except the cascade of deleting the mapping) | Implemented, tested on PostgreSQL |
| `integration_metric_mapping.current_revision_id`; nullable `telemetry_reading.mapping_revision_id` and `contract_evidence` with FK and CHECKs | Implemented, tested |
| `conversion_hash` (below) and drift refusal at ingest | Implemented, tested |
| Ingest resolves the revision a record is pinned to; unpinned records follow the rule below | Implemented, tested through the HTTP API |
| `telemetry_contract_hold`, bounded retry, expiry, operator list/resolve | Implemented, tested |
| `GET /collectors/{id}/telemetry-contracts` (HMAC-authenticated plan) | Implemented, tested |
| `append_mapping_revision` service | Implemented as an internal service; **no HTTP mutation surface** (deferred to a separate reviewed change) |
| Collector helpers: `ContractBook`, `telemetry_record_payload`, `classify_telemetry_ack`, `CentralClient.get_telemetry_contracts` | Implemented and **contract-tested only**: the packaged collector has no telemetry acquisition or delivery loop and none was added. The backend API test builds a record with these helpers, queues nothing, and posts it to Central. |

## Revision rules
* A revision is never edited. `effective_from` is Central's clock at creation and never retroactive.
* Mappings created through the ORM or API get an authored revision 1 in the same transaction.
* Migration seed: one revision per existing mapping, `provenance = 'backfilled_from_current'`,
  `effective_from` = migration time. It is the mapping row as it stood at upgrade, **not** evidence of the
  contract in force at any earlier event time. No existing `telemetry_reading` is touched: NULL revision and
  NULL evidence mean "stored before pinning, unverified".

## Resolution of a record
1. **Pinned** (`mapping_revision_id` present). The revision is authoritative however much has changed since,
   and stays resolvable forever. It must belong to the record's integration, source identifier and mapping
   (`MAPPING_REVISION_MISMATCH`, permanent), exist (`UNKNOWN_MAPPING_REVISION`, permanent), and agree with the
   optional `source_unit` / `source_scale` echoes. Evidence `pinned`.
2. **Unpinned.** Accepted, with evidence `inferred_single_revision`, only when exactly one revision has ever
   existed **and** `occurred_at >= effective_from`. A record that occurred before the only revision's
   activation, a mapping with several revisions, or a mapping with no revision is held
   (`AMBIGUOUS_MAPPING_CONTRACT`). Held records are never converted with the latest mapping.
   Historical consequence: for mappings that existed at upgrade, unpinned records that occurred before the
   upgrade are held, because no ledger recorded earlier edits.
3. A record whose `(collector, dedup_key)` is already stored answers `duplicate` before any hold check.
4. Before converting, the revision's `conversion_hash` is recomputed from the current registry; a mismatch is
   `CONVERSION_CONTRACT_DRIFT` (retryable; needs a Central fix).

## `conversion_hash`
SHA-256 of the UTF-8 text built by `app/domain/telemetry/contract.py`: a fixed `key=value` line sequence
(format tag `dcim.mapping-conversion.v1`, metric, registry version or `legacy`, source unit text, source scale at
8 decimal places and, for registry-versioned mappings, the resolved source and canonical unit symbol, dimension,
scale and offset, the 8-decimal precision and `ROUND_HALF_EVEN`). Numbers render as plain normalised Decimals.
A changed unit factor, offset, canonical unit, precision or rounding mode changes the hash. The hash makes drift
observable; it does not replace immutable conversion definitions, which remain G10.

## Holds
A held record is stored in `telemetry_contract_hold` (value kept as text), so no data is lost. Bounds:
`MAX_HOLD_ATTEMPTS = 12` deliveries or `MAX_HOLD_AGE = 72 h` since the first hold. After either, Central answers
`CONTRACT_HOLD_EXPIRED`, which the collector treats as final, and the stored record stays `expired`.
Collector-side backoff is the collector's existing retry policy.

Observability: structured log events `telemetry_contract_hold` and `telemetry_contract_hold_expired`;
`GET /telemetry/contract-holds` (`telemetry:read`).
Operator resolution: `POST /telemetry/contract-holds/{id}/resolve` (`telemetry:manage`, audited) ingests the held or
expired record under one explicitly chosen revision of the same integration source, with evidence
`operator_resolved`. It never edits a mapping or revision.

## Compatibility
* #99 canonical conversion, legacy alarm compatibility, savepoints, dedup key, `series_key` and the disjoint
  `registry:` namespace are unchanged. Stored values are canonical at write, so alarms, compaction/late merge and
  history need no change; no G2 code is edited.
* Existing API fields are unchanged. Added: optional record fields (`mapping_revision_id`, `source_unit`,
  `source_scale`), `current_revision_id` on mapping responses, the ack codes above.
* Older collectors keep working while mappings have a single revision.
* Daily aggregates still carry no revision lineage (G6/G7).

## Migration and rollback
Additive. `telemetry_reading` is altered last: two nullable columns plus a foreign key and two CHECK constraints added
`NOT VALID` (no scan, no rewrite). Downgrade takes `ACCESS EXCLUSIVE` locks and refuses if any authored or second revision, any reading
carrying a revision or evidence, or any hold exists. A seed-only schema downgrades losslessly.

Lock measurements: see the PR description (populated rehearsal). Constraints on `telemetry_reading` are left `NOT VALID` (enforced for new rows; existing rows are all NULL).

## Not done here
Mapping-update endpoint; a telemetry acquisition loop in the collector; strict per-integration mode;
exact-numeric (G3/G4) and registry-version immutability (G10).
