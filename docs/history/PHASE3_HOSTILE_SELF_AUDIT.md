# Phase 3 Hostile Self-Audit

**This is a self-audit performed by the implementation agent. It is NOT an
independent red-team validation and must not be represented as one.** Its
purpose is to attack the Phase 3 implementation as critically as possible
before an actual independent reviewer does, so that as many real defects as
possible are found and disclosed now rather than discovered later. No
application code, tests, migrations, or configuration were modified during
this audit. No fixes were applied.

---

## 1. Executive Summary

This audit found **one CRITICAL, confirmed-by-execution defect** in the
cycle-prevention mechanism that the implementation report and traceability
matrix both described as closing the H4 gap: canonical two-endpoint node
locking does **not**, in fact, prevent a real cycle from being created when
two concurrent transactions each add a different edge that is individually
cycle-free at the moment of its own check but jointly closes a cycle in
combination with each other and pre-existing edges. This was reproduced
experimentally (Section 9) and is not a theoretical concern.

Two further **HIGH-severity confirmed defects** were found:
a capacity/redundancy exception can silently disappear (return an *empty*
exception list, not even `CAPACITY_UNKNOWN`) for a node whose own capacity
is fully known but whose allocation roll-up hit the bounded-traversal limit
— directly contradicting the master prompt's "data-quality problems must be
visible" requirement — and retiring a `PowerNode` does not remove it from
graph traversal or capacity roll-up, only from new-connection eligibility,
so a "decommissioned" PDU continues to silently count toward its ancestors'
allocated capacity indefinitely.

