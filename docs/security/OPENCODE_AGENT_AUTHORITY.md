# OpenCode agent authority (v2.0)

Scope: `.github/workflows/opencode.yml`, `.github/scripts/opencode_{gate,router,collect,publish,verify_signatures}.py`, `.github/opencode/model-registry.json`, and `tests/deployment/test_opencode_*.py`. Delivered through PR #180.

Status: implemented and unit-validated offline. Not live-validated. Not accepted: the independent Codex review has not run.

## Correction to v1.0

v1.0 said review-comment triggers ran the workflow from the default branch. That is wrong for `pull_request_review_comment`: GitHub runs that event's workflow file from the PR merge ref, so a PR author could edit the gate and trigger it with a review comment. v2.0 removes that trigger. Inline review comments are ignored; the owner comments on the PR conversation tab (`issue_comment`), which always runs the default-branch workflow.

## Architecture

```
issue_comment (owner) ──> gate ──> opencode (agent) ──artifact──> publish
                         no secrets  read-only repo token          dedicated GitHub App token
                                     OPENCODE_API_KEY only         GitHub-signed commit + Draft PR
```

| Job | Runner state | Credentials | Can write to repo |
| --- | --- | --- | --- |
| `gate` | fresh | `GITHUB_TOKEN` read | no |
| `opencode` | runs model-driven code | `OPENCODE_API_KEY`; `GITHUB_TOKEN` with contents read, issues and pull-requests write | no |
| `publish` | fresh, never executes agent code | App private key (mint step only); short-lived App token | feature branches and Draft PRs only |

The agent job cannot push: its token has `contents: read`, and the App key is not in that job. OpenCode's own post-run `git commit` and `git push` therefore fail on the push (the agent step is `continue-on-error`). The working tree survives. `opencode_collect.py` packages the final tree as inert data (manifest plus file blobs). `publish` downloads that artifact on a clean runner, validates every entry, and commits through the GraphQL `createCommitOnBranch` mutation.

Why a separate runner: the agent can edit any file or git hook on its own runner. A publisher running there could be rewritten to leak a write token. Here the write token never exists on that runner.

## Authorization

Unchanged gate rules (v1.0), plus: only `issue_comment`; the commenter's immutable numeric ID must be in `OPENCODE_ALLOWED_ACTOR_IDS`; `author_association` must be OWNER, MEMBER or COLLABORATOR; the comment must start with `/oc` or `/opencode` plus a task; PR targets must be open with a same-repo head. Scripts come from `github.sha`, the commit that holds the workflow file, so workflow and gate always match. A merge that lands the workflow without its scripts makes the gate step fail, and nothing downstream runs.

### Bootstrap order

Merging PR #180 puts the workflow and every script on `main` in one commit. The workflow does nothing until the owner sets `OPENCODE_ALLOWED_ACTOR_IDS`; an empty value denies everyone. Do not set the variable until the first-activation checklist below reaches that step.

## Publication controls

- Branches: new work goes to `opencode/issue<N>-<run>-<attempt>`, created at the agent's start commit after the publisher confirms that commit is on the default branch. Commands on a PR update that PR's own head branch, and only if its tip still equals the agent's start commit. The default branch and any protected branch are refused.
- Control files the agent may not change (publisher rejects the whole change): `.github/workflows/`, `.github/opencode/`, `.github/scripts/opencode_*`, `tests/deployment/test_opencode_*`, `.github/CODEOWNERS`, `CODEOWNERS`, `dependabot.yml`, `.opencode/`, `opencode.json(c)`, any `AGENTS.md`, and this document.
- Unsupported: symlinks, submodules, new executable files and mode changes (the API cannot express them). Any one of them stops publication.
- Limits: 200 files, 5 MB per file, 20 MB total.
- Draft PRs say `Refs #N`, never `Closes`. Nothing merges, deploys or closes an issue.

## GitHub App

Name: `dcim-opencode-agent`. The owner creates and installs it; no step in this repo does.

