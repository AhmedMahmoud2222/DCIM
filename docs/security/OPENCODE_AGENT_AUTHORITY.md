# OpenCode agent authority (v3.1)

Scope: `.github/workflows/opencode.yml`, `.github/scripts/opencode_{gate,router,catalog,sandbox,proxy,guard,collect,publish,review,verify_signatures}.py`, `.github/opencode/` (registry, reviewer capability matrix, secret-scan allowlist), and `tests/deployment/test_opencode_*.py`. Delivered through PR #180.

Status: implemented and unit-validated offline. Not live-validated. Not accepted: the independent review of this revision has not run. The previous head (`29740c6`) was rejected by Codex and is not evidence of anything.

## Review disposition

| Finding | Disposition | Where |
| --- | --- | --- |
| P1 generation success contract | Replaced the third-party action with `opencode run --format json` behind a guard. Publication needs exit 0, no timeout, all events parseable, no `error` event, and a final `step_finish` with reason `stop`. | `opencode_guard.py`, workflow `opencode` job |
| P1 independent PR binding | Publisher refetches the issue or PR from GitHub and checks number, open and unmerged state, base and head repos, approved head branch, head SHA and branch tip before any write. | `bind_target` in `opencode_publish.py` |
| P1 trusted baseline | Gate records the baseline before the agent runs. Trusted SHA, manifest SHA and live branch SHA must all agree. | `binding` in `opencode_gate.py`, `bind_target` |
| P1 confidentiality boundary | Model input is the fixed preface plus the command text and files the agent reads from a sanitized copy. OS isolation: unprivileged user, loopback-only egress, key held by a proxy. | see "Input boundary" |
| P1 model data terms | A model needs terms verified with evidence, owner approval for repository code, healthy status and a live catalog entry. All six ship disabled. | `blockers` in `opencode_router.py`, registry |
| P2 token permissions | Generation job has `contents: read` only and no GitHub credentials in the agent's environment. Comments moved to trusted jobs. | workflow |
| P2 failure recovery | Existing PR branches are updated only by non-forced fast-forward after the commit is verified on a staging branch. Residual state is reported as partial. | `publish` |
| P2 concurrency | Concurrency applies only to jobs that run after authorization. | workflow |
| P3 schema validation | Strict manifest schema, declared sizes checked against blobs, undeclared artifact files rejected. | `validate_manifest` |
| Separation rule (added request) | Distinct reviewer model and session, read-only reviewer, evidence-based reviewer ranking, `REVIEW_BLOCKED`, commit-bound COMMENT review, staleness. | see "Implementer-reviewer separation" |

## Architecture

```
issue_comment (owner) -> gate ----------------------> opencode (sandboxed generation) --artifact--> publish --> report
                         no secrets                   read-only token, no GitHub write rights      App token  status comment
                         fixes trusted baseline       API key held by a proxy, not the agent       signed commit + Draft PR
```

- `gate` checks the commenter, parses the command exactly, fetches PR metadata and outputs the trusted target: `kind`, `number`, `authorized_sha`, `head_ref`. For issues the baseline is the default-branch commit that holds the workflow (`GITHUB_SHA`); for PRs it is the PR head SHA at authorization time.
- `opencode` checks out trusted scripts (workflow commit) and the target (authorized SHA) separately, reads the live model catalog, routes, builds the sandbox, runs the agent through the guard, and collects the result as data.
- `publish` runs on a fresh runner, mints a one-hour App token, publishes, and re-verifies the signature independently.
- `report` posts a plain status comment (issues write only) when nothing was published. Its text is built from fixed strings and sanitized enum values; no model output reaches it.

`issue_comment` is the only trigger. `pull_request_review_comment` runs the workflow file from the PR merge ref, which lets a PR author edit the gate, so it stays unsupported.

## Successful-generation contract

