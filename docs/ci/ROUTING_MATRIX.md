# Dependency-Aware Routing Matrix

**Repository:** `AhmedMahmoud2222/DCIM`
**Generated from:** `.github/scripts/ci_policy.py` (policy v1.0) and
`.github/scripts/ci_router.py` (router v1.0)
**Enforced by:** `.github/workflows/ci.yml` (Fast CI → Component CI / Full CI)

## 1. Classification rules (first match wins)

Rules are evaluated in the order listed. Full-risk rules are placed before the broader
component rules they contain, so any Full-risk change wins regardless of component.
A path matching no rule escalates to Full CI (fail-closed: unknown paths never route to
Fast CI).

| # | Path pattern | Level | Component(s) | Rationale |
|---|--------------|-------|--------------|-----------|
| 1 | `.github/workflows/` | Full | — | Changing CI itself must re-validate everything |
| 2 | `.github/scripts/ci_policy.py` | Full | — | The classifier itself; changing it re-validates all |
| 3 | `.github/scripts/ci_router.py` | Full | — | The classifier itself; changing it re-validates all |
| 4 | `backend/migrations/` | Full | — | Schema changes run full PostgreSQL integration |
| 5 | `backend/app/auth/` | Full | — | Authentication surface |
| 6 | `backend/app/security/` | Full | — | Security surface |
| 7 | `docker-compose.yml` | Full | — | Infrastructure |
| 8 | `docker-compose.production.yml` | Full | — | Infrastructure |
| 9 | `docker-compose.override.yml` | Full | — | Infrastructure |
| 10 | `backend/Dockerfile` | Full | — | Infrastructure |
| 11 | `frontend/Dockerfile` | Full | — | Infrastructure |
| 12 | `scripts/deploy-docker-compose.sh` | Full | — | Deployment tooling |
| 13 | `tests/deployment/` | Full | — | Deployment tooling regressions |
| 14 | `backend/pyproject.toml` | Full | — | Shared (backend) dependency |
| 15 | `frontend/package.json` | Full | — | Shared (frontend) dependency |
| 16 | `frontend/package-lock.json` | Full | — | Shared (frontend) dependency |
| 17 | `backend/app/models/` | Component | database-contracts | Shared DB contracts |
| 18 | `backend/app/core/` | Component | database-contracts | Shared DB contracts |
| 19 | `backend/app/infrastructure/` | Component | database-contracts | Shared DB contracts |
| 20 | `backend/` | Component | backend | Backend component |
| 21 | `backend/tests/` | Component | backend | Backend tests |
| 22 | `frontend/` | Component | frontend | Frontend component |
| 23 | `edge_collector/` | Component | edge-collector | Edge Collector component |
| 24 | `.github/opencode/` | Component | opencode | OpenCode agent infra |
| 25 | `.github/scripts/opencode_*` | Component | opencode | OpenCode agent infra |
| 26 | `README.md` | Fast | — | Documentation only |
| 27 | `docs/` | Fast | — | Documentation only |
| 28 | `*.md` | Fast | — | Documentation only |
| — | *(anything else)* | Full | — | Unknown path: fail-closed escalation |

## 2. Level precedence

`Full > Component > Fast`. A change set's overall level is the maximum severity of its
changed files:

* Any Full-risk path → **Full CI** (matrix, Postgres/Redis, Docker Compose, Playwright E2E,
  deployment tooling, security).
* Else any Component-risk path → **Component CI** for the touched components.
* Else (Fast only, or no changes) → **Fast CI** (lint, security baseline, migration
  integrity).

## 3. Required checks (always preserved)

| Check | Always runs? | Depth |
|-------|--------------|-------|
| `backend` | Yes (`if: always()`) | Fast for low-risk; Full for high-risk |
| `frontend` | Yes (no dependencies) | Full (already ~2 min) |

`backend` and `frontend` are the preserved required status checks. They are never skipped,
never cancelled as a side effect of Fast CI, and never reported green unless they
actually completed successfully.

## 4. Which CI level runs, by change

| Change type | Example paths | CI levels run |
|-------------|---------------|---------------|
| README / docs only | `README.md`, `docs/ci/ROLLBACK.md` | Fast CI + `backend` (fast) + `frontend` |
| Frontend source only | `frontend/src/App.tsx` | Fast CI + Component CI (frontend) + `backend` (fast) + `frontend` |
| Backend API only | `backend/app/routes/catalog.py` | Fast CI + Component CI (backend) + `backend` (fast) + `frontend` |
| OpenCode catalog only | `.github/opencode/runtime/package.json` | Fast CI + Component CI (opencode) + `backend` (fast) + `frontend` |
| Edge Collector only | `edge_collector/snmp.py` | Fast CI + Component CI (edge-collector) + `backend` (fast) + `frontend` |
| DB shared contract only | `backend/app/models/asset.py` | Fast CI + Component CI (database-contracts) + `backend` (fast) + `frontend` |
| Migration | `backend/migrations/versions/0055_x.py` | Fast CI + **Full CI** |
| Authentication | `backend/app/auth/login.py` | Fast CI + **Full CI** |
| Security | `backend/app/security/rbac.py` | Fast CI + **Full CI** |
| Shared dependency | `frontend/package-lock.json` | Fast CI + **Full CI** |
| Workflow config | `.github/workflows/ci.yml` | Fast CI + **Full CI** |
| Infrastructure | `docker-compose.yml` | Fast CI + **Full CI** |
| Unknown path | `config/example.yaml` | Fast CI + **Full CI** (escalated) |
| Detection failure | *(router error)* | Fast CI + **Full CI** (escalated) |

When Full CI runs, component jobs are suppressed (Full CI is comprehensive), avoiding
redundant validation.

## 5. Validation summary per level

**Fast CI (every PR):** router classification, ruff lint, Alembic single-head migration
integrity, confidential preflight-report guard.

**Component CI (affected components):**
* backend — backend fast/full validation per risk
* frontend — frontend unit tests, typecheck, eslint, build
* edge-collector — Edge Collector lint + tests + SNMPv3 interop
* database-contracts — Alembic single-head + Compose settings validation
* opencode — CI policy/router compile + policy shape confirmation

**Full CI (high-risk):** Python 3.12/3.13/3.14 matrix (install + backend suite + Edge
Collector tests per version), Postgres/Redis integration, Alembic migrations + round trip,
CAP_NET_RAW, retention hostile validation, extraction apply, Compose settings,
`docker compose config`, frontend tests/typecheck, Playwright E2E, deployment tooling
regressions (`check_workflows.py` + `tests/deployment` unit tests), real OCR + real scanner
(clamd) coverage.

## 6. Safeguards

* Unknown paths → Full CI (never Fast CI).
* Change-detection failure → Full CI (detection-failed output).
* No false-green: component/Full CI require `needs.fast-ci.result == 'success'`; a failed
  Fast CI does not mask into green.
* Stale Full CI approvals prevented by `concurrency.cancel-in-progress` per branch.
* All `uses:` actions pinned to full commit SHAs (supply-chain integrity).
