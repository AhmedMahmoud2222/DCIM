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

A PR can change its own `ci.yml`, router, policy, gate and tests. A check that runs
code from the same PR is therefore not a boundary on its own. The design uses three
layers, and only the third is independent of the PR author.

1. **No PR routing code on the route path.** The route step runs the router and policy
   of the base commit. When the base has no policy, and for every event except
   `pull_request`, the step writes the full route itself. No Python file of the
   checkout runs on that path. A changed `ci_policy.py` or `ci_router.py` in the PR
   cannot select its own route.
2. **The gates check the route against their own diff.** Each gate checks out full
   history and computes `merge-base..head` itself (`ci_gate.scope_errors`). The gate
   rejects a fast or component route when the PR changes `.github/` or `tests/ci/`,
   when the diff fails, or when the route is not from a clean `pull_request`
   detection. A PR that changes the CI controls therefore needs every worker to
   succeed. This layer protects against a wrong route from a trusted router. It does
   not protect against a PR that changes `ci_gate.py` or `ci.yml`, because GitHub runs
   that PR's copy of them.
3. **Actor controls outside the PR.**
   - The OpenCode agent cannot change the CI controls. `opencode_publish.py` refuses
     `.github/workflows/`, `.github/scripts/ci_`, `.github/scripts/opencode_` and
     `tests/ci/`, and the GitHub App has no `workflows` permission.
   - A person with write access can still change `ci.yml` or `ci_gate.py` in a PR and
     make the required checks pass. No `pull_request` workflow can prevent this, and
     a `pull_request_target` workflow that runs PR code is not acceptable.

### Proposed owner configuration (not applied)

The repository owner must decide and apply one of these. This PR does not change a
ruleset or a protection.

- **Option A: code-owner review for the CI controls.** Add `.github/CODEOWNERS`:

  ```
  /.github/            @<second-reviewer>
  /tests/ci/           @<second-reviewer>
  /docs/ci/            @<second-reviewer>
  ```

  In ruleset #23651422 ("Default Policy") set the pull request rule to
  `require_code_owner_review: true`, `dismiss_stale_reviews_on_push: true`,
  `require_last_push_approval: true` and `required_approving_review_count: 1`. Keep
  the required status checks `backend` and `frontend` and the strict policy. GitHub
  does not accept the approval of the PR author, so the code owner must be a second
  account that does not author CI PRs. With only one account, this option blocks
  every CI PR until a second reviewer exists.
- **Option B: a required workflow from `main`.** If the repository moves to an
  organization plan with ruleset workflow rules, add "Require workflows to pass before
  merging" with a workflow file on `refs/heads/main`. GitHub runs that file from
  `main`, so a PR cannot change it. The workflow must not run PR code. It reads the PR
  diff as data and fails when the PR changes `.github/` or `tests/ci/` without the
  owner's approval of the exact head SHA.

Until one option is in place, the residual risk is: a person with write access can
change the CI controls and the required checks in the same PR. The owner's review of
each PR that changes `.github/` or `tests/ci/` is the control for that case.

- A change to the policy interface (a new worker or output) needs two steps: the base
  policy does not know the new output, so the gate treats the new worker as required.

## Consequences

- A documentation-only PR runs `route`, `ci-policy` and the two gates.
- A frontend-only PR runs `frontend-suite` and `browser-e2e`.
- A backend PR runs `backend-suite` and `browser-e2e`. The Python 3.12 to 3.14 matrix
  runs only on full routes and on each push to `main`. This is the main time saving, and
  the main coverage reduction against the old CI.
- The PR that adds this policy routes full, because its base has no policy.