The previous integration ran `dceoy/opencode-action`, which runs its own commit and push and exits nonzero for many reasons; generated files could not be told apart from a failed run. The pinned action offers no generation-only mode, so it is gone. The workflow now installs OpenCode `1.19.3` (the version the action pinned) and calls `opencode run --format json` as the unprivileged user. In v1.19.3 `run` only produces a session: it makes no commit, push, PR or comment, auto-rejects any permission request that is not explicitly allowed, sets a nonzero exit code on a session or prompt error, and emits `step_finish` events (behavior read from `packages/opencode/src/cli/cmd/run.ts` at tag v1.19.3). That the `step_finish` part carries a `reason` field with value `stop` on normal completion is assumed from OpenCode's event model and is unverified; if absent, every run fails closed.

`opencode_guard.py` runs as the runner user. The agent runs as another user and cannot change the guard, its result file, or its job outputs. Collection, upload and publication all depend on `generation == success`. The publisher refuses anything else before making an API call, so a changed working tree after a failed run is never published. Cancellation never reaches publication because the job result is not `success`.

## Input boundary

Everything the model can receive:

1. The fixed preface in `opencode_guard.py` plus the authorized command text. The router refuses command text that looks like a credential or mentions credentials, production data, customer data or PII.
2. Files the agent reads inside `/srv/ocagent/work`, a sanitized copy of the tracked tree at the authorized SHA.
3. Its own tool output.

The upstream action's addition of issue bodies, comment threads and PR metadata is gone: no discussion text is passed. The copy excludes symlinks, `.opencode/`, `opencode.json(c)`, `.claude/`, `.agents/`, `.cursor/`, and credential-like names (`.env*` except examples, keys and certificates, databases and dumps, `.npmrc`, kubeconfig and similar). The remaining text files are scanned for high-confidence secrets (private-key blocks, GitHub, AWS, OpenAI-style, Slack and Google API keys); a hit fails closed before any model call, naming paths and never content. `.github/opencode/secret-scan-allowlist.txt` lists owner-approved fake fixtures. The scan cannot recognise every secret format; it is a backstop.

Execution isolation (not only OpenCode permissions):

- Dedicated user `ocagent`; the runner home is `chmod 700`, so the checkout, tokens and runner files are unreadable to it.
- `env -i` with a fixed environment: no `GITHUB_*`, no runner variables, no keys.
- Firewall rules for `ocagent`: loopback only, IPv4 and IPv6. The step probes `api.github.com` as `ocagent` and aborts the run if it connects.
- The OpenCode key is held by `opencode_proxy.py`, running as the runner user. The agent talks to it over loopback with a placeholder; the proxy replaces the credential and forwards only `GET` and `POST` to `/inference/` and `/zen/v1/` on `opencode.ai`. The agent cannot read the key from its environment, config or a process listing.
- Managed config in `/etc/opencode/opencode.json` plus `OPENCODE_CONFIG_CONTENT`, `OPENCODE_DISABLE_PROJECT_CONFIG` and `OPENCODE_DISABLE_EXTERNAL_SKILLS`, and the repo's own config is not copied at all. The config allows read, edit, glob, grep, list and bash; denies web access, subagents and reads outside the work directory; and adds `.env` read denials. Anything not allowed is rejected, not prompted.
- Collection runs against a runner-owned baseline git dir with the sandbox as a bare work tree, so no `.git` content the agent plants is consulted. A test plants a `core.fsmonitor` hook and checks it never runs.

Not covered: a model can still send what it reads from the sanitized copy to the OpenCode endpoint, which is the point of using it. Free-model terms decide what happens to that data (see below).

## Implementer-reviewer separation

Rule IMPLEMENTER_REVIEWER_SEPARATION applies when OpenCode implements and reviews on the same branch or PR. It is configured by `agent_separation` in the model registry. Every field is enforced in code and the registry may not weaken any of them: a missing, extended or altered policy blocks all routing, including implementation routing.

