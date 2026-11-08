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
  `.github/workflows/` (not `ci.yml`, `deploy.yml`, `refresh-branch.yml`, `opencode.yml`, `agent-collaboration.yml`, `claude.yml`, `claude-code-review.yml`, `agent-claude.yml` or `acceptance-review.yml`, no subdirectory; the last six use write-capable GitHub App tokens),
  `.github/acceptance/policy.json`, or `.github/scripts/orchestration_claude_adapter.py`.
* Each file equals its base version once its pin lines are reduced to the action
  reference. A pin line is a whole `uses: owner/repo@<40 hex>` line (with an optional
  `# version` comment of letters, digits and `. _ + -` only), the `"review_action"` line of the policy file, or the `ACTION`
  line of the adapter. Any other difference sends the PR to `full`.
* A file with a character outside this set is never pin-only: tab, LF, printable ASCII, and
  the printable Unicode YAML allows minus U+0085, U+2028, U+2029, U+FEFF, U+FFFE and U+FFFF.
  Normalization drops comments, so a line separator in a comment could hide a step and a
  forbidden character (NUL, DEL) could break the workflow. CR is outside the set too.
* The comment limit exists because a pin-shaped line can sit inside a `run: |` block, where
  the comment is script text. The line rules alone cannot tell such a line from an action
  reference, so a second check reads the files as data. It parses both versions, masks the
  40-hex pin of every `uses` value of a job or a step, and requires the two structures to be
  equal. A pin-shaped line in an `env` value, a flow mapping, a quoted multi-line string, a
  `with` block or a script then changes the structure and the PR goes to `full`. A workflow
  with an anchor, an alias, an explicit tag, a merge key (`<<`), a non-scalar key, a duplicate
  key (compared as raw text, also ignoring case) or invalid YAML is never pin-only. The
  workflow check composes YAML nodes and never constructs Python objects, so it compares raw
  scalar text, quoting style and order. PyYAML (YAML 1.1) would read `on` as `True`, equal to
  `true`, and `012` as 10, and would let such equal keys overwrite each other; GitHub (YAML 1.2,
  string keys) keeps them apart. Any edit that PyYAML would hide is now a difference. The same check
  reads `policy.json` as JSON (no duplicate keys) and the adapter as Python (`ast`, exactly one
  `ACTION` assignment).
* The structure check needs PyYAML. When `import yaml` fails, no workflow is pin-only and the
  rule changes nothing; the route and gate jobs run bare `python3`, so confirm the runner has
  PyYAML or install the pinned version in those jobs.

## Workers

| Worker | Original job | Content |
|---|---|---|
| `backend-suite` | `backend` | PostgreSQL 16 and Redis 7, least-privilege roles, Alembic single head, ruff, mypy, migrations and round trip, retention hostile tests, extraction apply, real OCR, clamd tests, full pytest |
| `frontend-suite` | `frontend` | npm audit, Vitest, typecheck, ESLint, build |
| `backend-runtime-matrix` | same | The backend suite and the Edge suite on Python 3.12, 3.13 and 3.14 |
| `backend-runtime-shards` | same as `backend-runtime-matrix` | The same suites, split into three shards per Python version by `ci_shard.py`. Each shard has its own database. Shard 0 also runs lint, type check, the retention and extraction steps and the Edge suite |
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

## Backend shards

`backend-runtime-matrix` keeps the check names `backend suite (Python 3.12)`, `3.13` and `3.14`, which the release verifier and the branch rules name. Its three jobs run no tests. Each one fails unless every `backend-runtime-shards` job succeeded, so one failed shard turns all three red; the shard job name shows the Python version and shard. The shards run on an `ubuntu-latest` runner each, nine runners in all. `ci_shard.py` assigns test files by size and fails on an empty shard, so a missing file list never lets `pytest` collect the whole suite. `tests/ci/test_ci_workflow.py` and the mutation tests pin the shard count, the preloaded files and the aggregate wiring.

`backend-suite` (the Python 3.11 job) follows the same pattern. `backend-suite-shards` runs its tests in three shards, and `backend-suite` keeps its check name (`.github/acceptance/policy.json` lists it) and fails unless every `backend-suite-shards` job succeeded. A full-route run now takes as long as its slowest shard, about 25 minutes, instead of the 49 minutes of the unsharded 3.11 job.

The three `test_bulk_import_real_*` files start embedded Celery workers. Because `ci_shard.py` assigns files by size, a PR that changes any test file's size can move other tests next to them, and the two-worker fencing test then failed in shard 1 on two unrelated PRs (#255). Those three files run in their own step in shard 0, in a fixed order, and are preloaded so no shard selects them again. Their placement no longer depends on file sizes. The tests are still order-dependent until #255 finds the cause; the pin removes the exposure and does not fix the test.
