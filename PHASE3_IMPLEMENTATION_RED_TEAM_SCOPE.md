# Phase 3 Red-Team Preparation Scope

This document identifies what an independent, hostile reviewer should target
when validating Phase 3. It is written by the implementer and is **not**
independent validation — self-testing cannot substitute for an adversarial
outside review. Its purpose is to point the red-team at the areas most
likely to hide real defects, and to state plainly what the implementer's own
tests did **not** cover.

## 1. Critical invariants to attack

- **Self-loop prevention**: `ck_power_connection_no_self_loop`. Attempt to
  bypass via a connection where `source_node_id == target_node_id` submitted
  through the API, and separately via a raw SQL insert with a superuser role
  to confirm the constraint (not just the API layer) rejects it.
- **Duplicate active edge prevention**: `uq_power_connection_active_edge` is
  a *partial* unique index (`WHERE effective_to IS NULL`). Attack the boundary:
  does closing a connection (`effective_to` set) and reopening an identical
  one succeed correctly? Does a race between "close A" and "create duplicate
  of A" ever produce two simultaneously-active identical edges?
- **Cycle prevention**: `assert_would_not_create_cycle` locks both nodes in
  canonical order, then walks the *downstream* closure of the proposed
  target to see if it reaches the proposed source. Attack: can two
  *concurrent* connection-creation requests, each individually non-cyclic at
  the moment of its own cycle check, combine to produce a cycle once both
  commit? The canonical lock ordering is intended to serialize any pair
  that could interact, but a red-team should construct a 3-or-more-node
  concurrent scenario (not just 2 nodes) and verify no cycle survives.
- **`asset_reference_exclusive` CHECK**: verify it cannot be bypassed by
  updating a node's `managed_asset_id` and `owning_asset_id` in two separate
  statements within one transaction such that the constraint (deferred? — it
  is NOT deferrable in this migration) is only checked at the end and a
  window exists. Confirm the constraint is genuinely immediate.

## 2. Concurrency surfaces

- `lock_node_pair_in_canonical_order` sorts by `str(uuid)`. Confirm this is
  a stable, total order (it is, since UUIDs are unique), but verify there is
  no code path that acquires locks on the *same pair* in a different order
  elsewhere in the codebase (e.g., disconnect, capacity edit) — a red-team
  should grep for every `SELECT ... FOR UPDATE` on `power_node` and confirm
  all of them go through the canonical-order helper or don't need to.
- `disconnect_power_connection`'s `SELECT ... FOR UPDATE` locks the
  connection row, not the two node rows. Construct a race between
  "disconnect connection X" and "create a new connection reusing X's
  now-freed (source, target, feed_label) slot" — confirm no phantom window
  where both a disconnect and a duplicate-creation succeed based on stale
  reads.
- The implementer's own concurrency tests used exactly 5 concurrent
  attempts for the connection-creation race and did not test races
  involving 3+ distinct node pairs contending for locks in different orders
  simultaneously (a potential deadlock scenario the canonical ordering is
  supposed to prevent, but which was not stress-tested under real
  contention, only reasoned about).

## 3. Graph attacks

- Reduced Graph-letter coverage: the implementer covered A, B, D, E, F, G, J,
  K, L via API-level tests, and C (redundant A/B) and H (deep) via unit
  tests on the application-layer functions rather than through the full API
  stack. **A red-team should re-run C and H through the actual HTTP API**,
  not just the internal service functions, since the API layer (request
  validation, serialization, error mapping) is untested for those two cases.
- `MAX_TRAVERSAL_DEPTH=500` / `MAX_TRAVERSAL_NODES=5000` are hard-coded
  constants, not configurable per environment. A red-team should confirm
  these limits are appropriate for the real expected topology size and
  are not so low that a legitimate large site topology gets falsely
  rejected as "pathological."
- The traversal is breadth-first over *active* connections only
  (`effective_to IS NULL`). Confirm a disconnected-then-reconnected edge
  cannot cause a node to be double-counted or cause a visited-set bug in a
  single traversal call (the visited-set is per-call, not persisted, so this
  should be safe, but was not explicitly tested with a reconnect-during-
  traversal race).
