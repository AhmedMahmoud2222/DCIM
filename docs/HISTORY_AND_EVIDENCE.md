# DCIM history, architecture and assurance — reading guide

**Consolidated guide (2026-11-13)** to the archived architecture, phase, implementation and independent-review reports. This page is an **interpretive map**, not a replacement review, re-certification or claim that old findings remain open/closed on today's code.

The original reports are retained in [the dated history index](history/README.md), with a [complete old-to-new path mapping](documentation-moves.json). The [consolidation inventory](DOCUMENTATION_CONSOLIDATION.md) records classification, hashes and preservation decisions against `main@b6947782571d07c84c3fbd899ef5f28f484cdb04`. Similar titles alone are not a safe basis for deletion.

## The short project story

| Historical stream | What changed at that stage | Begin with | Then read |
| --- | --- | --- | --- |
| Architecture (Phase 0) | Domain identity, referential integrity, placement, integration boundaries and review-driven revisions | [Architecture specification](history/ARCHITECTURE_REVIEW.md) | [Change matrix](history/ARCHITECTURE_CHANGE_MATRIX.md), [final architecture validation](history/FINAL_ARCHITECTURE_VALIDATION_REPORT.md) |
| Phase 1 — foundation | Identity, auth, audit/retention, outbox and schema; later audit-immutability and idempotency concurrency fixes | [Implementation](history/PHASE1_IMPLEMENTATION.md) | [Correction](history/PHASE1_CORRECTION_REPORT.md), [final hostile validation](history/PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md) |
| Phase 2 — physical/spatial | Rack/equipment and temporal placement, floor plans and import validation; nested-SVG hardening | [Implementation](history/PHASE2_IMPLEMENTATION_REPORT.md) | [Correction](history/PHASE2_CORRECTION_REPORT.md), [SVG correction](history/PHASE2_NESTED_SVG_CORRECTION_REPORT.md), [independent revalidation #2](history/PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_2_REPORT.md) |
| Phase 3 — power/capacity | Power graph, PDU/UPS/generator/panel objects, capacity and operational dashboard; graph/transaction corrections | [Implementation](history/PHASE3_IMPLEMENTATION_REPORT.md) | [Correction implementation](history/PHASE3_CORRECTION_IMPLEMENTATION.md), [final independent re-audit](history/PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md) |
| Phase 8 — integration/collectors | Driver framework, collectors, profile/mapping boundaries and telemetry intake | [Implementation](history/PHASE8_IMPLEMENTATION_REPORT.md) | [Edge contract](history/PHASE8_EDGE_COLLECTOR_CONTRACT.md), [blocking corrections](history/PHASE8_BLOCKING_CORRECTION_REPORT.md), [independent closure](history/PHASE8_FINAL_CLOSURE_VALIDATION.md) |
| Phase 10 — catalog/instantiation/live impact | Catalog designer, physical instantiation, live overlays, test and integration corrections | [Integration report](history/PHASE10_INTEGRATION_REPORT.md) | [Original independent audit](history/PHASE10_INDEPENDENT_AUDIT_REPORT.md), [subsequent status addendum](AUDIT_STATUS.md) |
| Phase 11 — Edge security | Security findings, remediation work packages and owner decision gates | [Security assessment](history/PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md) | [Remediation plan](history/PHASE11_SECURITY_REMEDIATION_PLAN.md), [security acceptance](security/ACCEPTANCE_GATE.md) |

The chronological labels reflect the historical delivery/reporting sequence; they are **not** a substitute for current R1–R5 roadmap stages or an exact-head release status. The primary guides identify their evidence baselines: [project status](PROJECT_STATUS.md), [architecture overview](ARCHITECTURE_OVERVIEW.md), and the actual source on `main` should be checked.

## How the overlapping records relate

- **Architecture specification vs revision/red-team/change-matrix documents:** the specification describes design; [v1.0–1.1 revision](history/ARCHITECTURE_REVISION_REPORT.md), [v1.1 red-team](history/ARCHITECTURE_RED_TEAM_REPORT.md), [targeted revision](history/ARCHITECTURE_TARGETED_REVISION_REPORT.md), and [change matrix](history/ARCHITECTURE_CHANGE_MATRIX.md) document *why and how* it changed. Treating these as identical duplicates would erase findings/dispositions.
- **Phase 1 implementation vs implementation report:** [implementation](history/PHASE1_IMPLEMENTATION.md) and [implementation report](history/PHASE1_IMPLEMENTATION_REPORT.md) overlap in scope. Both explicitly reference the later [independent red-team](history/PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md) which contradicted original audit/idempotency guarantees; follow through to the correction and final validation before citing those guarantees.
- **Phase 2 independent reports:** [initial review](history/PHASE2_INDEPENDENT_RED_TEAM_REPORT.md), [first revalidation](history/PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md), and [second revalidation](history/PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_2_REPORT.md) target different correction baselines, including the later deeply nested SVG issue. They are a review trail, not redundant approvals.
- **Phase 3 review sequence:** [initial correction design](history/PHASE3_CORRECTION_DESIGN.md), [hostile self-audit](history/PHASE3_HOSTILE_SELF_AUDIT.md), [independent validation](history/PHASE3_INDEPENDENT_VALIDATION_REPORT.md), and [final re-audit](history/PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md) have different authorship, scope and verdict boundaries.
- **Phase 10 audit vs integration:** the original independent audit and the later remediation/integration report refer to **different historical source SHAs**. Read [AUDIT_STATUS.md](AUDIT_STATUS.md) and [PROJECT_STATUS.md](PROJECT_STATUS.md) to interpret them. For example, an old statement that PR #29 was unmerged was true at its baseline; it is **not** the status on 2026-11-13.
- **Security assessment vs remediation plan:** findings, proposed work and actual implemented controls are different states. Read the issue/PR and exact-head validation before marking a security control closed.

## Evidence inventory by function

| Evidence type | Example and intent |
| --- | --- |
| Baselines and scope definition | [Phase 1 baseline](history/PHASE1_BASELINE.md), [Phase 2 gap analysis](history/PHASE2_GAP_ANALYSIS.md), [Phase 3 gap analysis](history/PHASE3_GAP_ANALYSIS.md), [Phase 8 gap analysis](history/PHASE8_GAP_ANALYSIS.md) |
| Deviations and traceability | [Phase 1 deviations](history/PHASE1_DEVIATIONS.md), [Phase 1 traceability](history/PHASE1_TRACEABILITY_MATRIX.md), [Phase 2 traceability](history/PHASE2_TRACEABILITY_MATRIX.md), [Phase 3 traceability](history/PHASE3_TRACEABILITY_MATRIX.md), [Phase 8 traceability](history/PHASE8_TRACEABILITY_MATRIX.md) |
| Proposed actions / design | [Phase 3 correction-design scope](history/PHASE3_CORRECTION_DESIGN_SCOPE.md), [Phase 3 correction design](history/PHASE3_CORRECTION_DESIGN.md), [Phase 8 architecture clarification](history/PHASE8_ARCHITECTURE_CLARIFICATION.md) |
| Independent review and follow-up | [Phase 1 implementation red-team](history/PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md), [Phase 2 independent review](history/PHASE2_INDEPENDENT_RED_TEAM_REPORT.md), [Phase 3 final closure](history/PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md), [Phase 8 final closure](history/PHASE8_FINAL_CLOSURE_VALIDATION.md) |
| Integration and release sequencing | [Branch reconciliation](history/BRANCH_RECONCILIATION_REPORT.md), [pre-MVP consolidation](history/PRE_MVP_CONSOLIDATION_REPORT.md), [MVP v0.1 plan](history/DCIM_MVP_V0_1_PLAN.md) |
| Dated capability checks | [MVP operational UI](history/MVP_DASHBOARD_OPERATIONAL_UI_REPORT.md), [retention verification](history/MVP_RETENTION_MONITORING_VALIDATION.md), [Phase 10 audit](history/PHASE10_INDEPENDENT_AUDIT_REPORT.md) |

A full machine-level file inventory includes many additional `*_SCOPE.md`, `*_CORRECTION_*.md` and `*_REAUDIT_*.md` reports. Those are source records, not reader entry points. The preferred reading order above eliminates the need to treat every report as a separate “current state” document. Their links and evidence remain preserved.

## Do not silently collapse these evidence families

1. **Independent audit findings and revalidation results:** keep commit identity, sequence, severity, reproducer and reviewer provenance.
2. **Security/approval decisions:** keep the exact owner authorization, declined alternatives and any hold. For example, [migration 0057](FK_INDEX_ROLLOUT_0057.md) still requires representative staging qualification **and** written owner and DBA approval before any production execution.
3. **Contracts and ADRs:** [architecture records](architecture/), [implementation contracts](implementation/), [security guidance](security/) and [benchmark runs](benchmark-results/0057/README.md) document different decisions or evidence from these archived reports.
4. **Tool/vendor documentation:** `.claude/skills/archify/` contains separate third-party assets, legal notices and renderer references and is not part of the DCIM historical cleanup.

## Consolidation decision log

- **Consolidated now:** navigation, reader-oriented phase summary and release-oriented entry points. Repeated/stale status paragraphs were removed from the root README in favor of a canonical link.
- **Retained:** original dated implementation and audit reports; specific migration benchmark results; architecture/security contracts; reviewer and agent authority. Their unique evidence is not “duplicate text” just because subjects overlap.
- **Relocated:** root architecture, phase and review reports into `docs/history/`; repository references are reconciled through the mapping above. Git commit permalinks remain unchanged. Old external branch-relative URLs cannot be redirected by GitHub; use the mapping or the original commit permalink. No historical report is deleted.