| Permission | Level | Reason |
| --- | --- | --- |
| Contents | write | create branches, commit |
| Pull requests | write | open Draft PRs |
| Issues | write | status comment on the triggering thread |
| Metadata | read | implicit |
| Workflows, Administration, Actions, Secrets, Environments | none | not needed; keeps agent unable to edit workflows or settings |

Install on `AhmedMahmoud2222/DCIM` only. The workflow mints a token limited to this repository with exactly these three write scopes (`actions/create-github-app-token` v3.2.0, SHA-pinned). Tokens expire after one hour and the action revokes them when the job ends. Attribution: commits and PRs show `dcim-opencode-agent[bot]`. The agent's own replies appear as `github-actions[bot]`, because the agent job deliberately has no App key.

### Signing

GitHub signs commits created through the GraphQL API with an App token, and marks them Verified under the App's bot identity. No SSH key is involved, and `DCIM_GIT_SIGNING_KEY_B64` is not referenced anywhere (a test asserts this). The publisher checks `verification.verified` and the author login right after creating the commit; for a new branch it deletes the branch and opens no PR if the check fails. A second step re-verifies independently (`verified`, reason `valid`, author equals the App bot).

Not proven: no commit has been created through this path yet. First activation must show a Verified badge before this design counts as accepted. If the ruleset requires a signer that GitHub's web-flow signature cannot satisfy, stop and decide with the owner.

### Setup the owner performs

1. Create GitHub App `dcim-opencode-agent` with the permissions above, no webhook, install on this repo only. Generate a private key.
2. Repository variables: `OPENCODE_APP_CLIENT_ID` (App client ID), `OPENCODE_APP_SLUG` (e.g. `dcim-opencode-agent`), `OPENCODE_ALLOWED_ACTOR_IDS` (e.g. `285010264`; set last).
3. Environment `opencode-agent`: secret `OPENCODE_API_KEY` (an OpenCode key created only for this); deployment branches restricted to the default branch.
4. Environment `opencode-publish`: secret `OPENCODE_APP_PRIVATE_KEY` (the `.pem` contents); same branch restriction; optionally required reviewer.
5. Ruleset check: default branch requires PRs, status checks and verified signatures; the App is not on any bypass list; `.github/` paths covered by CODEOWNERS owned by the owner.
6. Do not reuse any personal token or the personal signing key.

Rotation: generate a new App key, replace `OPENCODE_APP_PRIVATE_KEY`, delete the old key in the App settings. Rotate `OPENCODE_API_KEY` in the OpenCode console and the environment secret. Do both at least quarterly and immediately after any run that behaves unexpectedly.

## Model routing

Registry: `.github/opencode/model-registry.json` (version-controlled, owner review). The router also hard-codes the six approved IDs, so the registry alone cannot introduce a model; a paid or unlisted ID fails validation.

Catalog check: all six IDs exist under provider `opencode` with zero cost in OpenCode's bundled model snapshot (anomalyco/opencode commit 50c5fce, 2026-10-20). That is not a live check with your key. At run time the workflow reads `https://opencode.ai/zen/v1/models` with the key; the response format and endpoint are assumed from the OpenCode source and are unverified.

| Category | Chain (first = preferred) |
| --- | --- |
| small_fix | GLM-5.3-Flash, DeepSeek V4.1 Flash, MiMo-V2.6-Flash |
| general (default) | DeepSeek V4.1 Flash, GLM-5.3-Flash, MiMo-V2.6-Flash |
| backend | MiMo-V2.6-Flash, DeepSeek V4.1 Flash, GLM-5.3-Flash |
| frontend | MiniMax M3.1 Flash, DeepSeek V4.1 Flash, GLM-5.3-Flash |
| analysis_large | Nemotron 3 Ultra*, MiniMax M3.1 Flash, DeepSeek V4.1 Flash |
| triage | Nemotron 3.5 Lightning*, GLM-5.3-Flash, DeepSeek V4.1 Flash |

All IDs carry the `opencode/` prefix and the `-free` suffix. *Blocked from repository code until the owner sets `repo_code_approved: true`; the router skips them and logs why.

