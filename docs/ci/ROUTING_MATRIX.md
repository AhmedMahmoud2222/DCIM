# CI routing matrix

Source: `.github/scripts/ci_policy.py` (policy version 3). The router loads the policy
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

## Pin-only exception (rule 2)

A PR that changes only action pins routes to `fast` (no worker) instead of `full`.
The backend suites never run a workflow, so they cannot test a pin. The review of the
new pin against the upstream release tag does. The exception is narrow:

* Every changed file is *modified*, mode `100644`, and one of: a workflow in
  `.github/workflows/` (not `ci.yml`, not `deploy.yml`, no subdirectory),
  `.github/acceptance/policy.json`, or `.github/scripts/orchestration_claude_adapter.py`.
* Each file equals its base version once its pin lines are reduced to the action
  reference. A pin line is a whole `uses: owner/repo@<40 hex>` line (with an optional
  `# version` comment), the `"review_action"` line of the policy file, or the `ACTION`
  line of the adapter. Any other difference sends the PR to `full`.
* The router (from the base commit) and the gate (from the PR head) both read the
  files and apply `ci_policy.pin_only`. The gate rejects `fast` for a `.github/`
  change unless its own reading agrees.
* The `route`, `ci-policy` and gate jobs, and the separate deployment checks, still run.
  `tests/ci/ci_support.py` (`pin_cases`) checks 39 cases, and the mutation tests cover
  the rule.

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
