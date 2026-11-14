# DCIM historical archive index

This directory holds the original, dated architecture, phase, implementation, independent-review and audit reports that were previously stored at the repository root. They were relocated here during the 2026-11-13 documentation consolidation so that `docs/` retains the living canonical guides (navigation, operations, project status, release guidance, architecture overview, ADRs and contracts) while the dated evidence remains intact and traceable.

## Preservation principles

- **No historical report was deleted.** Every file below is a verbatim relocation of a root-level report; content, original findings, independent reviews, audit evidence, benchmarks, authorizations and commit SHAs are preserved unchanged.
- **Distinct documents are retained even where subjects overlap.** Overlapping titles are not treated as duplicates: each report carries its own baseline SHA, reviewer provenance, sequence, severity and correction chronology.
- **Git commit permalinks remain unchanged.** Relocation does not alter history. Original branch-relative URLs cannot be redirected by GitHub; use the [old-to-new path mapping](../documentation-moves.json) or the original commit permalink.
- **Relocation is not a status change.** Archiving a report here does not close a finding, lift a hold, or certify implementation. Migration 0057 remains on production HOLD (Issue #214): representative staging qualification and separate written owner **and** DBA approval are required before any production execution.

## Relocated reports (55)

| Delivery stream | Reports (original path → `docs/history/`) |
|---|---|
| Architecture (Phase 0) | `ARCHITECTURE_REVIEW.md`, `ARCHITECTURE_REVISION_REPORT.md`, `ARCHITECTURE_RED_TEAM_REPORT.md`, `ARCHITECTURE_CHANGE_MATRIX.md`, `ARCHITECTURE_TARGETED_REVISION_REPORT.md`, `FINAL_ARCHITECTURE_VALIDATION_REPORT.md` |
| Phase 1 — foundation | `PHASE1_BASELINE.md`, `PHASE1_IMPLEMENTATION.md`, `PHASE1_IMPLEMENTATION_REPORT.md`, `PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md`, `PHASE1_CORRECTION_REPORT.md`, `PHASE1_DEVIATIONS.md`, `PHASE1_TRACEABILITY_MATRIX.md`, `PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md` |
| Phase 2 — physical/spatial | `PHASE2_GAP_ANALYSIS.md`, `PHASE2_IMPLEMENTATION_REPORT.md`, `PHASE2_CORRECTION_REPORT.md`, `PHASE2_NESTED_SVG_CORRECTION_REPORT.md`, `PHASE2_INDEPENDENT_RED_TEAM_REPORT.md`, `PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md`, `PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_2_REPORT.md`, `PHASE2_IMPLEMENTATION_RED_TEAM_SCOPE.md`, `PHASE2_TRACEABILITY_MATRIX.md` |
| Phase 3 — power/capacity | `PHASE3_GAP_ANALYSIS.md`, `PHASE3_IMPLEMENTATION_REPORT.md`, `PHASE3_CORRECTION_DESIGN.md`, `PHASE3_CORRECTION_DESIGN_SCOPE.md`, `PHASE3_CORRECTION_IMPLEMENTATION.md`, `PHASE3_HOSTILE_SELF_AUDIT.md`, `PHASE3_HOSTILE_SELF_AUDIT_SCOPE.md`, `PHASE3_INDEPENDENT_VALIDATION_REPORT.md`, `PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md`, `PHASE3_CORRECTION_HOSTILE_REAUDIT.md`, `PHASE3_CORRECTION_HOSTILE_REAUDIT_SCOPE.md`, `PHASE3_N1_CORRECTION_REPORT.md`, `PHASE3_IMPLEMENTATION_RED_TEAM_SCOPE.md`, `PHASE3_TRACEABILITY_MATRIX.md` |
| Phase 8 — integration/collectors | `PHASE8_GAP_ANALYSIS.md`, `PHASE8_ARCHITECTURE_CLARIFICATION.md`, `PHASE8_EDGE_COLLECTOR_CONTRACT.md`, `PHASE8_IMPLEMENTATION_REPORT.md`, `PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md`, `PHASE8_INDEPENDENT_RED_TEAM_REPORT.md`, `PHASE8_BLOCKING_CORRECTION_REPORT.md`, `PHASE8_FINAL_CLOSURE_VALIDATION.md`, `PHASE8_TRACEABILITY_MATRIX.md` |
| Phase 10 — catalog/instantiation/live impact | `PHASE10_INDEPENDENT_AUDIT_REPORT.md`, `PHASE10_INTEGRATION_REPORT.md` |
| Phase 11 — Edge security | `PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md`, `PHASE11_SECURITY_REMEDIATION_PLAN.md` |
| MVP / branch planning | `DCIM_MVP_V0_1_PLAN.md`, `MVP_DASHBOARD_OPERATIONAL_UI_REPORT.md`, `MVP_RETENTION_MONITORING_VALIDATION.md`, `BRANCH_RECONCILIATION_REPORT.md`, `PRE_MVP_CONSOLIDATION_REPORT.md` |

## How to read this archive

Start with the [consolidation reading guide](../HISTORY_AND_EVIDENCE.md), which consolidates the reading order for original architecture/phase documents without rewriting their audit results. The [classification and consolidation report](../DOCUMENTATION_CONSOLIDATION.md) records the before/after inventory, document hashes and preservation decisions against `main@b6947782571d07c84c3fbd899ef5f28f484cdb04`.

The complete original-to-new path mapping is maintained in [documentation-moves.json](../documentation-moves.json). Local and CI validation evidence is recorded in [DOCUMENTATION_VALIDATION.md](../DOCUMENTATION_VALIDATION.md).

## Safety constraints

- Original historical and security evidence is preserved verbatim.
- Existing architecture and access-control decisions are not altered by this relocation.
- CI and branch-protection requirements are unaffected.
- Independent review and approval requirements remain in force.
- Issue #214 — migration 0057 remains on production HOLD. No migrations, production deployment, security-control weakening or unrelated application changes are authorized by this documentation consolidation.
