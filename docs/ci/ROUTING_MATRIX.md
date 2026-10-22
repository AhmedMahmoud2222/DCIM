# CI routing matrix

Source: `.github/scripts/ci_policy.py` (policy version 2). The router loads the policy
from the PR base commit. `tests/ci/ci_support.py` (`POLICY_CASES`) checks each row below.

## Rules, in order

| Order | Path | Route |
|---|---|---|
| 1 | No changed paths, or a path that is empty, absolute or has `\` | full |
| 2 | A path in `HIGH_RISK_PATHS`: `.github/`, `tests/`, `scripts/`, `backend/migrations/`, `backend/scripts/`, `backend/app/core/`, `backend/app/db/`, `backend/app/domain/auth/`, the auth, RBAC, authority and access-control modules, `backend/app/main.py`, `backend/app/api/deps.py`, dependency manifests, Dockerfiles, Compose files, `.env.example` | full |
| 3 | A path with `auth`, `secur`, `rbac`, `permission`, `authority`, `privileg`, `crypt`, `secret`, `credential`, `password`, `token`, `session`, `sandbox`, `opencode`, `preflight`, `retention` or `audit` | full |
| 4 | `docs/**` or a `*.md` file in the repository root | no worker |
| 5 | `edge_collector/**` | `edge-collector`, `edge-interop`, `backend-suite` |
| 6 | `backend/**` | `backend-suite`, `browser-e2e` |
| 7 | `frontend/**` | `frontend-suite`, `browser-e2e` |
| 8 | Any other path | full |

One full path makes the full route for the PR. Otherwise the route is the union of
the workers of each path. A full route runs all six workers.

## Workers

| Worker | Original job | Content |
|---|---|---|
| `backend-suite` | `backend` | PostgreSQL 16 and Redis 7, least-privilege roles, Alembic single head, ruff, mypy, migrations and round trip, retention hostile tests, extraction apply, real OCR, clamd tests, full pytest |
| `frontend-suite` | `frontend` | npm audit, Vitest, typecheck, ESLint, build |
| `backend-runtime-matrix` | same | The backend suite and the Edge suite on Python 3.12, 3.13 and 3.14 |
| `edge-collector` | same | Edge Collector ruff and pytest |
| `edge-interop` | same | SNMPv3 interoperability harness and results artifact |
| `browser-e2e` | same | Isolated PostgreSQL and Redis, backend, Celery, Vite, Playwright |

## Required checks

| Check | Waits for | Fails when |
|---|---|---|
| `backend` | `route`, `ci-policy` and all six workers | a control job did not succeed, the route is invalid or for another SHA, a selected worker did not succeed, or a worker that is not selected failed or was cancelled |
| `frontend` | `route`, `ci-policy`, `frontend-suite`, `browser-e2e` | the same rules for its scope |

## Examples

| Change | Workers |
|---|---|
| `README.md`, `docs/**` | none |
| `frontend/src/App.tsx` | `frontend-suite`, `browser-e2e` |
| `backend/app/application/alarm_service.py` | `backend-suite`, `browser-e2e` |
| `edge_collector/edge_collector/snmp.py` | `edge-collector`, `edge-interop`, `backend-suite` |
| `backend/app/domain/auth/models.py`, `backend/app/core/security.py` | all (full) |
| `.github/scripts/opencode_sandbox.py`, `.github/workflows/ci.yml` | all (full) |
| Rename `backend/app/domain/auth/x.py` to `docs/x.py` | all (full): the old path counts |
| `Makefile` or any other unknown path | all (full) |
| Push to `main`, `workflow_dispatch`, failed detection | all (full) |
