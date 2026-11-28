# NET-INTEGRITY-01 — Post-merge follow-up acceptance contract

**Status:** Draft scope only; implementation and independent review are pending.  
**Base at branch creation:** `main@df6e3bd1f5ee919a5fa7332158b1f93eb7943f8a` (squash merge of PR #371).  
**Related:** Merged PR #371; historical network delivery issue #101 (closed); Codacy governance #330 (open); staging coordination #300 (open).  
**Delivery roles selected by owner:** AGY (implementation); Claude (independent review). This file does **not** invoke either agent.

## Workstream A — Patch-panel transitive impact propagation (P3, potentially P2 if operationally relied upon)

Current `backend/app/application/impact_service.py::_network_bfs` walks active
`PortConnection` rows but not the explicit `PortPassThrough` /
`PortPassThroughMember` edges used by the accepted cable-trace model. PR #371
removed a test's invalid same-port source/target chain; its replacement tests
only a direct hop. Do **not** restore the invalid chain.

Acceptance:
1. Traverse only permitted authoritative edges: active `PortConnection` plus
   valid explicit pass-through relationships; respect link lifecycles and do
   not turn merely inferred/discovered neighbors into confirmed connections.
2. Cover `switch port -> panel front port -> pass-through -> panel rear port
   -> server port` and its reverse, with two distinct panel ports.
   Define hop semantics explicitly (physical edge vs pass-through edge) and
   assert `directly_impacted`, `indirectly_impacted`, impact types and
   affected services accordingly.
3. Cover disconnected/pass-through-removed cases, loops, duplicate paths,
   inactive/faulted links, redundant equipment, and both traversal directions.
   Preserve bounded `MAX_TRAVERSAL_DEPTH`/`MAX_TRAVERSAL_NODES`,
   visited-set/cycle protection and existing access-scoping contracts.
4. Update the misleading
   `test_network_port_failure_transitive_hop_through_patch_panel`
   to exercise genuine transitive behavior; add API/integration regression
   tests and a negative case showing that a single port cannot be reused
   as the source and target of distinct `PortConnection` rows.
5. Do not weaken PR #371 endpoint exclusivity, cable ownership,
   advisory-lock sequencing, error handling or audit/outbox atomicity.

## Workstream B — Narrow Codacy test-harness exclusions (P3)

Current `.codacy.yml` excludes the entire
`backend/tests/conftest.py` from `ruff` and `pylintpython3` because an
imported `race` fixture is intentionally discovered by pytest.

Acceptance:
1. Replace those **file-wide engine exclusions** with an appropriately
   targeted and demonstrably supported fixture-registration approach,
   e.g., an owned fixture definition in `conftest.py` or a verified
   single-line Pylint suppression. Avoid circular imports and behavior
   changes to the concurrency barrier.
2. Preserve the specific PostgreSQL/T-SQL engine mismatch handling for
   `backend/scripts/diagnose_port_connection_conflicts.sql`, unless an
   independently verified narrower alternative exists.
3. Do not add repository-wide exclusion paths, disable security analyzers
   or exclude application production source from scanning.
4. Prove `race` still resolves in all relevant tests, including the
   NET-INTEGRITY-01 parallel-race tests, and verify Ruff, Pylint/Codacy,
   Mypy where relevant and all required exact-HEAD CI gates.
5. Reconcile findings with existing issue #330 rather than treating its
   outdated proposed broad exclusion list as an approved configuration.

## Workstream C — Historical-conflict release evidence (P1 preventive gate)

The existing read-only
`backend/scripts/diagnose_port_connection_conflicts.sql` reports P1-P5.
No staging or production data qualification is represented by merging code.

This follow-up PR **may document a read-only staging qualification
procedure and sanitized evidence template only**. It must not run SQL,
connect to staging/production, migrate, repair data, deploy or change
release gates. Staging execution requires separate operator/DBA authorization
and a separately tracked acceptance record. Record environment fingerprint,
revision/SHA, query results, each P1-P5 exception, assessment and verifier.
Do not declare the environment clean without observed evidence.

## Review and release gates

- Keep this **one existing follow-up PR** open as Draft while AGY implements.
  Reviewers add findings and exact-head verdicts **on this PR**, not a
  replacement PR; agent instructions themselves stay in the owner's chat.
- Require Claude's independent substantive review naming the **current full
  HEAD SHA**, each finding's severity, direct code/test evidence and one
  final line matching `AGENTS.md` exactly.
- Require complete exact-HEAD CI, no unresolved P1/P2 findings, and no
  unresolved review threads before requesting the owner to consider merge.
- A GitHub `COMMENTED` review by the PR author is *not* a formal independent
  GitHub approval. Do not conflate technical verdict and branch protection.
- **No merge, deployment, production SQL, historical data mutation or issue
  closure is authorized.** Issue #214's Migration 0057 production hold and
  written owner + DBA approval requirement remain unchanged.
