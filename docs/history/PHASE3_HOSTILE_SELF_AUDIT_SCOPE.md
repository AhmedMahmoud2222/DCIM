# Phase 3 Hostile Self-Audit — Scope & Reproduction Companion

Companion to `PHASE3_HOSTILE_SELF_AUDIT.md`. That document is the full
narrative audit; this document is the compact, action-oriented checklist an
independent red-team can work from directly, with exact reproduction steps
for every EXECUTED finding so they do not have to reverse-engineer them
from prose. This is still a self-audit artifact, not an independent
validation, and confirms nothing on its own.

## What this audit covered

Full read of every file changed by commit `4606810` (28 files); execution
of the full 269-test backend suite in isolation; three targeted diagnostic
test scripts run against a real PostgreSQL 16 instance (not mocked);
one standalone asyncio script directly exercising the application's own
concurrency-control functions (`lock_node_pair_in_canonical_order`,
`assert_would_not_create_cycle`) with manually synchronized transaction
timing. No application code, tests, migrations, or configuration were
modified. All temporary diagnostic files were deleted before this document
was written (confirmed by `git status` showing a clean tree both before and
after).

## What this audit did NOT cover (explicitly — do not assume otherwise)

- Full master-prompt-target-scale performance (1,000 racks / 10,000
  equipment / 5,000 power nodes / 10,000+ connections) — **NOT EXECUTED**.
- Dashboard N+1 latency re-measurement — **NOT EXECUTED** (confirmed
  present by code reading only).
- Migration fresh/upgrade/downgrade/re-upgrade re-validation — **NOT
  EXECUTED** in this pass (relies on the original implementation's own
  claim).
- Real-browser validation of any kind — **NOT EXECUTED**.
- Audit-log field-content correctness (only presence, not per-field
  correctness, was inspected, and only by code reading, not by direct
  `audit_log` table query) — **NOT EXECUTED**.
- 3+-transaction concurrent cycle races (only the minimal 2-transaction,
  4-node case was constructed and executed) — **NOT EXECUTED** beyond the
  minimal case.
- NaN/Infinity/extreme-value capacity input handling — **NOT EXECUTED**.
- Cross-organization data leakage in a genuinely multi-tenant dataset —
  **NOT EXECUTED** (the absence of any scoping code was confirmed by
  reading; the practical consequence in a real multi-tenant deployment was
  not demonstrated against live data).

## Priority-ordered target list for the independent red-team

### P0 — Reproduce first

**T1. Independent-pair concurrent cycle creation (F-C1, CRITICAL)**

Reproduction recipe (mirrors this audit's own script):
1. Create 4 `PowerNode` rows (any type valid without an asset reference,
   e.g. `utility_intake`) — call them N1, N2, N3, N4.
2. Commit two edges: N2→N3 and N4→N1.
3. Start two concurrent transactions:
   - Tx1: lock `canonical_order(N1, N2)`, run `assert_would_not_create_
     cycle(source=N1, target=N2)`, then **pause before committing**.
   - Tx2: lock `canonical_order(N3, N4)`, run `assert_would_not_create_
     cycle(source=N3, target=N4)`, then **pause before committing**.
4. Confirm both cycle checks report "no cycle" (they will — this is
   expected and is not itself the bug).
5. Let both transactions commit.
6. Check whether the resulting graph is cyclic by traversing downstream
   from N2 (**not** from N1 — `_traverse` unconditionally discards its own
   root from the result, so checking `N1 in downstream(N1)` will always be
   `False` even when a cycle genuinely exists; this audit's own first
   verification attempt made exactly this mistake before catching it).
   `N1 in downstream(N2)` being `True` confirms the cycle.

Expected (per architecture claim): this should be impossible. Actual (this
audit, executed): it happens every time this exact interleaving is forced.

**T2. Capacity-exception masking under bounded traversal (F-H1, HIGH)**

Reproduction recipe:
1. Monkeypatch (or, for a black-box test, actually construct)
   `MAX_TRAVERSAL_NODES` to a small value, or build a downstream chain that
   genuinely exceeds 5,000 nodes at the real default.
2. Give the root node a known `rated_capacity_kw`.
3. Build a downstream chain longer than the bound.
4. Call `GET /power/capacity-exceptions` and `GET /power/nodes/{root}/
   capacity`.
5. Confirm: capacity response shows `data_quality: "unknown"`,
   `allocated_kw: null`, `utilization_pct: null` — but the exceptions
   endpoint returns nothing for this node at all, not even
   `CAPACITY_UNKNOWN`.

### P1 — Reproduce next

**T3. Retired node still counted (F-H2, HIGH)**

1. Create Utility → PDU chain, give the PDU a `rated_capacity_kw`.
2. Retire the PDU (`POST /power/nodes/{pdu_id}/retire`).
3. `GET /power/nodes/{utility_id}/downstream` — confirm the retired PDU is
   still listed.
4. `GET /power/nodes/{utility_id}/capacity` — confirm `allocated_kw` still
   includes the retired PDU's capacity.
5. Attempt a new connection FROM the retired PDU — confirm this (and only
   this) is correctly rejected with 422.

**T4. Malformed If-Match on capacity PUT (F-M1, MEDIUM)**

1. `PUT /power/nodes/{id}/capacity` once (no `If-Match` needed — first
   write).
2. `PUT /power/nodes/{id}/capacity` again with header `If-Match:
   not-a-number`.
3. Against a real running `uvicorn` server (not the ASGI test transport,
   which re-raises for debuggability rather than returning the production
   response) — confirm the actual HTTP response code and body. This
   audit's own test-mode execution surfaced the underlying unhandled
   `ValueError` directly rather than the wrapped 500 response; the
   independent red-team should confirm the real production response shape
   against a live server.

### P2 — Re-verify claims neither document independently confirmed

- T5. Re-run migration 0006 fresh install / upgrade / downgrade /
  re-upgrade from scratch.
- T6. Re-measure `GET /dashboard/summary` latency at 700+ and 5,000+ power
  nodes.
- T7. Query `audit_log` directly after each Phase 3 mutation type and
  verify every expected field is populated, not just that a row exists.
- T8. Extend T1 to 3+ concurrent transactions closing a longer cycle, and
  to a disconnect racing against two cycle-closing creates.

## Files touched by this audit (all reverted/removed before conclusion)

- `backend/tests/api/_hostile_audit_tmp.py` — created, run, then deleted.
  Confirmed absent by final `git status`.
- `/tmp/.../scratchpad/hostile_audit_cycle_race.py` — created outside the
  repository, run, left in the scratchpad (not part of the repo; not
  subject to the no-permanent-files rule, which applies to files inside
  the repository).
- No application file, test file, migration, or configuration file inside
  the repository was modified. Confirmed by `git status --short` and
  `git diff --stat` both returning empty immediately before this document
  was written.

## Bottom line

Two confirmed, executed, high/critical-severity defects exist in code that
both `PHASE3_IMPLEMENTATION_REPORT.md` and `PHASE3_TRACEABILITY_MATRIX.md`
described as correctly implemented and tested. This is not a reason to
distrust every other claim in those documents wholesale — most of what
this audit checked did hold up — but it is a concrete demonstration of why
an independent, hostile review is required before Phase 3 can be
considered done, and why "269 tests pass" is not, by itself, sufficient
evidence of correctness.
