# Documentation index

**Purpose:** navigate the *current* DCIM implementation without confusing historical design documents with the source now present in the repository. Updated 2026-09-26 for the PR #29 integration snapshot.

## Start here

| Reader / question | Document | What it covers |
|---|---|---|
| New contributor or project stakeholder | [README](../README.md) | What the platform currently implements, status, technology, quick-start and key limitations |
| Product owner / release reviewer | [Project status](PROJECT_STATUS.md) | Phase-by-phase source ledger, PR links, current CI evidence and remaining decisions |
| Architect / engineer | [Architecture overview](ARCHITECTURE_OVERVIEW.md) | Current modular monolith, core domain boundaries, telemetry models and API/UI entry points |
| Developer / CI maintainer | [Development and testing](DEVELOPMENT_AND_TESTING.md) | PostgreSQL/Redis bootstrap, frontend/Edge/Playwright suites, runtime matrix and migrations |
| Operations / deployment team | [Operations](OPERATIONS.md) | Security, configuration, backup/restore, monitoring, health checks, rollback rehearsals and incident entry points |
| Independent reviewer / auditor | [Audit status](AUDIT_STATUS.md) | Distinguishes original Phase 10 findings from PR #29 fixes, verified CI and pending review evidence |
| Administrator / security reviewer | [User & group management](USER_GROUP_MANAGEMENT.md) | Users, groups, allow/deny permissions, site and rack access, effective-permission rules and safeguards |
| Catalog administrator / security reviewer | [Catalog datasheet storage](CATALOG_DOCUMENTS.md) | Secure PDF upload, scanning, versioning and download of manufacturer datasheets |
| Catalog administrator / security reviewer | [Catalog datasheet extraction](CATALOG_DATASHEET_EXTRACTION.md) | Sandboxed native and OCR extraction, candidate values with provenance, multi-model handling, job semantics and review |

## Product roadmap

- [Consolidated product roadmap](PRODUCT_ROADMAP.md) — original scope, Phase 10/11 additions and proposed R1–R5 releases; distinguish planned from implemented and deployed.

## Historical architecture and phase records

The documents below are useful **historical evidence**. Their dates and baseline SHAs matter: a sentence saying a later phase "does not exist" may have been true when it was written and false for today's code. Do not silently rewrite those dated conclusions.

| Document | Intended use |
|---|---|
| [Architecture review](../ARCHITECTURE_REVIEW.md) | Canonical historical architecture §§1–50; Phase 10A addendum §4d and Phase 10B/C addendum §51; later implementation status is recorded here in the current guide |
| [Phase 1 implementation](../PHASE1_IMPLEMENTATION.md) | Foundation implementation and historical scope |
| [Phase 1 correction report](../PHASE1_CORRECTION_REPORT.md) | Corrective work on database ownership, audit retention, security/concurrency |
| [Phase 8 architecture clarification](../PHASE8_ARCHITECTURE_CLARIFICATION.md) | Framework versus persistent vendor/device/profile/metric layers and associated historical decisions |
| [Phase 10 independent audit report](../PHASE10_INDEPENDENT_AUDIT_REPORT.md) | Jules' original audit of the `d96676c` baseline — read alongside [audit status](AUDIT_STATUS.md) for the subsequent corrections |
| [Phase 10 integration report](../PHASE10_INTEGRATION_REPORT.md) | PR #27/#28 reconciliation, Phase 10 ordering and BER fixes, CI evidence and merge gate |

Other original architecture review and phase implementation artifacts are preserved at the repository root; the current index does not assert that all of them were revalidated on the newest branch.

## Source and issue links

- [Registered FastAPI routers](../backend/app/api/v1/router.py) and [React routes](../frontend/src/app/App.tsx).
- [Current CI workflow](../.github/workflows/ci.yml), [Phase 10 integration PR #29](https://github.com/AhmedMahmoud2222/DCIM/pull/29) and [verified reviewed-head CI run #80](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36141826654).
- [Original independent audit issue #24](https://github.com/AhmedMahmoud2222/DCIM/issues/24), [CI/docs issue #25](https://github.com/AhmedMahmoud2222/DCIM/issues/25), [browser/runtime issue #26](https://github.com/AhmedMahmoud2222/DCIM/issues/26).
- Implementation source PRs: [10A backend #16](https://github.com/AhmedMahmoud2222/DCIM/pull/16), [10A UI #20](https://github.com/AhmedMahmoud2222/DCIM/pull/20), [10A graphics #21](https://github.com/AhmedMahmoud2222/DCIM/pull/21), [10B #22](https://github.com/AhmedMahmoud2222/DCIM/pull/22), [10C #23](https://github.com/AhmedMahmoud2222/DCIM/pull/23).

## Documentation maintenance rules

1. **Label the baseline:** include the date, exact GitHub SHA and which branch or deployed release is being described.
2. **Separate implementation from assurance:** code present, merged to `main`, tested on CI, independently audited, deployed and production-validated are separate states.
3. **Update this index and the README** when adding a new phase/module, public API, CLI command or operational requirement.
4. **Write dated addenda** rather than silently changing historic audit evidence. If a historical audit missed a defect, retain the original baseline and record the later finding with the introducing and fixing commits.
5. **Verify commands** against the workflow, Compose services and scripts at the same commit; do not invent paths, service names, test counts or future roadmap completion dates.
6. After PR #29 merges, **change the current-status pointers to the new main merge SHA and post-merge CI run**. Until then, the README and this index document the PR integration branch, not the default branch's released state.
