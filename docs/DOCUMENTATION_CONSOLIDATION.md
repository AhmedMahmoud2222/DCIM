# DCIM documentation consolidation report

**Date:** 2026-11-13  
**Baseline:** `main@b6947782571d07c84c3fbd899ef5f28f484cdb04`  
**Branch:** `docs/history-link-integrity-20261113`  
**Reviewer:** OpenCode (implementation owner, replacing Codex); independent review by Claude Code  
**Production deployment:** NOT authorized. Issue #214 — migration 0057 remains on production HOLD.

## Summary

This consolidation reorganizes the repository's dated historical Markdown evidence into `docs/history/`, reconciles every affected reference, and adds machine-readable documentation validation. No historical report is deleted; distinct documents are retained even where subjects overlap.

Before: 167 tracked Markdown documents, all at their original locations.  
After: 167 tracked Markdown documents (count unchanged — relocation only).  
Historical documents relocated into `docs/history/`: **55**.  
Documents deleted: **0**.  
Exact/normalized whole-file duplicates: **0** (verified by Codex baseline; re-confirmed below).

## 1. Before/after Markdown inventory

| Category | Before count | After count | Disposition |
|---|---:|---:|---|
| Canonical / living guides | 12 | 12 | Retained in place |
| Implementation contracts | 11 | 11 | Retained in place |
| Architecture decisions (ADRs) | 5 | 5 | Retained in place (`docs/architecture/`) |
| Security evidence & agent authority | 9 | 9 | Retained in place (`docs/security/`) |
| Third-party / bundled (archify) | 24 | 24 | Retained; excluded from validation |
| CI / operations / other current docs | 41 | 41 | Retained in place |
| Historical phase / architecture / review reports | 55 | 55 | **Relocated to `docs/history/`** |
| **Total tracked Markdown** | **167** | **167** | |

The document count is unchanged because this is a relocation, not a deletion or addition of report content. Four new infrastructure files were added (`docs/history/README.md`, `docs/documentation-moves.json`, `docs/DOCUMENTATION_CONSOLIDATION.md`, `docs/DOCUMENTATION_VALIDATION.md`) — these are the consolidation's index, mapping, report and validation evidence.

## 2. Classification

### Canonical / living guides (retained in place)
Canonical navigation and guidance: `docs/DOCUMENTATION_INDEX.md`, `docs/HISTORY_AND_EVIDENCE.md`, `docs/ARCHITECTURE_OVERVIEW.md`, `docs/AUDIT_STATUS.md`, `docs/OPERATIONS.md`, `docs/PROJECT_STATUS.md`, `docs/RELEASE_AND_DEPLOYMENT.md`, `docs/DEVELOPMENT_AND_TESTING.md`, `docs/PRODUCT_ROADMAP.md`, `docs/DEPLOYMENT_GOVERNANCE.md`, `docs/DEPLOYMENT_IMPLEMENTATION.md`, root `README.md`, root `AGENTS.md`.

### Implementation contracts (retained in place)
`docs/implementation/` contracts (telemetry mapping/replay/aggregate/semantics, atomic optimistic concurrency, catalog extraction apply, M1/M2, TELEMETRY_*), `docs/architecture/` ADRs and contracts (ADR-0012, ADR-0013, CFD_INPUT_CONTRACT_V1, CFD_SERVICE_SECURITY_ARCHITECTURE, CFD_VERIFICATION_VALIDATION_POLICY, ORCHESTRATION_CONTROLLER_RELIABILITY_SPEC, PHYSICAL_VALUE_INVENTORY, controller-reliability journal/scan budget).

### Architecture decisions (retained in place)
`docs/architecture/` ADRs remain the decision-of-record. The dated `ARCHITECTURE_REVIEW.md` and its companion revision/red-team/change-matrix reports were relocated to `docs/history/` as historical evidence; they are not the current ADR source.

### Security evidence & agent authority (retained in place)
`docs/security/` (ACCEPTANCE_GATE, AGENT_COLLABORATION, AUTONOMOUS_OPERATIONS, CLAUDE_AGENT_AUTHORITY/PROTECTIONS, DCIM01_REMEDIATION_VALIDATION_HANDOFF_v1.2, DCIM01_SECURITY_AUDIT_REPORT_v1.2, OPENCODE_AGENT_AUTHORITY, SEC-RBAC-59-02 verification), `docs/SEC05_COLLECTOR_RETENTION.md`, `docs/SEC06_EDGE_STORAGE_ENCRYPTION.md`. These are access-control and security-control documents and are NOT relocated or weakened.

