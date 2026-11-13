# DCIM history, architecture and assurance — reading guide

**Consolidated guide (2026-11-13)** to the dated root-level architecture, phase, implementation and independent-review reports. This page is an **interpretive map**, not a replacement review, re-certification or claim that old findings remain open/closed on today's code.

The original reports are deliberately **retained at their existing paths** in this change: they carry distinct commit baselines, test evidence, reviewer provenance and correction chronology. No byte-identical Markdown blobs were found on `main@1e00e227039a4fb08c09ae46f565430678f6d55d`. Similar titles alone are not a safe basis for deletion.

## The short project story

| Historical stream | What changed at that stage | Begin with | Then read |
| --- | --- | --- | --- |
| Architecture (Phase 0) | Domain identity, referential integrity, placement, integration boundaries and review-driven revisions | [Architecture specification](../ARCHITECTURE_REVIEW.md) | [Change matrix](../ARCHITECTURE_CHANGE_MATRIX.md), [final architecture validation](../FINAL_ARCHITECTURE_VALIDATION_REPORT.md) |
| Phase 1 — foundation | Identity, auth, audit/retention, outbox and schema; later audit-immutability and idempotency concurrency fixes | [Implementation](../PHASE1_IMPLEMENTATION.md) | [Correction](../PHASE1_CORRECTION_REPORT.md), [final hostile validation](../PHASE1_FINAL_RED_TEAM_VALIDATION_REPORT.md) |
| Phase 2 — physical/spatial | Rack/equipment and temporal placement, floor plans and import validation; nested-SVG hardening | [Implementation](../PHASE2_IMPLEMENTATION_REPORT.md) | [Correction](../PHASE2_CORRECTION_REPORT.md), [SVG correction](../PHASE2_NESTED_SVG_CORRECTION_REPORT.md), [independent revalidation #2](../PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_2_REPORT.md) |
| Phase 3 — power/capacity | Power graph, PDU/UPS/generator/panel objects, capacity and operational dashboard; graph/transaction corrections | [Implementation](../PHASE3_IMPLEMENTATION_REPORT.md) | [Correction implementation](../PHASE3_CORRECTION_IMPLEMENTATION.md), [final independent re-audit](../PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md) |
| Phase 8 — integration/collectors | Driver framework, collectors, profile/mapping boundaries and telemetry intake | [Implementation](../PHASE8_IMPLEMENTATION_REPORT.md) | [Edge contract](../PHASE8_EDGE_COLLECTOR_CONTRACT.md), [blocking corrections](../PHASE8_BLOCKING_CORRECTION_REPORT.md), [independent closure](../PHASE8_FINAL_CLOSURE_VALIDATION.md) |
| Phase 10 — catalog/instantiation/live impact | Catalog designer, physical instantiation, live overlays, test and integration corrections | [Integration report](../PHASE10_INTEGRATION_REPORT.md) | [Original independent audit](../PHASE10_INDEPENDENT_AUDIT_REPORT.md), [subsequent status addendum](AUDIT_STATUS.md) |
| Phase 11 — Edge security | Security findings, remediation work packages and owner decision gates | [Security assessment](../PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md) | [Remediation plan](../PHASE11_SECURITY_REMEDIATION_PLAN.md), [security acceptance](security/ACCEPTANCE_GATE.md) |

The chronological labels reflect the historical delivery/reporting sequence; they are **not** a substitute for current R1–R5 roadmap stages or an exact-head release status. Current-ish guides still carry their own dated baselines: [project status](PROJECT_STATUS.md), [architecture overview](ARCHITECTURE_OVERVIEW.md), and the actual source on `main` should be checked.

## How the overlapping records relate

- **Architecture specification vs revision/red-team/change-matrix documents:** the specification describes design; [v1.0–1.1 revision](../ARCHITECTURE_REVISION_REPORT.md), [v1.1 red-team](../ARCHITECTURE_RED_TEAM_REPORT.md), [targeted revision](../ARCHITECTURE_TARGETED_REVISION_REPORT.md), and [change matrix](../ARCHITECTURE_CHANGE_MATRIX.md) document *why and how* it changed. Treating these as identical duplicates would erase findings/dispositions.
- **Phase 1 implementation vs implementation report:** [implementation](../PHASE1_IMPLEMENTATION.md) and [implementation report](../PHASE1_IMPLEMENTATION_REPORT.md) overlap in scope. Both explicitly reference the later [independent red-team](../PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md) which contradicted original audit/idempotency guarantees; follow through to the correction and final validation before citing those guarantees.
- **Phase 2 independent reports:** [initial review](../PHASE2_INDEPENDENT_RED_TEAM_REPORT.md), [first revalidation](../PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_REPORT.md), and [second revalidation](../PHASE2_INDEPENDENT_RED_TEAM_REVALIDATION_2_REPORT.md) target different correction baselines, including the later deeply nested SVG issue. They are a review trail, not redundant approvals.
- **Phase 3 review sequence:** [initial correction design](../PHASE3_CORRECTION_DESIGN.md), [hostile self-audit](../PHASE3_HOSTILE_SELF_AUDIT.md), [independent validation](../PHASE3_INDEPENDENT_VALIDATION_REPORT.md), and [final re-audit](../PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md) have different authorship, scope and verdict boundaries.
- **Phase 10 audit vs integration:** the original independent audit and the later remediation/integration report refer to **different historical source SHAs**. Read [AUDIT_STATUS.md](AUDIT_STATUS.md) and [PROJECT_STATUS.md](PROJECT_STATUS.md) to interpret them. For example, an old statement that PR #29 was unmerged was true at its baseline; it is **not** the status on 2026-11-13.
- **Security assessment vs remediation plan:** findings, proposed work and actual implemented controls are different states. Read the issue/PR and exact-head validation before marking a security control closed.

## Evidence inventory by function

| Evidence type | Example and intent |
| --- | --- |
| Baselines and scope definition | [Phase 1 baseline](../PHASE1_BASELINE.md), [Phase 2 gap analysis](../PHASE2_GAP_ANALYSIS.md), [Phase 3 gap analysis](../PHASE3_GAP_ANALYSIS.md), [Phase 8 gap analysis](../PHASE8_GAP_ANALYSIS.md) |
| Deviations and traceability | [Phase 1 deviations](../PHASE1_DEVIATIONS.md), [Phase 1 traceability](../PHASE1_TRACEABILITY_MATRIX.md), [Phase 2 traceability](../PHASE2_TRACEABILITY_MATRIX.md), [Phase 3 traceability](../PHASE3_TRACEABILITY_MATRIX.md), [Phase 8 traceability](../PHASE8_TRACEABILITY_MATRIX.md) |
| Proposed actions / design | [Phase 3 correction-design scope](../PHASE3_CORRECTION_DESIGN_SCOPE.md), [Phase 3 correction design](../PHASE3_CORRECTION_DESIGN.md), [Phase 8 architecture clarification](../PHASE8_ARCHITECTURE_CLARIFICATION.md) |
| Independent review and follow-up | [Phase 1 implementation red-team](../PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md), [Phase 2 independent review](../PHASE2_INDEPENDENT_RED_TEAM_REPORT.md), [Phase 3 final closure](../PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md), [Phase 8 final closure](../PHASE8_FINAL_CLOSURE_VALIDATION.md) |
| Integration and release sequencing | [Branch reconciliation](../BRANCH_RECONCILIATION_REPORT.md), [pre-MVP consolidation](../PRE_MVP_CONSOLIDATION_REPORT.md), [MVP v0.1 plan](../DCIM_MVP_V0_1_PLAN.md) |
| Dated capability checks | [MVP operational UI](../MVP_DASHBOARD_OPERATIONAL_UI_REPORT.md), [retention verification](../MVP_RETENTION_MONITORING_VALIDATION.md), [Phase 10 audit](../PHASE10_INDEPENDENT_AUDIT_REPORT.md) |

A full machine-level file inventory includes many additional `*_SCOPE.md`, `*_CORRECTION_*.md` and `*_REAUDIT_*.md` reports. Those are source records, not reader entry points. The preferred reading order above eliminates the need to treat every report as a separate “current state” document. Their links and evidence remain preserved.

## Do not silently collapse these evidence families

1. **Independent audit findings and revalidation results:** keep commit identity, sequence, severity, reproducer and reviewer provenance.
2. **Security/approval decisions:** keep the exact owner authorization, declined alternatives and any hold. For example, [migration 0057](FK_INDEX_ROLLOUT_0057.md) still requires representative staging qualification **and** written owner and DBA approval before any production execution.
3. **Contracts and ADRs:** [architecture records](architecture/), [implementation contracts](implementation/), [security guidance](security/) and [benchmark runs](benchmark-results/0057/README.md) document different decisions or evidence from these root reports.
4. **Tool/vendor documentation:** `.claude/skills/archify/` contains separate third-party assets, legal notices and renderer references and is not part of the DCIM historical cleanup.

## Consolidation decision log

- **Consolidated now:** navigation, reader-oriented phase summary and release-oriented entry points. Repeated/stale status paragraphs were removed from the root README in favor of a canonical link.
- **Retained:** original dated implementation and audit reports; specific migration benchmark results; architecture/security contracts; reviewer and agent authority. Their unique evidence is not “duplicate text” just because subjects overlap.
- **Not undertaken here:** mass-moving root reports into `docs/history/` or deleting them by title similarity. Either requires a cross-repository inbound/outbound link inventory (including source-code references), semantic comparison and independently reviewed reference updates. [Issue #195](https://github.com/AhmedMahmoud2222/DCIM/issues/195) records a relocation proposal; this document does not imply that the entire proposal has been adopted.
