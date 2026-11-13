# DCIM release, staging and deployment — consolidated guide

**Reader-oriented consolidation, 2026-11-13.** This is the **entry point** for release controls and staging operations; it does not replace the specialized scripts, original governance documents, or an environment-specific approval record. A passing CI workflow does **not** authorize deployment.

## Boundaries first

| State | Meaning | Evidence to inspect |
| --- | --- | --- |
| Implemented / merged | Code appears in a repository commit on the relevant branch | Actual source, merged PR, commit SHA |
| CI validated | Named checks ran successfully on the **exact** proposed commit or required release provenance | GitHub Actions run attempt and checks |
| Independently accepted | A non-author reviewed the exact head with no blocking findings | PR review, verdict, unresolved threads |
| Staging qualified | Real, representative staging environment was deployed and tested with documented outcomes | Operator record, health/rollback and migration validation |
| Production authorized | **Separate** authorized deployment change, safety approvals and operational ownership exist | Signed-off owner/operations/DBA decisions; **not** inferred from this document |

**Current safety posture:** This documentation PR is not staging or production authorization. Historical [deployment implementation](DEPLOYMENT_IMPLEMENTATION.md) explicitly disables the production workflow path; [governance](DEPLOYMENT_GOVERNANCE.md) documents approval/audit gaps at its own dated baseline. Re-check the present workflows, protection rules and environment before relying on either as live configuration.

## Release and staging decision path

1. **Identify the exact release candidate.** Record `main` commit SHA, relevant issues/PRs, migration inventory, codeowners/reviewers and source changes. Do not use a branch name alone as immutable release identity.
2. **Validate source and review gates.** Inspect live required CI, failed/skipped/pending checks, security review exceptions, merge protection, exact-head independent review and unresolved threads. A quota-based scan failure is not a passing scan.
3. **Establish environment prerequisites.** Verify isolated credentials, authorization, database/Redis availability, backups, encryption keys, scanning dependencies, network trust and recovery access as applicable. Start with [operations](OPERATIONS.md) and [development/test bootstrap](DEVELOPMENT_AND_TESTING.md), not historical README commands.
4. **Validate the staging release path.** See [deployment implementation](DEPLOYMENT_IMPLEMENTATION.md) for the SHA verification mechanism, manual workflow and staging-only script; [staging VM plan](STAGING_VM_DEPLOYMENT_PLAN.md) for prerequisites; and the [VM runbook](STAGING_VM_RUNBOOK.md) for a proposed procedure. The VM runbook is explicitly a draft requiring owner authorization; do not treat it as an already-executed deployment.
5. **Collect direct qualification evidence.** Record successful configuration validation **and**, where applicable, a real staging startup, database migration/rollback rehearsal, dependency readiness, representative authentication/data flows, Edge/telemetry checks, backup/restore and rollback, and any untested failures. Passing `docker compose config --quiet` alone is not an application startup test.
6. **Obtain separate approvals for any future production action.** Confirm branch/deployment governance, authorized release identity, operator/on-call ownership, change-control/audit recording and all migration-specific DBA sign-offs. No instruction in this guide removes a hold or enables the currently disabled production path.

## Critical exception — migration 0057

**Do not execute migration 0057 on production** based on a unit test, synthetic CI database or this summary.

Read in order: [preflight](FK_INDEX_PREFLIGHT_0057.md) → [benchmark plan](FK_INDEX_BENCHMARK_0057.md) → [recorded benchmark results](benchmark-results/0057/README.md) → [rollout/stop conditions](FK_INDEX_ROLLOUT_0057.md). Confirm a **representative staging qualification**, operational readiness and **separate written owner and DBA approval** before any production execution. The standing restriction is tracked in [Issue #214](https://github.com/AhmedMahmoud2222/DCIM/issues/214). Older tests or benchmark outputs do not waive it.

## Where detailed instructions live

| Need | Source of detail | Do not infer |
| --- | --- | --- |
| Backups, recoverability, credentials, health, incidents | [Operations](OPERATIONS.md) | A tested runbook alone establishes production readiness |
| Release SHA proof and staging runner | [Deployment implementation](DEPLOYMENT_IMPLEMENTATION.md) | Staging configuration check is a production deploy |
| Branch protection, approval/audit requirements | [Deployment governance](DEPLOYMENT_GOVERNANCE.md) | Dated described settings are necessarily still current |
| Staging host preparation | [Staging VM plan](STAGING_VM_DEPLOYMENT_PLAN.md), [draft runbook](STAGING_VM_RUNBOOK.md) | Commands have been executed or authorized |
| Edge durable queue encryption | [SEC-06 LUKS runbook](SEC06_EDGE_STORAGE_ENCRYPTION.md) | Application-level SQLite encryption was approved; it was not |
| Catalog PDF safety | [Catalog documents](CATALOG_DOCUMENTS.md) | Scanner availability can be bypassed in production |
| Independent security acceptance | [Security acceptance gate](security/ACCEPTANCE_GATE.md) | CI alone establishes adversarial acceptance |

## Minimal release evidence record

Record at least: proposed SHA, target environment, operator and approving authority, exact-head CI run IDs and conclusion, independent review, snapshot/backup IDs, schema revisions (including any holds), time of execution, health/telemetry checks, rollback outcome and unresolved risks. Preserve this evidence outside ephemeral chat or CI logs. Do **not** record secrets, tokens or resolved credential values.

Related: [Documentation index](DOCUMENTATION_INDEX.md) · [Historical evidence guide](HISTORY_AND_EVIDENCE.md) · [Project status](PROJECT_STATUS.md).
