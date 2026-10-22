# ADR: Risk-based routing for the CI workflow

**Status:** Proposed (PR #191, Draft)

## Context

`.github/workflows/ci.yml` ran every job on every PR. A full run takes about 48 minutes
(`docs/ci/BASELINE_RUNTIME_REPORT.md`). The active ruleset requires the check runs
`backend` and `frontend`. `.github/scripts/verify_release_sha.py` requires these job
names on each release commit of `main`: `backend`, `frontend`,
`backend suite (Python 3.12|3.13|3.14)`, `browser-e2e`, `edge-collector`.

## Decision

1. **Original jobs stay as workers.** The original jobs keep their steps, services and
   environment. Two job ids change: `backend` becomes `backend-suite` and `frontend`
   becomes `frontend-suite`. Each worker gets `needs: route` and one `if:` line.
2. **The required checks are gates.** New jobs `backend` and `frontend` (no `name:` key,
   so the check name is the lowercase job id) run `if: always()` and call
   `.github/scripts/ci_gate.py`. A gate passes only if all of these are true:
   - `route` and `ci-policy` have result `success`.
   - The route output is valid JSON for the exact head SHA of the event.
   - Each worker that the route selects has result `success`.
   - Each worker that the route does not select has result `skipped` or `success`.
   - The gate `needs` list is equal to `ci_gate.GATES`.

   The `backend` gate waits for every worker. The `frontend` gate waits for
   `frontend-suite` and `browser-e2e`. A failed, cancelled or missing result fails the
   gate. A worker that the base policy does not know is always required.
3. **The route comes from the base commit.** The `route` job copies `ci_policy.py` and
   `ci_router.py` from `github.event.pull_request.base.sha` and runs that copy. A PR
   cannot change the route of its own run through these files. If the base has no
   policy, and for every event except `pull_request`, the route step writes the full
   route itself and runs no Python file of the checkout. Each push to `main` therefore
   runs every job, and the release verifier finds every job name. The gate accepts a
   route below full only from a clean `pull_request` detection (`event` is
   `pull_request` and `degraded` is false).
4. **Change detection fails closed.** The router diffs `merge-base(base, head)..head`
   with `git diff --no-renames --name-only -z`. A rename gives the old path and the new
   path. A malformed SHA, a missing commit, a failed merge-base or a failed diff gives
   full. The router has no smaller fallback diff.
5. **The policy is conservative.** See `docs/ci/ROUTING_MATRIX.md`. Unknown paths,
   security words in a path, `.github/`, `tests/`, `scripts/`, migrations, core
   configuration and dependency manifests give full. Any backend change runs the
   complete original `backend-suite` (PostgreSQL, migrations, mypy, OCR, full pytest).
6. **The dedicated workflows do not change.** `deploy-tools-test.yml`,
   `deployment-validation.yml`, `opencode-security-validation.yml`, `opencode.yml` and
   `deploy.yml` keep their own triggers. The routed CI does not copy or replace them.
7. **Concurrency.** A new push to a PR cancels the older run of that PR. The group for
   a push to `main` is the commit SHA, so a `main` run is never cancelled.

## Enforcement and residual risk

- `ci-policy` runs `tests/ci` on each event: the policy scenarios, real git histories
  (multi-commit, rename, delete, moved base, bad SHAs), the gate truth table, the
  workflow structure rules, a bash run of the real route step against a hostile PR
  router, and the mutation tests. These tests are regression tests. They come from the
  PR head, so they are not a trust boundary.
- **Open (P1-07): the code in this workflow is not an independent boundary.** For a
  `pull_request` event, GitHub runs `ci.yml` from the PR merge commit, and the gates
  run `ci_gate.py` from the checkout. A PR that edits `.github/workflows/ci.yml` or
  `.github/scripts/ci_gate.py` can change the required checks of its own run. No code
  in a `pull_request` workflow can prevent this. A `pull_request_target` workflow that
  runs PR code is not an acceptable fix.
- Two controls can close the gap. Both need a ruleset change by the repository owner,
  and this PR does not make it:
  1. A ruleset "require workflows to pass" rule with a workflow from `main`. GitHub runs
     that workflow from the ruleset ref, not from the PR, so the PR cannot edit it. The
     workflow can recompute the route with the policy of `main` and fail when the PR
     changes `.github/` without a full route.
  2. A `CODEOWNERS` entry for `/.github/` and "require review from code owners" in the
     ruleset. This needs a code owner who is not the PR author, because GitHub does
     not accept the author's own approval.
- A change to the policy interface (a new worker or output) needs two steps: the base
  policy does not know the new output, so the gate treats the new worker as required.

## Consequences

- A documentation-only PR runs `route`, `ci-policy` and the two gates.
- A frontend-only PR runs `frontend-suite` and `browser-e2e`.
- A backend PR runs `backend-suite` and `browser-e2e`. The Python 3.12 to 3.14 matrix
  runs only on full routes and on each push to `main`. This is the main time saving, and
  the main coverage reduction against the old CI.
- The PR that adds this policy routes full, because its base has no policy.