| Field | Enforced by |
| --- | --- |
| `enabled`, `scope: same_branch_or_pr` | implementer records are read from the PR being reviewed |
| `require_distinct_models` | `check_separation`; the selector also excludes every model that implemented on the PR |
| `require_distinct_sessions` | `check_separation`; the guard records the OpenCode session id, a missing id fails the run |
| `review_read_only` | review config denies edit tools, the work tree is `root`-owned and `a-w`, and `verify-readonly` fails on any change |
| `reviewer_selection: best_verified_capability` | `select_reviewer` ranks only on recorded evaluation evidence |
| `block_if_no_eligible_reviewer` | no eligible reviewer produces `REVIEW_BLOCKED` and a PR comment |
| `external_acceptance_required` | every review states it is not an approval; `acceptance_status` never returns accepted |

Flow. `/oc <task>` implements (existing path). The publisher writes the implementer into the signed commit as trailers: `OpenCode-Model`, `OpenCode-Session`, `OpenCode-Run`, `OpenCode-Mode: implement`, and posts a comment naming implementer model, session, branch and commit. `/oc review [focus]`, on a PR only, starts the review path: the gate fixes the PR head as the reviewed commit; the review job reads implementer records from GitHub-verified commits authored by the App bot (forged trailers on other commits are ignored), selects a reviewer, and runs it as a new OpenCode session in the same loopback-only sandbox with all edit tools denied. Chaining implementation into review automatically is not done: the owner issues both commands.

Reviewer choice. A model qualifies only if it passes the implementation requirements (terms verified, repository code approved, healthy, in the live catalog), did not implement on this PR, and has a complete evaluation record in `.github/opencode/reviewer-capability-matrix.json`. Among those the highest weighted score wins; ties go to the stronger weakest dimension, then the model id. Weights follow the requested priorities: correctness 0.25, security 0.25, database and transaction safety 0.15, test quality and regression detection 0.15, cross-module dependencies 0.10, complex-change inspection 0.10. A record needs suite, suite version, date, an https run URL, a results hash and a score for every dimension, with each score at least `minimum_dimension_score` (0.5). Speed, latency and price are not inputs, and extra fields invalidate a record. The same provider can supply both models; only the model id and session matter.

