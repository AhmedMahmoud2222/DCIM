# DCIM documentation index

**Navigation edition:** 2026-11-13, based on the Markdown inventory of `main@1e00e227039a4fb08c09ae46f565430678f6d55d`.

**How to read this index:** “Primary guide” means the preferred entry point, **not** that every implementation or status claim has been revalidated at this commit. Many underlying pages give an earlier exact SHA and are snapshots. Check live source, GitHub PR/CI evidence and deployment records before making a current-state claim. No production deployment is authorized here.

## Start here — one entry point per question

| Reader or question | Primary guide | Supporting detail |
| --- | --- | --- |
| New contributor / stakeholder | [Repository README](../README.md) | [Project status](PROJECT_STATUS.md), [roadmap](PRODUCT_ROADMAP.md) |
| What exists, what is proposed and what is verified? | [Project status](PROJECT_STATUS.md) | [Architecture overview](ARCHITECTURE_OVERVIEW.md); GitHub history and current CI |
| What are the component and domain boundaries? | [Architecture overview](ARCHITECTURE_OVERVIEW.md) | [Historical architecture and decisions](HISTORY_AND_EVIDENCE.md); [architecture ADRs](architecture/) |
| How do I run and test locally? | [Development and testing](DEVELOPMENT_AND_TESTING.md) | [Edge collector](../edge_collector/README.md); [CI routing](ci/ROUTING_MATRIX.md) |
| How do I operate, investigate and recover? | [Operations](OPERATIONS.md) | [Staging VM runbook](STAGING_VM_RUNBOOK.md) |
| How do staging, releases and approvals work? | [Release and deployment guide](RELEASE_AND_DEPLOYMENT.md) | [Deployment implementation](DEPLOYMENT_IMPLEMENTATION.md), [governance](DEPLOYMENT_GOVERNANCE.md) |
| Where are original findings, corrections and audits? | [Historical evidence guide](HISTORY_AND_EVIDENCE.md) | [Phase 10 audit status](AUDIT_STATUS.md), [security acceptance gate](security/ACCEPTANCE_GATE.md) |
| What is the intended R1–R5 direction? | [Product roadmap](PRODUCT_ROADMAP.md) | Issue-specific architecture/contracts below |

## Feature and engineering documentation

- **Identity and permissions:** [User/group management](USER_GROUP_MANAGEMENT.md), [scoped authorization architecture](architecture/), [atomic optimistic concurrency](implementation/ATOMIC_OPTIMISTIC_CONCURRENCY.md).
- **Asset catalog and documents:** [PDF datasheet storage](CATALOG_DOCUMENTS.md), [datasheet extraction](CATALOG_DATASHEET_EXTRACTION.md), [decommission constraint](ASSET_DECOMMISSION_CONSTRAINT.md).
- **Monitoring and telemetry:** [Edge Collector](../edge_collector/README.md), [Edge interop](../edge_collector/interop/README.md), [telemetry metric semantics](implementation/TELEMETRY_METRIC_SEMANTICS_V2.md), [replay ledger](implementation/TELEMETRY_REPLAY_LEDGER.md), [mapping revisions](implementation/TELEMETRY_MAPPING_REVISIONS.md).
- **Network discovery:** [Issue #101 profile/discovery implementation](implementation/ISSUE_101_NETWORK_PROFILES_DISCOVERY.md), [supported profile contract](implementation/ISSUE_101_SUPPORTED_PROFILES.md).
- **Cooling, digital twin and CFD:** [Cooling/thermal implementation](implementation/ISSUE_105_COOLING_THERMAL.md), [CFD implementation scope](implementation/ISSUE_106_CFD_THERMAL_ANALYSIS.md), [solver ADR](architecture/ADR-0013-cfd-solver-selection.md), [CFD input contract](architecture/CFD_INPUT_CONTRACT_V1.md), [verification policy](architecture/CFD_VERIFICATION_VALIDATION_POLICY.md).
- **AI operations:** [Issue #107 implementation overview](implementation/ISSUE_107_AI_OPERATIONS.md), [data-quality/predictive design](implementation/ISSUE_107_DATA_QUALITY_AND_PREDICTIVE.md), [read-only assistant contract](implementation/ISSUE_107_M2_READONLY_ASSISTANT.md). **Design contracts must not be presented as deployed capabilities.**
- **Architecture and CI decisions:** [Physical-units ADR](architecture/ADR-0012-canonical-physical-units.md), [orchestration controller reliability](architecture/ORCHESTRATION_CONTROLLER_RELIABILITY_SPEC.md), [CI architecture ADR](ci/CI_ARCHITECTURE_ADR.md).

## Operations and security — authoritative detail

- [Release and deployment overview](RELEASE_AND_DEPLOYMENT.md) → [Operations](OPERATIONS.md) → [Staging plan](STAGING_VM_DEPLOYMENT_PLAN.md) / [VM runbook](STAGING_VM_RUNBOOK.md).
- **Migration 0057 — production hold:** [preflight](FK_INDEX_PREFLIGHT_0057.md), [benchmark](FK_INDEX_BENCHMARK_0057.md), [rollout](FK_INDEX_ROLLOUT_0057.md), [benchmarked results](benchmark-results/0057/README.md). No production execution without staging qualification and written owner and DBA approval.
- **Security and agents:** [Acceptance gate](security/ACCEPTANCE_GATE.md), [OpenCode authority](security/OPENCODE_AGENT_AUTHORITY.md), [Claude authority](security/CLAUDE_AGENT_AUTHORITY.md), [collaboration](security/AGENT_COLLABORATION.md), and [reviewer rules](../AGENTS.md). Do not merge or amend authority documents under a general docs-cleanup pretext.
- **Edge storage protection:** [SEC-06 LUKS/dm-crypt runbook](SEC06_EDGE_STORAGE_ENCRYPTION.md), [SEC-05 retention](SEC05_COLLECTOR_RETENTION.md).
- **Older security and validation evidence:** [Phase 11 findings and remediation plan](../PHASE11_SECURITY_REMEDIATION_PLAN.md), [Phase 10 audit addendum](AUDIT_STATUS.md), [dated independent audit records](HISTORY_AND_EVIDENCE.md).

## Historical evidence vs living guidance

The [historical evidence guide](HISTORY_AND_EVIDENCE.md) consolidates the *reading order* for original architecture/phase documents without rewriting their audit results. Their baseline SHAs, negative findings, revalidation and correction records are distinct evidence and should not be deleted merely because two titles address the same phase.

Files under `.claude/skills/archify/` are bundled skill, renderer and third-party notice documents, **not** duplicate DCIM technical specifications. Files in `docs/benchmark-results/` are individual benchmark records, not summaries to collapse into one untraceable result.

## Maintenance rules

1. Keep this index and the README short; put procedures and technical claims in their named canonical guide.
2. Specify branch, SHA, date and evidence when asserting a current implementation or review result. Do not copy a dated CI or PR status into multiple landing pages.
3. Separate **proposed**, **implemented**, **merged**, **independently accepted**, **staging-qualified**, and **production-authorized** states.
4. Preserve dated audits, findings, benchmark results, ADRs and security/agent authority verbatim. Add a dated correction or cross-reference rather than silently revising historical conclusions.
5. Validate relative links and affected operational commands when moving a file. Do not delete or relocate a linked report until its inbound and outbound references are reconciled.
6. Never interpret this documentation consolidation as an approval to deploy, run production migrations, weaken CI, or resolve active security findings.
