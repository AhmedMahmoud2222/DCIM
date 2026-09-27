## Status

**Supersedes:** [PR #34](https://github.com/AhmedMahmoud2222/DCIM/pull/34) (`docs/DEPLOYMENT_GATE_ENFORCEMENT.md`), written against `main@39151d8` on 2026-09-26, before [PR #35](https://github.com/AhmedMahmoud2222/DCIM/pull/35) merged. This document does not modify code, does not enable production deployment, and does not merge PR #34's branch. It is a governance and configuration proposal only.

**Base for this document:** `main@8d1793e161864ef804b4f3e0268177bff82b483e`, verified directly against the current repository state, not against a description of it.

## What PR #34 got right, and what it no longer describes

PR #34's diagnosis was correct at the time: the Deployment validation gate ran successfully on main, no automated deployment workflow existed, and required-status-checks were unverified. Two of its central claims are now obsolete and must not be repeated:

- **"No `deploy.yml`, `release.yml` or environment-based deployment workflow found."** Obsolete. `main` now contains `.github/workflows/deploy.yml`, `.github/scripts/verify_release_sha.py`, `docker-compose.production.yml`, `scripts/deploy-docker-compose.sh`, and `.github/workflows/deploy-tools-test.yml`, all merged via PR #35 and independently reviewed on this PR at `06589f1c7692d76b667349812fb84fc83ed09666`.
- **"No automated deployment workflow exists."** Obsolete for staging. A real, Docker-tested staging validation workflow exists and passes in CI. It remains accurate for production: no code path in the repository can execute a production deployment.

One claim from PR #34 remains accurate word for word: `docs/OPERATIONS.md` still states "There is currently no repository deployment job that provisions production resources, so CI alone cannot prevent a person from running `docker compose up` outside GitHub." That sentence was true before PR #35 and is true after it. Production deployment was never in scope for PR #35, and nothing here changes that.

## Newly verified fact PR #34 did not have

Checking `main`'s branch metadata directly (not inferring it from CI behavior) shows **`main` currently carries a branch protection rule** (`protected: true`). PR #34 treated required-status-checks as entirely unconfigured. That may no longer be the case. The tools available in this session cannot read which checks that rule requires, whether it requires reviews, or whether it can be bypassed by administrators. **The owner must confirm the rule's actual contents in Settings → Branches** before anyone relies on it. Do not treat this document's mention of `protected: true` as proof that any specific check is enforced.

## What is implemented in code today (staging only)

Full detail lives in `docs/DEPLOYMENT_IMPLEMENTATION.md`; this section only orients the governance discussion:

- `verify_release_sha.py` binds a release SHA to specific successful GitHub Actions runs by workflow ID, run/attempt, check-run ID, check-suite ID and app identity, and fails closed on incomplete evidence.
- `deploy.yml` is a manual `workflow_dispatch` with `staging`/`production` inputs. The `staging` path validates the effective Compose configuration without starting containers. The `production` path is a deliberate dead end: no job runs a production deployment.
- `scripts/deploy-docker-compose.sh` runs a real, lockable, rollback-capable staging deployment and refuses anything but `staging` outright.
- `deploy-tools-test.yml` and `deployment-validation.yml` provide real, Docker-backed regression coverage (not mocks) for the combined stack, verified across 11 passing CI jobs at the current `main` HEAD.

None of this constitutes deployment governance by itself. It is the mechanism that governance controls would apply to.

## Outstanding governance requirements

These are carried forward from PR #34's analysis, corrected to reference the current implementation, and are still unimplemented as of this document:

### 1. Confirm and configure branch protection on `main`

Owner action, not code. In **Settings → Branches**, confirm the existing rule and set, at minimum:

- Require status checks to pass before merging. Current candidate checks, read directly from the three workflows now on `main`:
  - From **CI**: `backend`, `backend suite (Python 3.12)`, `backend suite (Python 3.13)`, `backend suite (Python 3.14)`, `frontend`, `browser-e2e`, `edge-collector`
  - From **Deployment validation**: `Compose smoke`, `Deployment validation gate`
  - From **Deployment Tools Test**: `Deployment tooling regressions`, `Disposable combined-stack startup and rollback`
- Require branches to be up to date before merging.
- Require pull request review before merging, with a defined minimum approval count.
- Restrict who can push directly to `main`.

**Caveat the owner should weigh before making Deployment Tools Test's jobs required:** that workflow triggers only on pushes/PRs touching a specific path list (deployment scripts, Compose files, migrations, and related backend/frontend files). GitHub does not have a setting that automatically satisfies a required check when the workflow that produces it is skipped by top-level `paths`/`paths-ignore` filtering. A required check bound to such a workflow stays pending, not passing, on any PR that doesn't touch those paths, which blocks that PR's merge indefinitely. This is a different situation from a job inside a workflow that *does* run being skipped by its own `if:` condition: that job reports a real `skipped` conclusion, and GitHub's required-checks mechanism treats a skipped conclusion as satisfying the requirement. The problem described here is specifically the workflow itself never running, so no conclusion, successful or skipped, is ever reported for its jobs.

Before marking either of Deployment Tools Test's jobs as a required check, the owner should choose one of the following, none of which is implemented by this document:

- **(A) Keep the workflow path-filtered and do not mark its jobs as required checks.** Simplest option. The trade-off: a PR that doesn't touch the filtered paths merges without that regression coverage having run at all, by design.
- **(B) Remove the workflow's top-level path filters so it runs on every applicable PR, and use conditional execution inside it (an `if:` on specific steps or jobs) wherever a full run isn't warranted.** Preserves required-check reliability, since the workflow itself always runs and always reports a conclusion; costs CI time on every PR regardless of what changed.
- **(C) Introduce a separate, unconditional deployment-validation gate job**, purpose-built to always trigger and always report a real conclusion, and make that job, not the path-filtered workflow's own jobs, the required check.

This document takes no position on which option to choose. It is documenting the constraint, not proposing a specific fix to the workflow.

### 2. Deployment approval enforcement (proposed, not implemented)

No GitHub Environment approval gate exists in this repository. `deploy.yml` has no job protected by an `environment:` block and no required reviewers. This is a genuine gap, not a design choice hidden behind a removed feature: the prior implementation's `request-approval` job was deleted along with the rest of its broken production path, and nothing has replaced it.

If the owner wants deployment approval enforced through GitHub:

1. Create an environment (for example `production-deployment`) under **Settings → Environments**, with required reviewers and a branch restriction to `main`.
2. Add a job to a production-specific workflow that runs under that environment and depends on `verify-release`'s existing SHA-verification output, so approval is requested only for a SHA that has already passed provenance checks.
3. Do not build a new provenance check for this job. `verify_release_sha.py` already exists and is tested; reuse its output.

**This section describes a proposal. Nothing above it currently exists in the workflow. Do not cite this document as evidence that an approval gate is active.**

### 3. Deployment audit trail (proposed, not implemented)

No job records who deployed what, when, or under whose approval. The original design's artifact-upload/`record-deployment` job was removed along with the broken approval job it served; nothing replaced it. If an audit trail is still required:

- Record, per deployment: timestamp, deployed SHA, the specific successful `verify_release_sha.py` evidence it was checked against, the approving reviewer (once requirement 2 exists), and the resulting health-check outcome.
- Keep this record outside ephemeral CI logs. A GitHub Actions job summary disappears with log retention; a durable record needs either a committed change log (per `docs/OPERATIONS.md`'s existing "Deployment evidence register" convention) or an external system.

### 4. Operational ownership

No document names who is authorized to approve a production deployment, who holds production host access, or who is on call for a failed rollback. `docs/OPERATIONS.md` covers technical procedure (backup, migration order, health checks, incident starting points) thoroughly but assumes an operator already exists with defined authority. Requirement 2's required-reviewers list is one input to this, but the owner still needs to decide and record, outside of GitHub configuration, who those people are and what authorizes them.

## Summary table

| Requirement | Source | Status |
|---|---|---|
| Branch protection confirmed active on `main` | Verified this document | Confirmed to exist; exact contents unverified, owner must check |
| Required status checks configured to the current CI/deployment jobs | PR #34, updated here | Not verified as configured |
| GitHub Environment approval gate for production | PR #34, updated here | Not implemented in code |
| Deployment audit trail / change record | PR #34, updated here | Not implemented in code |
| Named operational ownership for approval and on-call | PR #34, restated here | Not documented anywhere |
| Staging validation, real-container tested | New since PR #34 | Implemented and passing in CI |
| Production deployment capability | PR #34, updated here | Does not exist; disabled at both workflow and script level |

## Recommended disposition for PR #34

Once this document is available on `main`, PR #34 should be **closed as superseded**, citing this PR, without merging its stale branch (`deployment-gate-enforcement` at `6ac96cf7e8067acf1365ac8b47bbe648cacfd043`, currently `behind` main by dozens of commits). That closure is an owner action; it is not performed by this PR.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01M7vQ7rbFjnxHGhbtCzMBNW