No model has evaluation evidence yet, so the matrix ships empty and every review ends in `REVIEW_BLOCKED`. I have not run an evaluation and the matrix does not claim any model is better. Evaluation protocol for the owner: build a seeded-defect corpus (changes with known planted defects across the six dimensions, kept out of the repo's production paths), run each candidate through the same sandbox in review mode, score the fraction of planted defects found with the right severity and file, store the raw results, and enter the scores with the results hash and run URL through a normal PR.

Review output. The reviewer ends with one JSON block. The trusted publisher validates it (severity, affected files, evidence required for critical and high, recommendation), strips mentions, hidden markers and slash commands, and recomputes the verdict: any critical or high finding makes it `BLOCKING_FINDINGS` whatever the model claimed; an approval-style verdict is rejected. It posts one PR review with event `COMMENT` and `commit_id` set to the reviewed commit, containing implementer model, session, commit and branch, reviewer model and session, findings, and the verification evidence the reviewer reported (not re-run by the publisher). The review job and reviewer have no merge, approval or branch write path; its App token is `pull-requests: write` only.

Staleness. The review marker carries the reviewed commit and must match GitHub's own `commit_id` for the review. `acceptance_status` returns `CURRENT` only when the newest OpenCode review covers exactly the PR head, otherwise `STALE`, and never `accepted`. When a new OpenCode implementation commit lands on a PR that has an earlier OpenCode review, the publisher's comment says the review is stale. A commit by anyone else needs a new review request; a review cannot be dismissed through the API because it is a COMMENT review.

Confidentiality. The reviewer sees the sanitized copy plus `REVIEW/diff.patch`. The review is blocked if the diff touches credential-like or agent-configuration files, contains secret-like text, exceeds 1.5 MB, or if the PR head is not the authorized commit.

External acceptance by Codex or Jules is unchanged and still required before merge.

## Model eligibility

Registry: `.github/opencode/model-registry.json`. The router hard-codes the six approved identifiers; a registry entry outside them, or without the `-free` suffix, fails validation. A model is selected only if all of these hold:

| Requirement | Source |
| --- | --- |
| Available | listed by the live catalog at run time |
| Terms verified | `terms_verified: true` and a complete `terms_review` record (reviewer, date, source) |
| Approved for repository code | `repo_code_approved: true`, set by the owner |
| Operationally healthy | `operational_status: "healthy"`, set by the owner; no automated health probe exists |

All six models ship with every requirement unmet except catalog presence, so the router stops before any model call until the owner enables models. A free price does not enable a model, including Nemotron. There is no paid fallback: a chain has at most three registered free entries and the run stops if none qualifies.

Catalog: all six IDs appear under provider `opencode` at zero cost in OpenCode's bundled model snapshot (anomalyco/opencode `50c5fce`, 2026-10-20), served from `https://opencode.ai/inference/openai/v1`. The workflow lists models from `.../inference/openai/v1/models` first and falls back to `/zen/v1/models`, which OpenCode's gateway still routes (`inference-proxy.ts`). Neither endpoint has been called with a real key.

Routing categories, chains and rules are unchanged from v2.0 of this document and live in the registry. Routing never changes GitHub permissions.

## Target binding and publication

Before any write the publisher does the following, using only gate values:

1. `GENERATION_STATUS == success`.
2. `AUTHORIZED_SHA` is a full SHA and equals the manifest's `base_sha`.
3. The repository's default branch matches the event and is protected.
4. Issue target: open, number matches, not a PR, and the baseline is reachable from the default branch.
5. PR target: number matches; open and unmerged; base and head both in this repo; head branch equals the gate's approved branch; head SHA equals the baseline; branch tip equals the baseline; branch not protected and not the default branch.

Then:

- New work: create `opencode/issue<N>-<run>-<attempt>` at the baseline, commit with `createCommitOnBranch` (`expectedHeadOid` is the baseline), verify, open a Draft PR. A failed check deletes the branch.
- Existing PR: create staging branch `opencode/pr<N>-<run>-<attempt>`, commit there, verify the signature and the App author, then move the PR branch with a non-forced ref update (`PATCH git/refs`, `force: false`). The PR branch is never touched by an unverified commit. A concurrent push makes the update fail and the PR branch stays as it was. No branch is ever rewritten or reset.
- If something durable remains after a failure (undeletable staging branch, a verified commit with no PR), the publisher raises `PartialPublication`, sets `published=partial` with the branch and commit, and the report comment names the branch. Nothing is cleaned up by force.

## Permissions

| Job | Token | Notes |
| --- | --- | --- |
| gate | `contents: read`, `pull-requests: read` | checkout of scripts, PR metadata |
| opencode | `contents: read` | checkout only; the token is not given to the agent, and the agent has no issue or PR write rights |
| publish | `contents: read` plus App token: contents, pull-requests, issues write | repo-scoped, one hour |
| report | no `GITHUB_TOKEN` permissions; App token: issues write only | status comment |

No `workflows`, `administration`, `actions` or `id-token`. The GitHub App cannot be verified from inside Actions to lack ruleset bypass; the owner must check that it is on no bypass list. The publisher confirms only that the default branch reports `protected`.

## GitHub App and signing

App `dcim-opencode-agent`, installed on this repository only, created by the owner. Permissions: Contents write (branches, commits), Pull requests write (Draft PRs), Issues write (status comments), Metadata read; nothing else. The workflow mints a one-hour token limited to this repository with only the scopes each job needs.

Commits go through GraphQL `createCommitOnBranch`; GitHub signs them and shows them as Verified under the App bot. No SSH key exists in this flow and `DCIM_GIT_SIGNING_KEY_B64` is never referenced. Not yet proven: no commit has been created this way. First activation must show Verified before the design counts as accepted.

Rotation: replace `OPENCODE_APP_PRIVATE_KEY` and delete the old App key; rotate `OPENCODE_API_KEY` in the OpenCode console. Do both quarterly and after any unexpected run. Audit trail: commit trailers (`OpenCode-Run`, `-Model`, `-Category`, `Requested-By`), the PR body, and the workflow log.

## Setup the owner performs

1. Environment `opencode-agent` holds only `OPENCODE_API_KEY`. Environment `opencode-publish` holds `OPENCODE_APP_PRIVATE_KEY` and is used by `publish` and `report`. Restrict both to the default branch.
2. Read the OpenCode data terms for each model you want and edit the registry through a normal PR: set `terms_verified`, fill `terms_review`, set `repo_code_approved` and `operational_status`. Review the registry diff like a security change.
3. Variables: `OPENCODE_APP_CLIENT_ID`, `OPENCODE_APP_SLUG`, `OPENCODE_ALLOWED_ACTOR_IDS` (last).
4. Confirm the ruleset requires PRs, status checks and verified signatures, and that the App is not a bypass actor. Put `.github/` and `docs/security/` under CODEOWNERS.

Reviews stay `REVIEW_BLOCKED` until the owner fills the capability matrix. Do not install or activate the production App until the independent review passes and the activation plan is accepted.

## First activation (disposable)

1. Merge only after review and owner approval. Leave the actor variable unset: a comment should show `authorized=false`.
2. Create the App, environments, secrets and variables, with every registry model still disabled. An authorized `/oc test` must stop at routing with the "owner must verify terms" message and a report comment.
3. Enable one model in the registry through a PR, merge, and run `/oc fix a typo in README.md` on a disposable issue. Check, in order: the firewall probe passed, the guard reported `generation=success`, one commit authored by the App bot shows Verified, a Draft PR exists, CI runs on it.
4. Repeat on a disposable PR branch: the PR branch advances by one verified commit, and the staging branch is gone.
5. Provoke failures: an impossible task, a model removed from the catalog, a push to the PR branch during the run. Each must publish nothing.

## Validation status

Offline, local: 182 tests in `tests/deployment` pass (148 in the five OpenCode test files, up from 58). They include reproductions of the three publisher failures from the review (failed generation, unrelated branch with mismatched PR number, forged artifact baseline), the confidentiality cases, guard failure modes (provider error, timeout, crash, cancellation signal, truncation, garbage output), proxy header injection with a stub upstream, router eligibility, and schema attacks. Twenty-four deliberate code mutations of the key checks (publisher binding, guard contract, router eligibility, secret scan, separation, read-only review, staleness) were each caught by a test. `actionlint` 1.7.12 and `shellcheck` 0.11.0 report nothing on the workflows.

Not validated, because it needs a runner, secrets or your App (the review path adds: reviewer output format from a real model, `gh api` pagination and diff media type, and a review run end to end): `useradd`, `iptables` owner rules, the proxy against the real endpoint, OpenCode honoring the managed config path, `OPENCODE_DISABLE_*` flags and permission schema in v1.19.3, `opencode run` exit and event behavior in a real run, the installer's trust (it downloads from `opencode.ai`; the version is pinned, the script is not checksummed), GitHub's signature on API-created commits, CI triggering on bot-created PRs, and ruleset behavior. Any of these failing stops the run without publication; none can publish by failing.

## Remaining risks

- A model that reads a file in the sanitized copy sends it to a free provider whose terms are unverified.
- The secret scan misses unknown formats and low-entropy secrets.
- The route to a published change still depends on a model reading untrusted PR content; a prompt injection inside a file can steer edits. The control files are protected and nothing merges, but a malicious edit to ordinary code could reach a Draft PR and needs human review.
- Operational health is a manual flag.
- A newer authorized comment can replace an older one still queued in the same group.