- Fan-out attack: a single node with thousands of direct connections was
  not tested. The bounded-traversal test used a deep chain
  (`MAX_TRAVERSAL_DEPTH` triggered via monkeypatch to 3), not a wide
  fan-out hitting `MAX_TRAVERSAL_NODES`. A red-team should construct a
  single node with >5000 direct children and confirm the node-count bound
  (not just the depth bound) is actually enforced.

## 4. Capacity calculation attacks

- `compute_allocated_kw` stops expanding past any node with its own
  `PowerCapacity` record. This means a node with a capacity record whose
  `rated_capacity_kw` and `configured_capacity_kw` are BOTH null (a
  "recorded but valueless" capacity row) still stops expansion at that
  node, potentially undercounting real downstream load. This was tested at
  the unit level (`test_allocated_kw_stops_at_own_capacity_record`) but the
  edge case of a valueless-but-present capacity record specifically was not
  isolated as its own test.
- Floating-point edge cases: `NaN`, `Infinity`, and extremely large
  `configured_capacity_kw` values were not explicitly tested through the
  API. Pydantic's default float validation may or may not reject `NaN`/`inf`
  depending on version behavior — **a red-team should explicitly POST
  `NaN`/`Infinity`/`-Infinity` as capacity values and confirm the API
  rejects them with a 422, not silently store them** (a stored NaN would
  poison every downstream roll-up and utilization calculation).
- Integer/float overflow on `rated_current_a`, `input_voltage`: DB columns
  are presumably NUMERIC/FLOAT without explicit upper bounds beyond
  positivity. A red-team should attempt absurd values (e.g., 1e308) and
  confirm no silent overflow or DB error leaks internal detail.

## 5. Authorization boundaries