Rules: keyword and path scoring against fixed tables (ties resolved by a fixed priority; no match means `general`); task text cannot name a provider or model; the first available model in the chain wins; at most three candidates; one agent attempt per run, with no retry loop and no paid fallback; if every candidate is unavailable, or the catalog cannot be read after three tries, the run stops before any model call. The selected model, category and reason print in the workflow log and go into the PR body. Routing never changes GitHub permissions. The job timeout stays at 35 minutes.

These routes are starting preferences, not measured rankings. Collect results from controlled runs before changing them.

### Provider data handling

OpenCode states that Zen providers use zero retention and no training "with exceptions" listed on its site. I could not read the exception list or per-model terms, so every model has `terms_verified: false`. Before first activation the owner must read the current terms for the four enabled models and confirm repository code may be sent to them. Material changes in provider terms require the owner to re-approve in the registry.

Controls in the pipeline: tasks that contain secret-like strings or the words credentials, production data, customer data, PII or personal data are refused before any model call (this also refuses legitimate tasks that merely mention credentials; use Claude Code or Codex for those); the agent config denies reads of `.env` files (the schema is taken from OpenCode's own defaults but unverified in this action). The agent still runs with its API key and a read-token in its environment, so a model with shell access could read and transmit them. That residual risk is why both are low-privilege and rotated.

## Audit

Each run records: commenter ID and login (event metadata), selected model and reason, base commit, agent step outcome, published commit SHA, branch, PR number, and verification result, in the workflow log, the commit trailers (`OpenCode-Run`, `OpenCode-Model`, `OpenCode-Category`, `Requested-By`) and the PR body. Artifacts expire after one day.

## Failure recovery

| Symptom | Action |
| --- | --- |
| Gate shows `authorized=false` | read the logged reason; no secret was exposed |
| Router stops: models unavailable | rerun later or use another agent; do not add a paid fallback |
| Publisher: branch moved | rerun the command on the updated PR |
| Publisher: protected or control file | the agent touched a forbidden path; make that change by hand |
| Commit not verified | publisher deletes the new branch; check App signing and ruleset, do not bypass |
| Agent failed with no changes | job fails; read the agent log |
| Suspected leak | revoke App key and `OPENCODE_API_KEY`, clear `OPENCODE_ALLOWED_ACTOR_IDS`, review runs |

## First activation (disposable)

1. Merge only after Codex review and owner approval. Leave `OPENCODE_ALLOWED_ACTOR_IDS` unset.
2. Comment `/oc test` as the owner: expect `authorized=false`, no environment approval prompt, no secret use.
3. Create the environments, App and secrets; set the variables including the actor ID.
4. On a disposable issue, comment `/oc fix a typo in README.md`. Check: model log line, agent step outcome (a failure here is expected from the blocked push), a `opencode/issue…` branch, one commit marked Verified and authored by the App bot, a Draft PR, and CI running on it.
5. Confirm the Draft PR cannot merge without required checks and review, and that a comment from a non-allowlisted user does nothing.
6. Delete the disposable branch and PR.

## Validation status

Offline, passing locally: 92 tests in `tests/deployment` (58 of them in the three OpenCode test files). They cover authorization, routing, fallback, allowlist, data-policy blocking, manifest and path policy, branch policy, publish flow with a fake API, verification failure handling, workflow permissions and secret scoping. `actionlint` reports no findings on the workflows. `shellcheck` was not available.

Not validated (needs your App, secrets and a run): App token scope in practice, agent edits through `opencode github run` with a read-only token and `persist-credentials: false`, verified status of API-created commits, CI triggering on bot-created PRs, ruleset behavior, live model availability.

## Remaining risks

- A GitHub-hosted runner holds an API key and a read/comment token while a model reads issue and PR text that others wrote. Fork PRs are refused; same-repo branches from other writers are not.
- A non-owner comment can replace an owner's queued run in the concurrency group.
- The third-party action installs OpenCode from `opencode.ai` at run time.
- If the agent step does not fail on a blocked push (behavior unverified), the flow still works; if it exits non-zero without changes the job fails by design.
- Free-model terms are unverified; availability can change without notice.
