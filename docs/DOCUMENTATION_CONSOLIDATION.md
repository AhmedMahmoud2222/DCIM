# DCIM documentation consolidation report

**Date:** 2026-11-13  
**Baseline:** `main@b6947782571d07c84c3fbd899ef5f28f484cdb04`  
**Branch:** `docs/history-link-integrity-20261113`  
**Reviewer:** OpenCode (implementation owner, replacing Codex); independent review by Claude Code  
**Production deployment:** NOT authorized. Issue #214 — migration 0057 remains on production HOLD.

## Summary

This consolidation reorganizes the repository's dated historical Markdown evidence into `docs/history/`, reconciles every affected reference, and adds machine-readable documentation validation. No historical report is deleted; distinct documents are retained even where subjects overlap.

Before: 167 tracked Markdown documents, all at their original locations.  
After: 170 tracked Markdown documents (55 historical reports relocated to `docs/history/` plus 3 new consolidation infrastructure Markdown files: `docs/history/README.md`, `docs/DOCUMENTATION_CONSOLIDATION.md` and `docs/DOCUMENTATION_VALIDATION.md`).  
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
| Historical phase / architecture / review reports | 55 (at root) | 55 (relocated to `docs/history/`) | **Relocated** |
| New consolidation infrastructure Markdown | 0 | 3 | `docs/history/README.md`, `docs/DOCUMENTATION_CONSOLIDATION.md`, `docs/DOCUMENTATION_VALIDATION.md` |
| **Total tracked Markdown** | **167** | **170** | |

The net increase of three is solely the new consolidation infrastructure Markdown files; no historical report was deleted or added. `docs/documentation-moves.json` is the fourth infrastructure file (JSON, not Markdown) and is excluded from the Markdown counts.

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

**Link-only adjustments (3 files):** all 55 relocated reports are otherwise byte-identical, except `ARCHITECTURE_REVIEW.md` (line 1642), `PHASE10_INDEPENDENT_AUDIT_REPORT.md` (line 207) and `PHASE10_INTEGRATION_REPORT.md` (line 507), whose `docs/X.md` references to other root-level docs were rewritten to `../X.md` (or `../../README.md`) so they resolve from `docs/history/`. No findings, evidence or conclusions were altered.

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
- Validator regression tests: `python -m unittest discover -s tests/ci -p test_check_docs.py -v` — 11 passing (includes `test_path_report_loads_committed_mapping`, which loads the committed `docs/documentation-moves.json` and asserts `ARCHITECTURE_REVIEW.md` reports as relocated).
- Path/literal-reference inventory: `python scripts/check_docs.py --path-report` — 139 REVIEW findings at the final HEAD, none are errors: 72 intentional test fixtures under `tests/`; 67 genuine citations (14 relocated-report citations correctly labeled `relocated literal path` with their `docs/history/` replacement; 53 references to non-tracked/link-text files, kept in the review category).

### Reference reconciliation

All Markdown, source-code, test, script and workflow references to the 55 relocated reports were repointed to their `docs/history/` paths:

- **80 files** had code-comment citations repointed (source, tests, scripts, config, workflows).
- Reproducible diff metric: `git diff b6947782571d07c84c3fbd899ef5f28f484cdb04..HEAD --unified=0 | grep '^+' | grep -c 'docs/history/'` → **256 added lines** containing `docs/history/` (**273 total occurrences**; this includes the 55 rename path lines and the new consolidation docs). The consolidation scan counted **198 citation insertions** across the 80 pre-existing source files.
- `docs/documentation-moves.json` is the machine-readable old-to-new mapping (55 entries, nested under `"mapping"`; read by `path_references()`).

## 7. Limitations

- Similar titles alone were not used as a deletion criterion; overlap in subject matter is not treated as duplication.
- This consolidation does not re-certify any historical finding, re-baseline any dated SHA, or assert production readiness.
- External chat statements are not equivalent to a submitted GitHub PR review; independent review is requested on the exact PR HEAD.