- Tested: Viewer role gets 403 on create, 200 on read; unauthenticated gets
  401; `capacity:read` vs `capacity:manage` distinction for Operator.
  **Not tested**: cross-organization data leakage. Phase 3 does not
  implement organization-scoped filtering on `/dashboard/summary` (disclosed
  explicitly in the endpoint's own docstring and in Known Limitations) — a
  red-team in a genuinely multi-tenant deployment should confirm whether
  this is an actual data-leakage risk in the target deployment model or
  merely a reporting-scope limitation, since the master prompt's global
  authorization model may or may not be the correct model for a specific
  deployment.
- IDOR: object-level access to a specific `power_node_id`/`power_connection_id`
  by a user with `power:read` was tested for the happy path but not with a
  crafted, syntactically-valid-but-nonexistent, or malformed (non-UUID)
  ID — confirm the 404 vs 422 distinction is handled without an
  unhandled exception leaking a stack trace.

## 6. Audit boundaries

- Not independently re-verified in this phase beyond confirming audit rows
  are created in the same transaction as domain writes (by code inspection
  and reuse of the exact Phase 1 helper). **A red-team should query the
  audit_log table directly after each Phase 3 mutation type
  (PowerNode create/retire, PowerConnection create/patch/disconnect,
  PowerCapacity put) and confirm every field expected by the append-only
  audit contract is populated correctly** — this was not done with a
  dedicated audit-content assertion test in this phase (only audit
  *presence*, not full field-content correctness, was verified).

## 7. API abuse cases

- Oversized pagination: `/power/nodes?limit=200` was used in testing;
  the endpoint's actual max-limit enforcement (if any) was not
  specifically attacked with `limit=999999` or negative limits.
- Mass assignment: Pydantic input models were hand-written per endpoint
  (not derived from the ORM model directly), which should prevent mass
  assignment of server-controlled fields like `version` or `id` — a
  red-team should attempt to POST a `version` or `id` field on create
  endpoints and confirm it is ignored, not honored.

## 8. Dashboard correctness risks

- `total_configured_kw` at the dashboard-wide level is a simple sum across
  ALL nodes with a configured capacity, without regard to topology — this
  means a UPS's configured capacity and its downstream PDU's configured
  capacity are BOTH summed into the same dashboard total, which double-counts
  capacity that exists at multiple tiers of the same physical chain. This is
  disclosed in the code as a deliberate simplification (the alternative,
  `total_allocated_kw`, is intentionally left `None` at the dashboard-wide
  level specifically because of this double-counting risk) but the
  `total_configured_kw` figure itself still carries the same risk and a
  red-team should confirm this is clearly enough labeled to avoid an
  operator misreading it as a true independent-capacity total.
- The N+1 query pattern in `GET /dashboard/summary` (disclosed in
  PHASE3_IMPLEMENTATION_REPORT.md §14) is a performance risk, not a
  correctness risk, but under real production load it could cause request
  timeouts that surface as a dashboard *availability* problem — a red-team
  should confirm what the actual timeout/error behavior is when this
  endpoint is slow (does it degrade gracefully or hang the connection pool).

## 9. Data-quality risks

- `data_quality` tagging was tested at the unit level for the capacity
  functions but not end-to-end through the dashboard UI for every
  combination (e.g., a node with `data_quality="not_applicable"` rendering
  correctly distinct from `"unknown"` in the actual browser, not just in
  the API response) — the browser validation script checked for the
  presence of "Available" text but did not assert on the specific
  data-quality label text rendered for each state.

## 10. Performance risks

- **Not tested at the master prompt's stated target scale**
  (1,000 racks / 10,000 equipment / 5,000 power nodes / 10,000+ power
  connections). The implementer's own performance test used 1,000 power
  nodes only (a 300-deep chain + 700 leaves), roughly 20% of the target
  power-node count and with no racks/equipment at scale simultaneously in
  the same test. **This is the single largest unverified area of this
  phase** and should be the red-team's highest scale-related priority.
- The N+1 dashboard query (1.9s at 700 capacity-bearing nodes) will get
  substantially worse at 5,000 nodes — a red-team should extrapolate or
  directly measure this at full scale before accepting the endpoint as
  production-ready.
- Index usage was not verified via `EXPLAIN ANALYZE` on any of the new
  queries — the implementer relied on standard FK/pagination index
  conventions from Phase 1/2 without profiling actual query plans under
  Phase 3's specific access patterns (e.g., the batched per-level
  traversal query, the allocated-capacity roll-up's two batched queries).

## 11. Migration risks

- Fresh install, upgrade, downgrade, re-upgrade were all validated on an
  empty scratch database. **Not tested**: upgrading a database that
  already contains substantial Phase 1/2 production-shaped data (thousands
  of existing racks/equipment) to confirm migration 0006 applies cleanly
  and quickly at that scale — only structural correctness (constraints,
  indexes, seeded permissions) was validated, not migration performance
  under load.
- The `_seed_new_permissions()` idempotent-seed function was reused
  unchanged from migration 0004's exact pattern and not independently
  re-audited for correctness beyond confirming it runs without error.

## 12. What the implementer's own tests did NOT test (explicit list)

- Full master-prompt-scale performance (1,000 racks / 10,000 equipment /
  5,000 power nodes / 10,000+ connections) — only a reduced 1,000-power-node
  scenario was run.
- Graph C (redundant A/B) and Graph H (very deep) through the actual HTTP
  API rather than only the internal service-layer unit tests.
  A wide fan-out attack against `MAX_TRAVERSAL_NODES` (only the depth bound
  was exercised via monkeypatching; the node-count bound was not
  independently triggered).
- `NaN`/`Infinity`/extremely large numeric values submitted through the API
  for any capacity or electrical field.
- Cross-organization data leakage on the dashboard endpoints in a genuinely
  multi-tenant dataset.
- Full audit-log field-content correctness (only audit-row presence was
  confirmed, not that every expected field is populated correctly for each
  Phase 3 mutation type).
- Oversized/negative pagination limits on the new endpoints.
- Mass-assignment attempts against `version`/`id` fields on create
  endpoints.
- `EXPLAIN ANALYZE` query-plan verification for any new query.
- Migration performance against a database already containing
  production-scale Phase 1/2 data.
- Concurrent contention across 3+ distinct node pairs simultaneously (only
  a single-pair, 5-way race was tested).

A red-team that limits itself to re-running the existing test suite will
not discover anything new. The areas above — full-scale performance, the
untested Graph-letter/API combinations, numeric edge cases, and audit
field-content correctness — are where genuine, still-undiscovered defects
are most likely to exist.
