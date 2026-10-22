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
   cannot change the route of its own run. If the base has no policy, the route is
   full. Every event except `pull_request` gives full, so each push to `main` runs every
   job and the release verifier finds every job name.
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
  (multi-commit, rename, delete, moved base, bad SHAs), the 400-case gate truth table,
  the workflow structure rules and the mutation tests.
- The workflow file and `ci_gate.py` come from the PR head. A PR that edits them can
  change the gate of its own run. The route of such a PR is full, but only a review by
  the owner stops a malicious edit of `.github/`. A CODEOWNERS file for `.github/` and a
  ruleset that requires code-owner review close this gap. That change needs
  authorization from the repository owner. This PR does not change the ruleset.
- A change to the policy interface (a new worker or output) needs two steps: the base
  policy does not know the new output, so the gate treats the new worker as required.

## Consequences

- A documentation-only PR runs `route`, `ci-policy` and the two gates.
- A frontend-only PR runs `frontend-suite` and `browser-e2e`.
- A backend PR runs `backend-suite` and `browser-e2e`. The Python 3.12 to 3.14 matrix
  runs only on full routes and on each push to `main`. This is the main time saving, and
  the main coverage reduction against the old CI.
- The PR that adds this policy routes full, because its base has no policy.