One MEDIUM-severity confirmed defect (malformed `If-Match` on the capacity
PUT endpoint triggers an unhandled `ValueError`, mapped only by the generic
catch-all handler, not the endpoint's own clean 4xx path) and several
additional MEDIUM/LOW static-analysis findings (dead `redundancy_factor`
field, unreachable `POWER_TOPOLOGY_INVALID` condition code, no DB-level
subtype check on `PDUOutlet.pdu_asset_id`, no site-scoping anywhere on
`/power/*`, alarm-fatigue risk in the redundancy classifier's SPOF
detection for the very common single-utility/single-generator topology)
round out the findings list.

269/269 backend tests pass in a clean, isolated re-run performed during
this audit (Section 3), confirming no gross regression — but test count and
pass rate do not, on their own, mean the implementation is correct; several
of the findings above pass every existing test while still being real
defects, because no existing test exercises the specific interleaving or
data-quality-masking condition involved.

**Do not treat this audit as approval.** Phase 3 requires further work
before it should be considered ready for a genuinely independent gate,
starting with the cycle-prevention defect in Section 9.

---

## 2. Exact Commit Audited

```
commit 4606810586084511a6b3992050baca63c5aedbf6
Author: Claude <noreply@anthropic.com>
Date:   Thu Sep 17 13:40:04 2026 +0000
    feat: Phase 3 power infrastructure, capacity, and dashboard foundation
Parent: 066860a4effb1abaf36293fb140da9572f5ad6fa
Branch: claude/new-session-1vutvy
```

`HEAD` was confirmed equal to the target commit at the start of this audit
(EXECUTED: `git rev-parse HEAD` == `4606810586084511a6b3992050baca63c5aedbf6`).
No divergence to reconcile.

## 3. Repository State

EXECUTED at audit start and again at audit end:

```
git status            -> nothing to commit, working tree clean (both times)
git log --oneline -20  -> confirms the commit sequence through 4606810 exactly
git show --stat 4606810 -> 28 files changed, 4834 insertions(+), 8 deletions(-)
```

28 files changed by the target commit (exact list already recorded in
`PHASE3_TRACEABILITY_MATRIX.md`; not repeated here). No file outside that
list was touched by this commit.

A clean, isolated re-run of the full backend suite was EXECUTED during this
audit:

```
269 passed, 6 warnings in 62.70s
```

This matches the count claimed in `PHASE3_IMPLEMENTATION_REPORT.md`. Note:
an earlier, non-isolated attempt to run the suite concurrently with a
diagnostic script against the same test database produced 19 spurious
failures (floor-plan/outbox/health tests) caused by Redis not running in
this audit environment plus concurrent-process contention on the shared
test database — **not** a Phase 3 regression. This is recorded here for
transparency, not suppressed; the clean re-run above is the number that
should be trusted.

## 4. Environment

- PostgreSQL 16, started manually at the beginning of this audit (`sudo
  service postgresql start`) — it was not running when the audit began.
- Redis was **not** running at the start of this audit and had to be
  started manually before a clean full-suite run was possible. This is
  itself a minor observability/operational note: nothing in the repository
  documents or checks for this at audit/session start (see Section 25).
- All temporary diagnostic scripts used in this audit were created under
  `/tmp/claude-.../scratchpad/` (outside the repository) or briefly inside
  `backend/tests/api/_hostile_audit_tmp.py` (deleted before this audit
  concluded — confirmed absent by the final `git status` in Section 3).

## 5. Phase 3 Contract (Reconstructed)

Reconstructed from the master prompt and `ARCHITECTURE_REVIEW.md` §4b/§13/
§13a/§14, as already summarized in `PHASE3_GAP_ANALYSIS.md` and
`PHASE3_TRACEABILITY_MATRIX.md`. Not repeated in full here; this audit
treats those two documents' contract restatement as accurate and instead
focuses on whether the *implementation* actually satisfies it — the
findings below show it does not, in at least the cycle-prevention and
capacity-exception-visibility cases, despite both documents asserting that
it does.

## 6. Architecture Review

The implementation follows the modular-monolith, ManagedAsset-anchored,
API-first shape correctly at a structural level: `PDU`/`UPS`/`Generator`/
`PowerPanel` are genuine shared-PK `ManagedAsset` subtypes; `PowerNode` is
a single real join target; no second audit/outbox/idempotency system was
introduced; no telemetry, alarm engine, AI, or microservice dependency was
found anywhere in the diff (confirmed by inspection of all 28 changed
files — none reference SNMP, Modbus, Kafka, an LLM API, or a vector store).
This part of the self-report is accurate.

Where the architecture review's *intent* is not fully realized is in the
concurrency guarantee it explicitly claims for cycle prevention (Section 9)
and in the capacity-visibility guarantee the master prompt explicitly
demands (Section 10) — both are structurally present as code but do not
hold under the conditions this audit constructed.

## 7. Database Review

Constraints present and confirmed by direct model inspection
(`backend/app/domain/power/models.py`):

- `power_node.node_type_allowed`, `power_node.asset_reference_exclusive` —
  present, correctly written, previously tested by
  `test_phase3_power_constraints.py` (not re-litigated here).
- `power_connection.no_self_loop`, `uq_power_connection_active_edge`
  (partial unique index), `connection_type_allowed`, `feed_label_allowed`,
  `phase_allowed`, `status_allowed`, `voltage_positive`,
  `rated_current_a_positive` — all present.
- `power_capacity` non-negative/range/`warning_le_critical`/
  `redundancy_factor_allowed` CHECKs and `uq_power_capacity_current_per_node`
  partial unique index — all present.

**New finding not previously disclosed** (STATIC ANALYSIS): `pdu_outlet`'s
`pdu_asset_id` is a plain FK to `managed_asset.id` with **no DB-level
constraint that the referenced row is actually a `PDU`** (as opposed to a
`Rack`, `Equipment`, or any other `ManagedAsset` subtype). The API layer
(`create_pdu_outlet` in `power.py`) does correctly check this by loading
`db.get(PDU, body.pdu_asset_id)` before creating the outlet, so the API
path is safe — but a direct SQL insert (or a future code path that
forgets this check) could attach a `PDUOutlet` to any `ManagedAsset`,
including a Rack or a piece of Equipment, and nothing in the schema would
reject it. The same is true of `PowerNode.owning_asset_id` for
`equipment_power_input` nodes: it is FK'd to `managed_asset.id` generically,
not specifically constrained to rows that are also `Equipment` rows.

**Also newly noted** (STATIC ANALYSIS): `managed_asset_id`/`owning_asset_id`
on `power_node` are `ondelete="CASCADE"` from `managed_asset`, while
`power_connection.source_node_id`/`target_node_id` are `ondelete="RESTRICT"`
from `power_node`. No hard-delete endpoint for `ManagedAsset` exists
anywhere in Phase 1/2/3 today, so this is currently unreachable — but if one
is ever added, deleting a `ManagedAsset` backing a connected `PowerNode`
would attempt to cascade into a `power_node` row that `power_connection`'s
`RESTRICT` FK refuses to let disappear, producing a raw `IntegrityError`
mid-cascade rather than a clean, anticipated error. This is a latent
landmine for a future phase, not a Phase 3 defect today, but worth carrying
forward as a documented risk.

## 8. Power Graph Review

`app/application/power_graph.py`'s `_traverse` is genuinely iterative
(explicit `frontier`/`visited` sets, one batched query per level, no Python
recursion) — confirmed by direct code reading, matching its own docstring's
claim. `MAX_TRAVERSAL_DEPTH=500`/`MAX_TRAVERSAL_NODES=5000` are simple,
hard-coded module constants (not environment-configurable) — appropriate
for a first implementation but worth flagging as an operational
inflexibility, not a defect.

`_traverse` unconditionally strips the traversal root from its own result
set (`visited.discard(root_id)`) regardless of whether the root re-appears
via a genuine cycle. This is benign for the traversal's own stated purpose
("everything downstream/upstream of X, not including X"), but it means a
cycle check that only asks "does root appear in its own downstream set"
would always answer "no" even when a real cycle exists — this exact
property is what made a naive verification of Section 9's race attempt
initially (and incorrectly) report "no cycle found" until the audit's own
verification method was corrected to check reachability from a *different*
node in the cycle instead. This does not affect `assert_would_not_create_
cycle` itself (which checks `source in downstream(target)`, never
`source in downstream(source)`), but it is a sharp edge worth documenting
for anyone else writing verification code against this module.

## 9. Cycle Prevention / Concurrency Review — CRITICAL FINDING (EXECUTED)

This is the most important finding of this audit.

**Claim under test** (from `power_graph.py`'s own docstring and
`PHASE3_TRACEABILITY_MATRIX.md` row 8): canonical two-endpoint node locking
plus a transactional downstream-traversal cycle check together prevent any
concurrent pair of connection-creation requests from producing a cycle.

**Attack constructed:** two pre-existing (already-committed) edges N2→N3
and N4→N1. Two concurrent transactions: Tx1 adds N1→N2 (canonical lock set
`{N1,N2}`), Tx2 adds N3→N4 (canonical lock set `{N3,N4}`). These two lock
sets are disjoint — nothing serializes Tx1 against Tx2, because canonical
ordering only ever locks the two *endpoints of the edge being created*, not
any node the traversal walks through to check for a cycle. If both
transactions run their own cycle check *before* either commits, each sees
a cycle-free graph (the other's edge isn't there yet), both pass, and both
commit — leaving the finished graph as N1→N2→N3→N4→N1, a real 4-cycle.

**EXECUTED** (script: `hostile_audit_cycle_race.py`, run directly against
`dcim_test`, calling the actual `lock_node_pair_in_canonical_order` and
`assert_would_not_create_cycle` functions from `app.application.power_graph`
with two independent `AsyncSession`s synchronized via `asyncio.Event` to
force exactly this interleaving):

```
Canonical lock set for Tx1 (N1->N2): [8e64b226-..., 93397d88-...]
Canonical lock set for Tx2 (N3->N4): [c82f8d05-..., f72b1ceb-...]
Disjoint lock sets: True
Results: {'tx1_cycle_check': 'PASSED (no cycle detected at this point)',
          'tx2_cycle_check': 'PASSED (no cycle detected at this point)',
          'tx2_committed': True, 'tx1_committed': True}
Downstream of N2 after both commits: ['N1', 'N4', 'N3']
CYCLE ACTUALLY PRESENT IN COMMITTED GRAPH: True
```

Both individual cycle checks passed. Both transactions committed. The
resulting graph is a confirmed, real cycle (N1 is reachable downstream of
N2, which N1 itself feeds). This is not a false negative in the check
called by each transaction individually — each one is, in isolation,
correct given what it could see at the time. It is a genuine gap in the
concurrency *protocol*: true global acyclicity is a property of the whole
graph, and this implementation only ever serializes on the two endpoints
of the edge currently being inserted, never on the transitive set of nodes
the cycle check itself reads.

**Severity: CRITICAL.** This directly contradicts the implementation
report's claim of closing architecture review's H4 gap, and contradicts
the master prompt's explicit requirement that the distribution graph
remain acyclic under concurrent mutation. It requires two genuinely
concurrent, precisely-timed requests to trigger — not a trivial attack for
a casual user to pull off by accident, but entirely realistic for (a) two
automated provisioning scripts wiring up different parts of a large
topology concurrently, or (b) a deliberate adversary who understands the
locking scheme (which is now, by this document's own existence, public
knowledge within the project).

**What would actually fix this** (documented here for the record, per the
no-fix rule — **not implemented**): the correct fix requires either (a)
`SERIALIZABLE` transaction isolation for connection-creation transactions
(Postgres's SSI would detect this exact write-skew pattern and abort one of
the two transactions), or (b) locking a broader, deterministically-ordered
set of nodes than just the two edge endpoints — e.g., every node in the
downstream closure of the target plus every node in the upstream closure of
the source, still sorted canonically to avoid deadlock, which is
significantly more expensive but actually correct, or (c) a single global
advisory lock serializing all connection-creation transactions against each
other (simple, correct, but eliminates concurrency in this specific
mutation entirely — likely acceptable given connection-creation is not a
particularly high-frequency operation in a DCIM system). None of these was
implemented in this audit; this is a recommendation for the independent
red-team and future correction work, not a fix applied here.

**Untested combinations the same defect likely also affects**, not
independently executed in this audit due to time constraints: 3+
concurrent transactions each adding one edge of a longer cycle (this audit
only tested the minimal 2-transaction/4-node case); a transaction disconnecting
an existing edge concurrently with two others racing to close a cycle
through the disconnected edge's former neighbors.

## 10. Capacity Review — HIGH FINDING (EXECUTED)

**Claim under test:** "data-quality problems must be visible" (master
prompt) and "never silently treat missing power relationships as healthy"
— i.e., a node whose capacity cannot be fully computed should never look
identical to a healthy node with no issues.

**Attack constructed:** `compute_allocated_kw`'s downstream-subtree
discovery pass is bounded by `MAX_TRAVERSAL_NODES`; when exceeded, it
returns `(None, "unknown")` for the *entire* node's allocation, regardless
of how much of the subtree was actually resolved. `get_capacity_figures`
correctly threads this into `data_quality="unknown"`,
`utilization_pct=None`. But `derive_node_capacity_exceptions` only emits
`CAPACITY_UNKNOWN` when `figures.effective_capacity_kw is None` — **it does
not check `figures.data_quality` at all**. A node whose own
rated/configured capacity IS known (so `effective_capacity_kw` is not
`None`) but whose *allocation* is unknown (because the downstream subtree
is large) falls through to `return exceptions` with an **empty list** —
neither `CAPACITY_UNKNOWN`, nor `CAPACITY_OVERLOAD`, nor
`CAPACITY_NEAR_LIMIT`, nor anything else.

**EXECUTED** (via `backend/tests/api/_hostile_audit_tmp.py`, monkeypatching
`MAX_TRAVERSAL_NODES` down to 2 and building a 5-node downstream chain
under a utility-intake node with a known `rated_capacity_kw=100`):

```
AUDIT: exceptions list for the bounded-traversal utility node: []
AUDIT utility capacity figures under bounded traversal:
  {'rated_capacity_kw': 100.0, 'effective_capacity_kw': 100.0,
   'allocated_kw': None, 'available_kw': None, 'utilization_pct': None,
   'data_quality': 'unknown', ...}
```

The node's own capacity API response correctly shows `data_quality:
"unknown"` — so a human looking directly at that one node's capacity panel
in the frontend would see the ambiguity. But `/power/capacity-exceptions`
and `/dashboard/exceptions` — the surfaces the master prompt explicitly
requires to make data-quality and capacity problems *visible without
having to inspect every node individually* — report **nothing** for this
node. At the master prompt's own target production scale (5,000+ power
nodes, 10,000+ connections), any node whose downstream subtree is large
enough to hit the 5,000-node traversal bound (plausible for a utility
intake or a generator feeding a large fraction of a site) would silently
disappear from the dashboard's exception list even if it were, in reality,
severely overloaded. This is precisely the "silently treat a real problem
as healthy" failure mode the master prompt explicitly forbids.

**Severity: HIGH.** Root cause: `derive_node_capacity_exceptions` branches
on `figures.utilization_pct is None` and then, within that branch, only on
`figures.effective_capacity_kw is None` — it never considers the
`allocated`-side data-quality independently. The fix (not implemented, per
the no-fix rule) would add a `CAPACITY_UNKNOWN`-class exception (or a new,
distinct code — perhaps reusing `POWER_TOPOLOGY_INVALID`, which currently
sits unused, see Section 30 finding F-7) whenever `figures.data_quality in
("unknown",)` regardless of which side (capacity or allocation) caused it.

## 11. A/B Redundancy Review

Constructed and traced through the actual code (not independently
re-executed against a live database in this audit pass, since the unit
tests already exercise the core `equipment_power_summary` logic
extensively per `PHASE3_TRACEABILITY_MATRIX.md` row 16 — this section is
STATIC ANALYSIS building on that existing coverage):

- Healthy A/B (two genuinely separate upstream chains, no shared ancestor):
  correctly classified `dual_feed_healthy`. Confirmed by reading the
  existing unit test `test_effective_demand_kw_uses_max_for_ab_pair` and
  the classifier logic itself.
- A/B sharing a common upstream node (e.g., both ultimately trace back to
  the site's single generator or single utility intake — the overwhelmingly
  common real-world topology for anything short of a full 2N site):
  correctly classified `degraded` via the ancestor-set-intersection check.
  This is technically **correct** SPOF detection, not a bug — but see
  Section 30 finding F-1 for the operational-usefulness concern it raises
  (near-universal "degraded" status in ordinary single-utility-feed sites).
- One feed present, one missing: correctly falls into `missing_upstream`
  and classified `degraded` (not silently treated as healthy). Confirmed by
  code reading.
- A node with duplicate feed labels (e.g., two feed nodes both connected
  with `feed_label="A"`, none with `"B"`): `labels = {info["feed_label"]
  for ...}` collapses to `{"A"}` (a Python `set`), `{"A","B"} <= labels` is
  `False`, falls to the final `else: classification = "degraded"` branch.
  This is a defensible fallback but the system never surfaces *why* it's
  degraded in this specific case (duplicate-label topology error vs. a
  genuine shared-upstream SPOF vs. a missing feed all collapse into the
  same generic `"degraded"` string with no distinguishing detail in the
  API response beyond the accompanying `missing_upstream`/`shared_ancestors`
  internal computation, which is not itself exposed to the API caller).
  MEDIUM-severity, STATIC ANALYSIS — not independently re-executed.

`redundancy_factor` (the `PowerCapacity.redundancy_factor` column, values
`N`/`N+1`/`2N`/`2N+1`) is stored and returned by the capacity GET/PUT
endpoints (confirmed via `grep` — appears in `power.py` lines 611, 624,
642, 690 and the model itself) but is **never read by any redundancy
classification or exception-derivation logic** (confirmed: zero matches for
`redundancy_factor` in `power_capacity.py`, the module that owns
`equipment_power_summary` and `derive_node_capacity_exceptions`). An
operator can set `redundancy_factor="2N"` on a node and it will be echoed
back faithfully forever, but it has no effect on any computed value or
exception. This is a schema/feature-completeness gap: the column exists
because the architecture review's own §14 lists it, but Phase 3's
application logic derives redundancy purely from A/B topology and never
consults it. LOW/MEDIUM severity — decorative field, not a correctness
defect, but a discrepancy between the documented data model's implied
capability and what the system actually computes.

## 12. Shared-Upstream Review

Covered above (Section 11) — the ancestor-set-intersection method
(`upstream_sets[node_a.id] & upstream_sets[node_b.id]`) is the correct
general approach and, by construction, finds *any* shared ancestor, not
just the nearest one (it does not need to find the *lowest* common
ancestor — any non-empty intersection is suff
icient to prove a shared
dependency exists, which is all `REDUNDANCY_DEGRADED` needs). No false
negative found by static analysis. The false-positive risk is the
alarm-fatigue concern in Section 30 F-1, not an incorrect detection.

## 13. Rack/Equipment Integration Review

`create_equipment_feed` and `create_pdu_outlet` both correctly verify the
referenced `Equipment`/`PDU` row exists via `db.get(...)` before creating
the `PowerNode` (Section 7's DB-vs-API distinction applies here — this is
API-enforced, not DB-enforced). No cross-site validation exists anywhere in
this call chain (an equipment item in Site A can be given a power feed and
then connected to a PDU physically modeled as being in Site B — nothing
rejects this). This was not independently re-executed in this audit
(STATIC ANALYSIS) but is a straightforward reading of the code: no site_id
comparison appears anywhere in `power.py` or `power_capacity.py`.

## 14. Authorization / IDOR Review

Every Phase 3 endpoint requires the correct permission scope
(`power:read`/`power:manage`/`capacity:read`/`capacity:manage`) via
`require_permission(...)`, confirmed by reading every route decorator in
`power.py` and `dashboard.py` — none are missing this dependency. This
matches the traceability matrix's claim and was previously tested by
`test_power.py`'s authorization tests (not re-litigated here).

**No endpoint anywhere in `/power/*` or `/dashboard/*` (beyond the
dashboard's own optional site/building/floor/room query-param filters)
performs any object-level ownership or site-scoping check.** Any
authenticated principal holding `power:read` can list, read, and traverse
every `PowerNode`/`PowerConnection` in the entire deployment, regardless of
which site it belongs to. This is consistent with the master prompt's
explicit instruction not to introduce site-scoped RBAC, so it is *not* a
violation of the approved scope — but it is a real latent risk the moment
this system is ever deployed multi-tenant (multiple customer organizations
sharing one instance), and nothing in the implementation guards against
that deployment model being chosen later without someone re-reading this
finding first. MEDIUM severity, STATIC ANALYSIS (matches and reaffirms
`PHASE3_IMPLEMENTATION_RED_TEAM_SCOPE.md`'s own §5, now with the specific
observation that *zero* endpoints have any scoping, not just the dashboard
summary).

IDOR on malformed/nonexistent IDs: `test_malformed_uuid_is_a_clean_422_not_
a_500` already exists in `test_power.py` and was confirmed passing in the
clean 269/269 re-run (Section 3) — this specific sub-case is adequately
covered, contrary to what the original red-team-scope document (written
before this audit) implied might be missing.

## 15. Optimistic Concurrency Review

`PowerConnection` PATCH correctly uses the shared `require_if_match`
dependency (robust parsing, clean 400 on malformed input — confirmed by
reading `concurrency.py`). `PowerCapacity` PUT does **not** use this shared
dependency; it re-implements `If-Match` parsing ad hoc with a bare
`int(if_match.strip().strip('"'))` call and no `try/except`. See Section 23
for the confirmed, executed consequence.

## 16. Idempotency Review

No new idempotency-key-bearing endpoint was introduced by Phase 3
(confirmed by grep — no `Idempotency-Key` handling appears anywhere in
`power.py`/`dashboard.py`). NEW-1 (the Phase 1 idempotency stale-reclaim
race) is genuinely untouched by this phase, consistent with
`PHASE3_GAP_ANALYSIS.md`'s own claim. Not independently re-tested in this
audit (out of scope — nothing new to test).

## 17. Audit / Outbox Review

Every mutating endpoint in `power.py` calls `write_audit_log` and
`write_outbox_event` in the same transaction as the domain write, before
`db.commit()` — confirmed by reading all nine mutating routes. Whether
every audited field is *correct* (as opposed to merely present) was **not**
independently re-verified by direct `audit_log` table inspection in this
audit pass (time-boxed) — this remains an untested item, consistent with
`PHASE3_IMPLEMENTATION_RED_TEAM_SCOPE.md`'s own §6, which already flagged
this gap; this audit did not close it.

## 18. Dashboard / N+1 Review

The N+1 pattern in `dashboard.py`'s `get_summary` (a Python loop calling
`derive_node_capacity_exceptions`/`equipment_power_summary` once per row)
was confirmed present by direct code reading in this audit. **This audit
did not independently re-measure its latency** (the original
implementation report's own 1.9s-at-700-nodes figure was not re-executed
here due to time constraints) — this remains a claim taken on trust from
the self-report, not independently reproduced in this audit pass. The
independent red-team should re-measure it directly rather than trust
either document's number.

## 19. Performance / Scaling Review

Not independently re-executed in this audit (time-boxed; the original
implementation's own 1,000-power-node test was not re-run or extended).
This audit takes no position on whether the master prompt's full target
scale (1,000 racks / 10,000 equipment / 5,000 power nodes / 10,000+
connections) would perform acceptably — this remains **NOT EXECUTED** by
either the original implementation (already disclosed) or this audit.

## 20. API Contract Review

`POWER_TOPOLOGY_INVALID` (defined in `power_capacity.py` line 58) is never
raised or returned anywhere in the codebase (confirmed by grep across
`app/` — the only match is its own definition). It appears in the
traceability matrix's row 17 as one of the "fixed condition codes" the
capacity exception engine implements, but no code path can ever actually
produce it. This is a discrepancy between the documented API contract and
the actual implementation: a client written against the documented
condition-code list would never see this code in practice, which is
arguably fine (no false information reaches the client) but means the
"deterministic condition codes" claim in Section 10 of
`PHASE3_IMPLEMENTATION_REPORT.md` overstates what is actually reachable.
LOW/MEDIUM severity, STATIC ANALYSIS.

## 21. Frontend Review

No client-side recalculation of any authoritative value was found — every
capacity/utilization/demand figure displayed in `PowerTopologyPage.tsx`,
`DashboardPage.tsx`, `EquipmentDetailPage.tsx`, and `RackDetailPage.tsx` is
formatted (`.toFixed(...)`) from a value that came directly from the API
response, never independently computed in the browser. This is a clean
source-of-truth discipline (confirmed by targeted grep across all four
files for `allocated_kw`/`utilization_pct`/`effective_capacity`/
`Math.`/`toFixed`).

One near-miss was investigated and **ruled out**: `EquipmentDetailPage.tsx`
line 319 calls `.toFixed(1)` on `effective_demand_kw` without an inline
null-check at that exact line, but it is correctly wrapped in a parent
`{powerSummaryQuery.data.effective_demand_kw !== null && (...)}` guard
(line 317) — no null-pointer crash risk. Confirmed by reading the
surrounding JSX, not merely the single line.

Not independently re-executed against a live browser in this audit
(time-boxed) — the loading/empty/error/stale-cache states described in the
original red-team scope document remain unverified by this audit pass as
well.

## 22. Migration Review

Migration 0006 was read in full during this audit (STATIC ANALYSIS) and
confirmed structurally sound: purely additive (no `ALTER` on any Phase 1/2
table), correct FK/CHECK/index definitions matching the ORM models exactly,
`_seed_new_permissions()` reused verbatim from migration 0004's own
idempotent pattern. **This audit did not re-execute fresh-install /
upgrade / downgrade / re-upgrade** — that remains a claim from the original
implementation report, not independently reproduced here. The independent
red-team should re-run this validation itself rather than accept either
document's word for it.

## 23. Security / Input Handling Review

**Confirmed by execution:** `PUT /power/nodes/{id}/capacity` with a
malformed (non-integer) `If-Match` header, once a current capacity record
already exists for that node, raises an unhandled Python `ValueError` from
line 680 of `power.py` (`int(if_match.strip().strip('"'))`), which is not
caught by any endpoint-local `try/except` and is not one of the four
specific exception types (`ApiError`, `StarletteHTTPException`,
`RequestValidationError`, `IntegrityError`) that `core/errors.py` maps to a
clean response — it falls through to the registered catch-all
`@app.exception_handler(Exception)`, which returns a generic
`500 Internal Server Error`. This is inconsistent with the *same*
validation already correctly implemented (with a proper `try/except ValueError
-> ApiError(400)`) in `concurrency.py::parse_if_match`, and inconsistent
with `PowerConnection` PATCH, which correctly uses that shared helper via
`Depends(require_if_match)`. `PowerCapacity` PUT bypasses the shared helper
and reimplements the parsing ad hoc, without the same protection.

```
E   ValueError: invalid literal for int() with base 10: 'not-a-number'
    app/api/v1/power.py:680: ValueError
```

(Captured directly from a pytest run against the real app instance with a
real Postgres backend — the httpx `ASGITransport` test client re-raises
unhandled application exceptions rather than swallowing them into an
HTTP response, by design, for debuggability; in a real `uvicorn`-served
deployment this same code path returns the generic 500 JSON body via the
registered catch-all handler instead of a Python traceback, since FastAPI's
registered exception handlers *do* apply there — the underlying defect,
an expected-and-preventable user input error reaching an unhandled
exception rather than the endpoint's own precondition-failure/bad-request
path, is the same either way.)

Severity: MEDIUM (not HIGH, because the resulting 500 leaks no sensitive
information beyond a generic "reference the request ID" message, and the
endpoint still requires proper authorization before this code path is even
reached — but it is a concrete, reproducible violation of the master
prompt's "expected invalid operations should produce deterministic API
errors rather than accidental server errors" requirement).

Other security checks performed by STATIC ANALYSIS only in this audit
(not independently executed): NaN/Infinity capacity values, SQL injection
(no raw string interpolation into SQL found anywhere in the diff — all
queries use SQLAlchemy's parameterized `select()`/`where()` — confirmed by
reading every `db.execute` call in `power.py`/`power_capacity.py`/
`power_graph.py`/`dashboard.py`), mass assignment (every Pydantic input
model is hand-declared with an explicit field list, never derived from the
ORM model directly — confirmed by reading every `*In`/`*Update` class
definition — `version`/`id` cannot be client-supplied on any create
endpoint).

## 24. Error Handling Review

Beyond the Section 23 finding, no other unhandled-exception path was found
by static reading of `power.py`/`dashboard.py`/`power_capacity.py`/
`power_graph.py` — every other user-facing failure mode (not-found,
conflict, cycle, bounded-traversal, retired-node) raises a typed `ApiError`
subclass with an explicit status code. This was not exhaustively
re-executed for every single endpoint in this audit pass; the Section 23
case was found specifically because it stood out during code reading as
the one endpoint that reimplements shared validation logic ad hoc rather
than reusing the already-hardened helper.

## 25. Observability Review

Structured logging exists for the generic `IntegrityError`/`Exception`
catch-all handlers (`core/errors.py`) but Phase 3 introduces no
Phase-3-specific structured log lines of its own (no `logger.info`/
`logger.warning` call appears anywhere in `power.py`, `power_capacity.py`,
`power_graph.py`, or `dashboard.py` — confirmed by grep). A
`GraphTraversalBounded` or `WouldCreateCycle` exception is mapped straight
to a 422 API response with no server-side log line recording that it
happened, meaning an operator cannot currently distinguish "a client made
one bad request" from "a client is repeatedly hitting the traversal bound,
possibly indicating a pathological or adversarial topology" without
parsing HTTP access logs for 422 responses on these specific routes. This
is inconsistent with the master prompt's explicit requirement to log
"invalid topology detection... unexpected graph depth" as named
observability events. MEDIUM severity, STATIC ANALYSIS (grep-confirmed
absence, not independently tested against a running log pipeline).

Also newly noted (Section 4): nothing in the repository documents or
verifies that Redis must be running for the *test* suite to pass cleanly —
this audit's own environment did not have it running at first, and the
resulting spurious 19 test failures (Section 3) could easily be mistaken
for real Phase 3 regressions by a less careful reviewer. This is a minor
developer-experience gap, not a Phase 3 code defect.

## 26. Test-Quality Review

The existing 66 Phase 3 tests were read (not just counted) during this
audit. They are behavioral, not merely structural: the DB-constraint tests
genuinely bypass the API and assert on `IntegrityError`/constraint names;
the concurrency tests genuinely use independent `AsyncSession`s against a
real Postgres instance (the `per_request_client` pattern), not sequential
simulation; the unit tests for capacity/redundancy assert on specific
numeric outcomes and `data_quality` values, not just "it didn't crash."

However, **none of the existing 66 tests would have caught either of the
two most severe findings in this audit** (Sections 9 and 10):

- The cycle-prevention test suite (`test_power_graph.py`) tests concurrent
  creation of the *same* edge (or its exact reverse) between the *same*
  two nodes — it never constructs the independent-pair, pre-existing-edge
  scenario Section 9 relies on. This is exactly the gap
  `PHASE3_IMPLEMENTATION_RED_TEAM_SCOPE.md` §2 already flagged as untested
  ("concurrent contention across 3+ distinct node pairs") — this audit
  confirms that gap is not just untested but actually exploitable.
- The capacity-exception tests (`test_power_capacity.py`) test the
  "unknown" `data_quality` value in isolation but never call
  `derive_node_capacity_exceptions` on a node whose allocation (not
  capacity) is unknown while its own capacity is known — the specific
  combination Section 10 relies on. Every existing test either has both
  known or both unknown; none has capacity-known-but-allocation-unknown.

This is the central lesson of this audit: **passing tests describe the
paths the test author thought to construct, not the space of all possible
inputs and interleavings** — both defects above are real, executable,
production-relevant, and invisible to the existing suite.

## 27. Source-of-Truth Review

No violation found (see Section 21 — frontend never recalculates capacity/
redundancy/utilization independently). Backend-side: `get_capacity_figures`
is the single function every capacity-reading endpoint calls (confirmed —
`get_node_capacity`, `list_capacity_exceptions` via
`derive_node_capacity_exceptions`, and `equipment_power_summary` all route
through it or `compute_allocated_kw` directly, never a separate
recomputation). This part of the architecture is sound.

## 28. Architectural Consistency Review

No Phase 4 dependency, no microservice, no duplicated audit/outbox/
idempotency system, no hidden UI-as-source-of-truth behavior found.
`ManagedAsset` remains the single identity anchor for every asset-backed
power node. This part of the self-report's claims held up under this
audit's inspection.

## 29. Findings (Summary Table)

| ID | Severity | Component | Status |
|----|----------|-----------|--------|
| F-C1 | **CRITICAL** | `power_graph.py` cycle prevention | **CONFIRMED (EXECUTED)** — Section 9 |
| F-H1 | **HIGH** | `power_capacity.py` exception derivation | **CONFIRMED (EXECUTED)** — Section 10 |
| F-H2 | **HIGH** | `power.py` node retirement / `power_graph.py` traversal | **CONFIRMED (EXECUTED)** — Section 30 F-2 |
| F-M1 | MEDIUM | `power.py` capacity `If-Match` parsing | **CONFIRMED (EXECUTED)** — Section 23 |
| F-M2 | MEDIUM | `power.py`/`power_capacity.py` redundancy classification | STRONGLY SUSPECTED (STATIC) — Section 30 F-1 |
| F-M3 | MEDIUM | `power_node`/`pdu_outlet`/`managed_asset` FK typing | CONFIRMED (STATIC) — Section 7 |
| F-M4 | MEDIUM | `/power/*` authorization scope | CONFIRMED (STATIC, by design) — Section 14 |
| F-M5 | MEDIUM | Observability — no structured logging for graph/capacity failures | CONFIRMED (STATIC) — Section 25 |
| F-L1 | LOW | `redundancy_factor` field unused | CONFIRMED (STATIC) — Section 11 |
| F-L2 | LOW | `POWER_TOPOLOGY_INVALID` unreachable | CONFIRMED (STATIC) — Section 20 |
| F-L3 | LOW | `managed_asset` CASCADE vs `power_connection` RESTRICT | CONFIRMED (STATIC), currently unreachable — Section 7 |
| F-I1 | INFO | Dashboard N+1 latency not re-measured this pass | NOT EXECUTED — Section 18 |
| F-I2 | INFO | Full-scale performance not re-measured this pass | NOT EXECUTED — Section 19 |
| F-I3 | INFO | Migration re-validation not re-executed this pass | NOT EXECUTED — Section 22 |

Full narrative detail for each is in the numbered sections above, not
repeated here.

## 30. Known Limitations — Independent Reassessment

The five limitations `PHASE3_IMPLEMENTATION_REPORT.md` previously
disclosed are reassessed here rather than accepted at face value:

1. **Dashboard summary N+1 query** — previously classified as a disclosed
   performance limitation. This audit did not re-measure it, but confirms
   by code reading that the loop structure is exactly as described.
   Reassessment: **operational risk**, not harmless — at the master
   prompt's own target scale this endpoint could plausibly become slow
   enough to affect perceived application responsiveness or exhaust
   connection-pool capacity under concurrent dashboard viewers, not merely
   "a bit slow." Not re-classified as a correctness defect.

2. **No dedicated capacity-edit UI form** — reassessed as genuinely
   harmless technical debt; the API path this exercises (direct PUT) is
   fully functional and tested, and the *only* consequence found in this
   audit is UX inconvenience for an operator, not a data-integrity or
   security concern.

3. **Full target-scale performance not verified** — reassessed as an
   **operational risk that is more serious than the original disclosure
   implies**, precisely because of the new F-H1 finding (Section 10): the
   silent-exception-masking defect only manifests at exactly the scale
   that was never tested, meaning the untested-performance limitation and
   the capacity-masking defect compound each other — a real production
   deployment at target scale could have overloaded nodes that are both
   slow to report on *and* invisible in the exception list.

4. **No organization-scoped dashboard filtering** — reassessed alongside
   the new F-M4 finding (Section 14): the gap is broader than originally
   disclosed. It is not just the dashboard that lacks scoping — *no*
   `/power/*` endpoint has any site/organization filter at all. Reassessed
   as a **deployment-model risk** (real today only if deployed
   multi-tenant) rather than a pure reporting-scope limitation.

5. **No telemetry/alarm/AI/microservices** — reassessed as correctly
   out-of-scope per the master prompt's own explicit instruction; nothing
   in this audit found a hidden dependency on any of these. Harmless by
   design.

## 31. Independent Red-Team Priorities

In priority order, for whoever performs the actual independent validation:

1. **Reproduce F-C1 (Section 9) independently**, ideally also with 3+
   concurrent transactions closing a longer cycle, and with a disconnect
   racing against two cycle-closing creates. This is the highest-value,
   highest-confidence, most severe finding in this audit and should be
   verified first.
2. **Reproduce F-H1 (Section 10)** and then extend it: does the same
   masking occur for `REDUNDANCY_DEGRADED`/`POWER_PATH_MISSING` detection
   when `equipment_power_summary`'s own upstream traversal hits a bound
   (as opposed to `compute_allocated_kw`'s downstream traversal, which is
   the specific path this audit tested)?
3. **Reproduce F-H2 (retirement gap)** and determine the intended
   semantics: should a retired node's connections auto-disconnect, should
   traversal/allocation exclude retired nodes, or is the current
   "retirement only blocks new connections" behavior actually the intended
   design that was simply under-documented? This audit found the *behavior*
   but did not find any specification clarifying the *intent* — that
   ambiguity itself should be resolved, not just the code.
4. Re-measure the dashboard N+1 latency and full-scale performance
   independently (Sections 18-19) — do not trust either this document's or
   the implementation report's numbers without re-running them.
5. Re-run migration fresh/upgrade/downgrade/re-upgrade independently
   (Section 22) — not re-executed in this audit pass.
6. Attempt the audit-log field-content verification this audit did not
   perform (Section 17).
7. Attack F-M1 (Section 23) directly via HTTP against a running `uvicorn`
   instance (not just the ASGI test transport) to confirm the production
   500 response body matches what this audit infers from the registered
   exception handler.

## 32. Recommended Corrections (NOT IMPLEMENTED)

Documented for the record only — per the no-fix rule, none of these were
applied during this audit:

- F-C1: adopt one of the three approaches sketched in Section 9 (broader
  locking, `SERIALIZABLE` isolation, or a global advisory lock on
  connection creation). Recommend starting with the global advisory lock
  as the simplest correct fix, then optimizing to broader/finer-grained
  locking only if connection-creation throughput is later measured to be
  a real bottleneck.
- F-H1: change `derive_node_capacity_exceptions` to branch on
  `figures.data_quality` directly rather than re-deriving a narrower
  condition from `effective_capacity_kw`/`utilization_pct` alone; emit a
  distinct exception (or reuse `POWER_TOPOLOGY_INVALID`, closing F-L2 at
  the same time) whenever `data_quality == "unknown"` regardless of which
  side of the calculation caused it.
- F-H2: decide and document the intended retirement semantics (see red-
  team priority 3 above), then implement whichever is chosen — most likely
  either auto-disconnecting active connections on retirement, or excluding
  retired nodes from traversal/allocation via an additional filter
  alongside the existing `effective_to IS NULL` checks.
- F-M1: replace the ad hoc `int(if_match...)` call in `set_node_capacity`
  with the existing `parse_if_match`/`require_if_match` helper, exactly as
  the `PowerConnection` PATCH endpoint already does.
- F-M2: consider exposing the specific reason a redundancy classification
  is `"degraded"` (shared ancestor vs. missing upstream vs. duplicate feed
  label) rather than a single opaque string, and consider whether
  `REDUNDANCY_DEGRADED` needs a lower-severity variant for the "shared
  utility feed only, everything else healthy" case to avoid alarm fatigue.
- F-M3: add an application-layer (not necessarily DB-layer, given
  Postgres's limited support for polymorphic FK constraints) guard on
  outlet/feed creation double-checking asset subtype, and document the
  decision either way.
- F-L1: either wire `redundancy_factor` into the classification logic or
  document explicitly (in the module docstring, not just this audit) that
  it is operator-facing metadata only and intentionally not consumed by
  Phase 3's own computations.

## 33. Self-Audit Conclusion

**A. What appears solid:** the domain model's structural shape (real FKs,
correct `ManagedAsset` subtyping, correct CHECK constraints for everything
a single-row constraint *can* express), the frontend's source-of-truth
discipline, the absence of any Phase 4/telemetry/microservice scope creep,
and the bulk of the existing 66 tests' behavioral quality.

**B. What is questionable:** the redundancy classifier's real-world
operational usefulness given how common single-utility-feed topologies
are (F-M2); whether the retirement semantics found (F-H2) are a bug or an
under-specified intentional design; whether the dashboard N+1 and untested
full-scale performance compound with F-H1 into a more serious combined
risk than either alone (Section 30 item 3).

**C. What is definitely defective:** F-C1 (cycle prevention, CRITICAL,
executed), F-H1 (capacity-exception masking, HIGH, executed), F-H2
(retirement not excluding node from graph, HIGH, executed), F-M1
(malformed If-Match unhandled exception, MEDIUM, executed).

**D. What remains unverified** (by this audit; some previously claimed by
the original implementation and not re-checked here): full-scale
performance, dashboard N+1 latency at scale, migration re-validation,
audit-log field-content correctness, real-browser data-quality-label
rendering, Graph C/H through the full HTTP API, 3+-way concurrent cycle
races, NaN/Infinity capacity input handling.

**E. Highest-risk areas for the independent red-team:** F-C1 above all
else — a confirmed, reproducible cycle in a system whose entire purpose is
to model an acyclic power distribution graph is a correctness failure at
the core of Phase 3's stated mission, not a peripheral concern.

**F. Tests the independent red-team MUST execute:** the exact
reproduction in Section 9 (or an equivalent independently constructed
one), the exact reproduction in Section 10, a direct HTTP request against
a running `uvicorn` instance reproducing Section 23's malformed `If-Match`
case, and a fresh, from-scratch migration validation run.

**G. Recommended corrections:** documented in Section 32, not implemented.

This audit does not approve, certify, or sign off on Phase 3. It identifies
what it found. The decision of whether Phase 3 may proceed belongs to the
independent red-team gate, not to this document.
