# CI Architecture ADR — Risk-Based GitHub Actions CI

**Status:** Proposed (implemented in branch `ci/risk-based-ci-optimization`, Draft PR)
**Repository:** `AhmedMahmoud2222/DCIM`
**Priority:** P1
**Owner:** Implementation agent (architecture supervisor: GPT-6; independent review required before merge)

## 1. Context and problem statement

The single `ci.yml` workflow ran every job (`backend`, `frontend`, `edge-collector`,
`edge-interop`, `browser-e2e`, and the `backend-runtime-matrix` Python 3.12/3.13/3.14
matrix) on every pull request, regardless of risk. Measured baseline: a full CI run
took **~48 minutes** (the three Python matrix jobs each ran ~46–48 minutes and drove the
total; the required `backend` check alone ran ~42 minutes; `frontend` ~2 minutes).

That is wasteful for the majority of PRs (documentation, frontend tweaks, isolated
backend changes) that do not need the full Python matrix, Playwright E2E, Docker Compose
or deployment-tooling regression suite. At the same time, the required status checks
`backend` and `frontend` must be preserved exactly and must always validate meaningfully.

## 2. Decision

Adopt a **three-level risk-based CI** driven by a deterministic, static change-detection
router (`.github/scripts/ci_router.py` + `.github/scripts/ci_policy.py`):

| Level | When it runs | What it validates |
|-------|--------------|-------------------|
| **Fast CI** | Every PR | Change detection + risk classification; linting (ruff); security baseline (confidential preflight-report guard); migration integrity (Alembic single-head). |
| **Component CI** | Affected components only | Backend, Frontend, OpenCode, Edge Collector, Database/shared contracts — only the components the change touches. |
| **Full CI** | High-risk changes (or detection failure) | Complete Python compatibility matrix (3.12/3.13/3.14); PostgreSQL/Redis integration; Docker Compose; Playwright E2E; deployment tooling regressions; relevant security validation. |

Full CI is mandatory for **security, authentication, migrations, infrastructure,
workflow configuration and shared dependencies**. Any unknown path or
change-detection failure escalates to Full CI (fail-closed).

### Safeguards (all implemented and tested)

1. **Preserved required checks.** The check names `backend` and `frontend` are unchanged,
   always run, and always validate meaningfully. `backend` consumes Fast CI's routing
   decision but uses `if: always()` so it can never be skipped as a side effect of Fast
   CI. `frontend` has no dependencies and runs independently.
2. **No false-green aggregation.** A job that failed, was cancelled, skipped
   unexpectedly or never completed is never reported as success. Component/Full CI jobs
   gate on `needs.fast-ci.result == 'success'`; Fast CI itself never suppresses a real
   failure.
3. **Escalate unknowns to Full CI.** Unknown file paths and change-detection failures
   route to Full CI (the router never returns Fast CI for an unknown path or a failed
   detection).
4. **No stale Full CI approvals.** `concurrency.cancel-in-progress: true` cancels
   in-progress runs for a branch when a new commit lands, so a stale Full CI result from
   an earlier commit cannot be presented as the current check status. All jobs re-run on
   every `pull_request` event.
5. **Transitive dependencies validated.** Full CI installs the real dependency set
   (`pip install -e ".[dev]"`, `pip install "./edge_collector[test]"`, `npm ci`) per
   Python version and runs the dependent Edge Collector tests against it.
6. **Signed commits.** All commits on the delivery branch are GPG/SSH signed
   (`gpg.format = ssh`, `user.signingkey = id_ed25519_signing`); verified with
   `git verify-commit`.
7. **No control modifications.** Branch protection, rulesets, production secrets, and
   deployment workflows (`deploy.yml`, `deployment-validation.yml`, `deploy-tools-test.yml`)
   are not modified.
8. **No merge, deploy, or bypass.** Work is delivered in a Draft PR only; no merge or
   deployment is performed.

## 3. Routing policy (`.github/scripts/ci_policy.py`)

A static, ordered path-rule table classifies each changed file as Fast / Component /
Full, with Full taking precedence. First match wins, so Full-risk rules (workflows,
migrations, auth, security, infrastructure, shared dependencies, the router/policy
themselves) are listed before the broader component rules they contain.

* Full-risk: `.github/workflows/`, `.github/scripts/ci_router.py`, `.github/scripts/ci_policy.py`,
  `backend/migrations/`, `backend/app/auth/`, `backend/app/security/`, `docker-compose*.yml`,
  `backend/Dockerfile`, `frontend/Dockerfile`, `scripts/deploy-docker-compose.sh`,
  `tests/deployment/`, `backend/pyproject.toml`, `frontend/package.json`,
  `frontend/package-lock.json`.
* Component-risk: `backend/app/models/`, `backend/app/core/`, `backend/app/infrastructure/`
  → database-contracts; `backend/` → backend; `frontend/` → frontend;
  `edge_collector/` → edge-collector; `.github/opencode/`, `.github/scripts/opencode_*`
  → opencode.
* Fast-risk: `README.md`, `docs/`, `*.md`.
* Unknown path → Full CI (fail-closed).

## 4. Required-check depth

`backend` runs two depths selected by Fast CI's `risk-level` output:
* **Fast** (low-risk): Alembic single-head gate + ruff lint + backend import smoke test.
  Completes in minutes.
* **Full** (high-risk): the complete preserved suite (OCR engine, clamd, Compose settings,
  least-privilege role provisioning, Postgres extensions, migrations, mypy, CAP_NET_RAW,
  retention hostile validation, extraction apply, full regression suite).

`frontend` always runs its full suite (it is already ~2 minutes) and has no dependency on
Fast CI, so it can never be skipped.

## 5. Concurrency and caching

* `concurrency.cancel-in-progress: true` per branch (safe concurrency cancellation;
  prevents stale runs).
* `actions/cache` for pip (`~/.cache/pip`, keyed on `backend/pyproject.toml`) and npm
  (`~/.npm`, keyed on `frontend/package-lock.json`), restoring across runs to reduce
  redundant setup.
* All `uses:` references pinned to full 40-character commit SHAs (enforced by
  `tests/deployment/check_workflows.py`).

## 6. Consequences

### Positive
* Low-risk PRs (the majority) skip the ~46–48 minute Python matrix, Playwright E2E,
  Docker Compose and deployment-tooling suite, and run a fast `backend` path.
* Measured baseline full run: ~48 minutes. Projected low-risk PR: under ~10 minutes
  (Fast CI + fast `backend` + `frontend`), subject to measurement on GitHub Actions.
* Required checks `backend`/`frontend` preserved exactly; no false-green aggregation.

### Negative / risks
* High-risk PRs take approximately the same as baseline (full validation is required by
  design) — this is the intended trade-off.
* `backend` fast path is DB-free and bounded; it does not replace the full PostgreSQL
  integration suite, which runs on every high-risk change.
* The router is static and path-based; a change that only alters behavior without touching
  classified paths routes by its touched files only.

## 7. Related documents

* `docs/ci/BASELINE_RUNTIME_REPORT.md` — measured baseline runtimes and bottlenecks.
* `docs/ci/ROUTING_MATRIX.md` — dependency-aware path → level → component matrix.
* `docs/ci/BEFORE_AFTER_BENCHMARK.md` — before/after benchmark.
* `docs/ci/ROLLBACK_INSTRUCTIONS.md` — how to roll back to the previous `ci.yml`.
* `tests/ci/` — fail-closed regression tests (routing scenarios, false-green negatives).