### Third-party material (retained, excluded from validation)
`.claude/skills/archify/` (skill, renderer, schema, locale, brand-mark and THIRD_PARTY_NOTICES documents) — bundled tool documentation maintained upstream, excluded from validation via `EXCLUDED_PREFIXES`.

### Historical reports (relocated to `docs/history/`)
55 dated root-level reports (see `docs/history/README.md` and `docs/documentation-moves.json`). These include original findings, independent reviews, audit evidence, benchmarks (`docs/benchmark-results/0057/` was NOT relocated — see section 4), authorizations and commit SHAs.

## 3. Disposition: retain, consolidate, relocate, delete

| Action | Files | Justification |
|---|---|---|
| **Retain in place** | 112 documents | Canonical guides, contracts, ADRs, security evidence, third-party material; still referenced by source and current navigation. |
| **Relocate** | 55 historical reports | Dated phase/architecture/review evidence moved to `docs/history/`; references reconciled via `docs/documentation-moves.json`. |
| **Consolidate (navigation text)** | `README.md`, `docs/DOCUMENTATION_INDEX.md`, `docs/HISTORY_AND_EVIDENCE.md` | Repeated/stale status paragraphs removed from root README in favor of a canonical link to `docs/history/`. No evidence deleted. |
| **Delete** | 0 | No document deleted. Distinct documents are not removed merely because subjects overlap. |
| **Not relocated (active hold evidence)** | `docs/FK_INDEX_BENCHMARK_0057.md`, `docs/FK_INDEX_PREFLIGHT_0057.md`, `docs/FK_INDEX_ROLLOUT_0057.md`, `docs/benchmark-results/0057/` | Migration 0057 evidence tied to open Issue #214 production HOLD. Kept at current paths so the rollout contract and hold remain immediately visible; relocation would not lift the hold and is out of scope for this documentation pass. |

## 4. Items explicitly NOT changed by this consolidation

- **Migration 0057 remains on production HOLD** (Issue #214). This documentation pass does not run, approve, or lift any migration. Representative staging qualification and separate written owner **and** DBA approval are required before production execution.
- **No source-code, migration, CI-workflow, or branch-protection changes** beyond documentation reference repairs.
- **No security-control weakening.** Access-control decisions, acceptance gates, and agent authority documents are unchanged.
- `docs/benchmark-results/0057/` and `docs/FK_INDEX_*_0057.md` remain at their current paths (active hold evidence, section 3).

## 5. Original-to-new path mapping

Complete mapping: `docs/documentation-moves.json` (55 entries, generated from the `git mv` operations). Example entries:

- `ARCHITECTURE_REVIEW.md` → `docs/history/ARCHITECTURE_REVIEW.md`
- `PHASE1_IMPLEMENTATION.md` → `docs/history/PHASE1_IMPLEMENTATION.md`
- `PHASE10_INDEPENDENT_AUDIT_REPORT.md` → `docs/history/PHASE10_INDEPENDENT_AUDIT_REPORT.md`
- `PRE_MVP_CONSOLIDATION_REPORT.md` → `docs/history/PRE_MVP_CONSOLIDATION_REPORT.md`

Git commit permalinks remain unchanged. Original branch-relative URLs cannot be redirected by GitHub; use the mapping or the original commit permalink.

## 6. Verification

- Offline Markdown link validation: `python scripts/check_docs.py --json` — 0 errors (see `docs/DOCUMENTATION_VALIDATION.md`).
- Validator regression tests: `python -m unittest discover -s tests/ci -p test_check_docs.py -v` — 10 passing.
- Path/literal-reference inventory: `python scripts/check_docs.py --path-report` — reviewed; code-comment citations to relocated reports were updated to their new `docs/history/` paths.

## Limitations

- Similar titles alone were not used as a deletion criterion; overlap in subject matter is not treated as duplication.
- This consolidation does not re-certify any historical finding, re-baseline any dated SHA, or assert production readiness.
- External chat statements are not equivalent to a submitted GitHub PR review; independent review is requested on the exact PR HEAD.
