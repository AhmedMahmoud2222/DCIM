# OpenCode agent authority (v6.2)

Scope: `.github/workflows/opencode.yml`, `.github/scripts/opencode_{gate,router,catalog,sandbox,fsisolate,proxy,guard,collect,publish,review,verify_signatures}.py`, `.github/opencode/` (registry, reviewer capability matrix, secret-scan allowlist, pinned runtime lockfile), `tests/deployment/test_opencode_*.py`, `tests/deployment/fixtures/opencode_1_19_3/`, `tests/deployment/opencode_validation/` and `.github/workflows/opencode-security-validation.yml`. Delivered through PR #180.

Status: implemented and merged through PR #180 (merge commit `83a83d8`, 2026-10-22); test infrastructure simplified in v6.0 (architecture proposal v1.0 to v1.2 on PR #180). Accepted at exact head `2a1aca3` against the checklist under Validation: CI passed on that SHA, including 38 of 38 required privileged tests with none skipped; validation run 39703229056 caught 84 of 84 mutants; the independent Codex review of that head found no major issues. The [acceptance report](https://github.com/AhmedMahmoud2222/DCIM/pull/180#issuecomment-6330964917) on PR #180 holds the evidence. Not run on GitHub-hosted runners with real secrets. The GitHub App gate and the model eligibility gate stay closed, with every registry model disabled, until the owner completes the setup and first activation below. Earlier heads (`29740c6`, `bc54f2e`) were rejected and are not evidence for this one.

## Review disposition (second review)

| Finding | Disposition | Where |
| --- | --- | --- |
| P1-1 proxy does not bind the model | One proxy per run, bound to the router's model. It validates method, exact path, headers and the JSON body (no duplicate keys, allow-listed fields, `model` equals the bound model) before the real key is attached. It refuses to start for a model that is unapproved, paid, disabled, unverified, unhealthy, or (for the reviewer) one that implemented. Rejections log code, method and a sanitized path only. | `opencode_proxy.py`, `test_opencode_sandbox.py`, `test_opencode_e2e.py` |
| P1-2 reviewer filesystem | `/srv/ocreview` and `src` are root-owned (dirs 0555, files 0444/0555) with a read-only bind mount when the platform allows it. The agent user cannot rename, replace, delete, symlink or write. Effective permissions are checked as the reviewer UID before and after the run through `access(2)` with effective IDs (ownership, modes, ACLs and read-only mounts), plus an ownership-chain check and a byte digest. The preflight rejects any entry the agent UID owns anywhere in the protected tree and on its ancestor path (an owner can `chmod` a `0444` file or `0555` directory and then modify it), any entry not owned by root, hard-linked files, and symlinks. It reads metadata only. It never writes, renames, deletes or links anything under the real trees; destructive operations run only against disposable fixtures in tests, where they confirm that the permission verdicts match what the kernel does. The implementation work tree sits under a root-owned parent for the same reason. | `opencode_fsisolate.py`, `test_opencode_runtime.py` |
| P1-3 transcript validation | Lifecycle validator built from the real 1.19.3 event format: one session, strictly nested steps, matching message ids, ordered timestamps, only `tool-calls` then a final `stop`, no error event, exit 0, no timeout. Real fixtures captured from the 1.19.3 binary. | `opencode_guard.py`, `test_opencode_guard_fixtures.py` |
| P2-1 final authorization | The PR and branch are refetched and every condition rechecked immediately before the update. | `check_pr_target`, `publish` |
| P2-2 concurrency | The PR branch moves only by `git push --force-with-lease=REF:EXPECTED_SHA`, an exact-SHA compare-and-swap that a reset to an ancestor cannot satisfy. The REST ref update is gone. Tested against real bare repositories, including a server-side race. | `git_cas_push`, `test_opencode_runtime.py` |
| P2-3 immutable review inputs | The gate records base and head SHAs. Diff, files and commits come from `compare/BASE...HEAD`. Results are bound to both SHAs and posted marked stale if either moved. Git-quoted paths are decoded. | `prepare_inputs`, `publish_main` |
| P2-4 confidentiality | Whole-file classification with explicit limits: only valid UTF-8 text without NUL bytes, at most 1 MiB per file, 20,000 files and 100 MiB in total reaches the sandbox. Everything else is excluded; limits exceeded stop the run. | `classify_file`, `prepare` |
| P2-5 publication recovery | After an ambiguous GitHub failure the real remote state is queried. Reports say "verified unchanged" only after a successful lookup, otherwise "partial". An evidence file is uploaded for manual recovery. Nothing is force-pushed or rewritten. | `publish`, `write_evidence` |
| Real defects found while capturing fixtures | OpenCode hung offline on external plugin installs (fixed with `--pure`, `OPENCODE_PURE=1`). `OPENCODE_DISABLE_EXTERNAL_SKILLS` does not exist in 1.19.3 and did nothing (removed). OpenCode titles sessions with a different model over a different API unless `small_model` is set (set; the proxy blocks it regardless). A length-truncated run exits 0. | see "Validation" |

## Architecture

```
issue_comment (owner) -> gate ----------------------> opencode (sandboxed generation) --artifact--> publish --> report
                         no secrets                   read-only token, no GitHub write rights      App token  status comment
                         fixes trusted baseline       API key held by a proxy, not the agent       signed commit + Draft PR
```

- `gate` checks the commenter, parses the command exactly, fetches PR metadata and outputs the trusted target: `kind`, `number`, `authorized_sha`, `base_sha` (PRs), `head_ref`. For issues the baseline is the default-branch commit that holds the workflow (`GITHUB_SHA`); for PRs it is the PR head SHA at authorization time.
- `opencode` checks out trusted scripts (workflow commit) and the target (authorized SHA) separately, reads the live model catalog, routes, builds the sandbox, runs the agent through the guard, and collects the result as data.
- `publish` runs on a fresh runner, mints a one-hour App token, publishes, and re-verifies the signature independently.
- `review` / `review_publish` are the read-only reviewer path (see separation).
- `report` posts a plain status comment (issues write only) when nothing was published. Its text is built from fixed strings and sanitized enum values; no model output reaches it.

`issue_comment` is the only trigger. `pull_request_review_comment` runs the workflow file from the PR merge ref, which lets a PR author edit the gate, so it stays unsupported.

## Successful-generation contract

The previous integration ran `dceoy/opencode-action`, which runs its own commit and push and exits nonzero for many reasons. It is gone. OpenCode 1.19.3 is installed with `npm ci --ignore-scripts` from a committed lockfile (`.github/opencode/runtime`, sha512 integrity for every tarball, exact version) instead of a `curl | bash` installer, and `opencode run --pure --format json` runs as the unprivileged user. `run` only produces a session: no commit, push, PR or comment.

`opencode_guard.py` runs as the runner user; the agent cannot change it or its outputs. It accepts a run only when all of these hold:

- exit code 0, no timeout (the exit code alone is not trusted: a truncated run exits 0)
- every line is a JSON event with a known type, one `sessionID` throughout, ordered timestamps, and a `part` whose type, session and `msg_` id match the event
- steps nest strictly (`step_start`, body events, `step_finish` with the same message id); no event outside a step; no step after the final `stop`; no step left open
- every `step_finish` reason is `tool-calls` (and the step ran at least one tool) or the single final `stop` (with no tool call in that step)
- no `error` event anywhere, no tool call left pending
- the transcript ends in a supported termination (`stop`)
- no process of the agent account survives the run (guard v3.1)

After every run, successful or not, the guard kills every process of the agent account (`sudo -n pkill -KILL -u ocagent`) and proves none is left (`sudo -n pgrep -u ocagent` finds nothing), retrying up to ten times. A survivor (`agent_processes_survived`) or a check that cannot run (`agent_process_check_failed:N`) fails the run, so nothing is collected or published. This closes Codex P1 [r4338780471](https://github.com/AhmedMahmoud2222/DCIM/pull/180#discussion_r4338780471): a process an agent tool detached could outlive OpenCode and keep writing to the working tree while the collector read it. Matching by account is safe because the account exists only for the job on a disposable runner. `pgrep` runs through sudo so that a restricted `/proc` cannot hide a survivor. The implementer and the reviewer jobs both run through the guard. A real test starts a detached writer as the agent account, reaps it with the production function and shows the file stops growing.

A valid transcript shows that the run ended normally. It does not show that the change is correct; CI and review decide that.

Evidence: `tests/deployment/fixtures/opencode_1_19_3/` holds eight transcripts captured from the real 1.19.3 binary against a scripted local model server (reproduce with `capture.py`; npm integrity of the binary package is in the lockfile). They cover success (text and a tool-call edit), provider 500 and 401 after retries (exit 1 with an `error` event), a `length` truncation (exit 0, no error), a tool-call step followed by a provider failure, a cut stream that retries until killed (2066 events, first 60 kept), and a hang. Mutations of a real success transcript (foreign session, dropped or duplicated steps, reordered events, trailing events, injected errors, altered reasons) are all rejected.

Limits of that evidence: the model's replies are scripted, not produced by a real provider; a provider that returns `stop` together with tool calls would be rejected (fail closed) and a real provider's other edge cases are unobserved.

## Input boundary

Everything the model can receive:

1. The fixed preface in `opencode_guard.py` plus the authorized command text. The router refuses command text that looks like a credential or mentions credentials, production data, customer data or PII.
2. Files the agent reads inside the sanitized copy of the tracked tree at the authorized SHA (and, for reviews, `REVIEW/diff.patch`).
3. Its own tool output.

No issue body, comment thread or PR metadata is passed.

What reaches the sandbox copy, with explicit limits:

- Excluded: symlinks and non-regular files; `.opencode/`, `opencode.json(c)`, `.claude/`, `.agents/`, `.cursor/`; credential-like names (`.env*` except examples, keys, certificates, databases, dumps, `.npmrc`, kubeconfig and similar).
- Every remaining file is read in full and must be valid UTF-8 text, free of NUL bytes, and at most 1 MiB; otherwise it is excluded and recorded with its reason. There is no scan window: content after any offset or after early NUL bytes is either scanned or never exposed. Tests cover a secret at 2.5 MiB (file excluded), a secret near the 1 MiB limit (run stops), and secrets after NUL bytes (file excluded).
- A secret-shaped match (private-key blocks, GitHub, AWS, OpenAI-style, Slack, Google, Stripe, GitLab, npm, Azure storage, JWT) stops the run before any model call and names paths, never content. Patterns match without a leading word boundary, so a token glued to preceding text is still found.
- More than 20,000 files or 100 MiB stops the run: input that cannot be bounded is not sent.
- `.github/opencode/secret-scan-allowlist.txt` lists owner-approved fixtures. A test requires the repository itself to pass the scan, so a secret-shaped literal added to any tracked file fails CI instead of silently blocking every run.

Residual: the scan recognises listed formats only; a low-entropy or unlisted secret in an included text file is still exposed. Files excluded as binary or oversized cannot be reviewed or edited by the agent.

Execution isolation (not only OpenCode permissions):

- Dedicated user `ocagent`; the runner home is `chmod 700`, so the checkout, tokens and runner files are unreadable to it.
- `env -i` with a fixed environment: no `GITHUB_*`, runner variables or keys. The flags used exist in 1.19.3 (`OPENCODE_PURE`, `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_CONFIG_CONTENT`).
- Firewall rules for `ocagent`: loopback only, IPv4 and IPv6, probed before the agent starts.
- The OpenCode key is held by `opencode_proxy.py` (runner user). The agent reaches it over loopback with a placeholder.
- Managed config in `/etc/opencode/opencode.json` plus `OPENCODE_CONFIG_CONTENT`. With the real 1.19.3 binary, a hostile project `opencode.json` that redirected the provider to a dead port and allowed edits lost to the managed file (manual experiment; not an automated test, since it writes `/etc`). The repo's own config is also not copied.
- Collection runs against a runner-owned baseline git dir with the sandbox as a bare work tree; a test plants a `core.fsmonitor` hook and checks it never runs. The collector also refuses a work tree that is a symlink or has an agent-controlled ancestor.

### Proxy model binding

Rules enforced by the proxy, not by the provider: `POST` only to `/inference/openai/v1/chat/completions`; `GET` only to the two model-list paths; no query string, fragment, encoded slash, `..` or `//`; no `Transfer-Encoding` or `Content-Encoding`; `Content-Type: application/json`; the body must parse as a JSON object with no duplicate keys (a duplicated `model` is the classic smuggling route), no `NaN`, only allow-listed top-level fields (routing fields such as `models`, `provider`, `route`, `fallbacks` are not on the list), a non-empty `messages` list, and `model` exactly equal to the bound bare id. The original bytes are forwarded unchanged only after validation.

Found with the real binary: without `small_model`, OpenCode asks `gpt-5.4-nano` over `/responses` to title the session. The config now sets `small_model` to the bound model, and the proxy rejects the substitute anyway (the end-to-end test removes `small_model` and shows the rejection and a still-successful run). A shell command, CLI flag or config edit naming another model reaches the proxy and is refused; a provider URL pointing elsewhere has no route because of the firewall.

## Implementer-reviewer separation

Rule IMPLEMENTER_REVIEWER_SEPARATION applies when OpenCode implements and reviews on the same branch or PR. It is configured by `agent_separation` in the model registry. Every field is enforced in code and the registry may not weaken any of them: a missing, extended or altered policy blocks all routing, including implementation routing.

| Field | Enforced by |
| --- | --- |
| `enabled`, `scope: same_branch_or_pr` | implementer records are read from the PR being reviewed |
| `require_distinct_models` | `check_separation`; the selector also excludes every model that implemented on the PR |
| `require_distinct_sessions` | `check_separation`; the guard records the OpenCode session id, a missing id fails the run |
| `review_read_only` | review config denies edit tools; the source lives under a root-owned parent and tree (read-only mount when available); effective write, rename, replace and symlink permissions are checked non-destructively as the reviewer UID before and after the run; the tree digest and `verify-readonly` must match |
| `reviewer_selection: best_verified_capability` | `select_reviewer` ranks only on recorded evaluation evidence |
| `block_if_no_eligible_reviewer` | no eligible reviewer produces `REVIEW_BLOCKED` and a PR comment |
| `external_acceptance_required` | every review states it is not an approval; `acceptance_status` never returns accepted |

Flow. `/oc <task>` implements (existing path). The publisher writes the implementer into the signed commit as trailers: `OpenCode-Model`, `OpenCode-Session`, `OpenCode-Run`, `OpenCode-Mode: implement`, and posts a comment naming implementer model, session, branch and commit. `/oc review [focus]`, on a PR only, starts the review path: the gate fixes the PR head as the reviewed commit; the review job reads implementer records from GitHub-verified commits authored by the App bot (forged trailers on other commits are ignored), selects a reviewer, and runs it as a new OpenCode session in the same loopback-only sandbox with all edit tools denied. Chaining implementation into review automatically is not done: the owner issues both commands.

Reviewer choice. A model qualifies only if it passes the implementation requirements (terms verified, repository code approved, healthy, in the live catalog), did not implement on this PR, and has a complete evaluation record in `.github/opencode/reviewer-capability-matrix.json`. Among those the highest weighted score wins; ties go to the stronger weakest dimension, then the model id. Weights follow the requested priorities: correctness 0.25, security 0.25, database and transaction safety 0.15, test quality and regression detection 0.15, cross-module dependencies 0.10, complex-change inspection 0.10. A record needs suite, suite version, date, an https run URL, a results hash and a score for every dimension, with each score at least `minimum_dimension_score` (0.5). Speed, latency and price are not inputs, and extra fields invalidate a record. The same provider can supply both models; only the model id and session matter.

No model has evaluation evidence yet, so the matrix ships empty and every review ends in `REVIEW_BLOCKED`. I have not run an evaluation and the matrix does not claim any model is better. Evaluation protocol for the owner: build a seeded-defect corpus (changes with known planted defects across the six dimensions, kept out of the repo's production paths), run each candidate through the same sandbox in review mode, score the fraction of planted defects found with the right severity and file, store the raw results, and enter the scores with the results hash and run URL through a normal PR.

Review output. The reviewer ends with one JSON block. The trusted publisher validates it (severity, affected files, evidence required for critical and high, recommendation), strips mentions, hidden markers and slash commands, and recomputes the verdict: any critical or high finding makes it `BLOCKING_FINDINGS` whatever the model claimed; an approval-style verdict is rejected. It posts one PR review with event `COMMENT` and `commit_id` set to the reviewed commit, containing implementer model, session, commit and branch, reviewer model and session, findings, and the verification evidence the reviewer reported (not re-run by the publisher). The review job and reviewer have no merge, approval or branch write path; its App token is `pull-requests: write` only.

Staleness. The review marker carries the reviewed head and base commits and must match GitHub's own `commit_id` for the review. `acceptance_status` returns `CURRENT` only when the newest OpenCode review covers exactly the PR head and base and was not stale on arrival, otherwise `STALE`, and never `accepted`. When a new OpenCode implementation commit lands on a PR that has an earlier OpenCode review, the publisher's comment says the review is stale. A commit by anyone else needs a new review request; a review cannot be dismissed through the API because it is a COMMENT review.

Confidentiality. The reviewer sees the sanitized copy plus `REVIEW/diff.patch`, taken from `compare/BASE...HEAD` at the SHAs the gate recorded (not from the live PR). The review is blocked if the diff touches credential-like or agent-configuration files, contains secret-like text, exceeds 1.5 MB, names a path GitHub does not list for those revisions, or the file list may be truncated (300 files). Git-quoted paths are decoded before the checks. At publication the PR is refetched: if its head or base moved, or it closed, the review is still posted for the revisions it examined but marked `STALE ON ARRIVAL`, and `acceptance_status` never reports it as current.

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

Routing categories, chains and rules live in the registry. Routing never changes GitHub permissions.

## Target binding and publication

Before any write the publisher does the following, using only gate values:

1. `GENERATION_STATUS == success`.
2. `AUTHORIZED_SHA` is a full SHA and equals the manifest's `base_sha`.
3. The repository's default branch matches the event and is protected.
4. Issue target: open, number matches, not a PR, and the baseline is reachable from the default branch.
5. PR target (`check_pr_target`): number matches; open and unmerged; base and head both in this repo; head branch equals the gate's approved branch; head SHA equals the baseline; branch tip equals the baseline; branch not protected and not the default branch.

Then:

- New work: create `opencode/issue<N>-<run>-<attempt>` at the baseline, commit with `createCommitOnBranch` (`expectedHeadOid` is the baseline), verify, open a Draft PR. A failed check deletes the branch.
- Existing PR: create staging branch `opencode/pr<N>-<run>-<attempt>`, commit there, verify the signature and the App author. Immediately before the update, `check_pr_target` runs again against freshly fetched data and GitHub's compare data must show the verified commit descends from the baseline. The PR branch then moves with `git push --force-with-lease=refs/heads/<branch>:<baseline>`: it succeeds only if the remote branch is still exactly the baseline. `--force-with-lease` is used for its exact-SHA check, and the descent check above makes the update a fast-forward; it is never an unconditional force. A concurrent push, a force-push, a reset to an ancestor or a deleted branch makes it fail and leaves their state in place. The earlier non-forced REST update could not give this: a reset to an ancestor leaves the new commit a valid fast-forward, so it would have silently undone the reset.
- Evidence: tests push to real bare repositories and cover the unchanged branch, a concurrent descendant push, a reset to an ancestor (shown to be a fast-forward that the lease still refuses), a reset to an unrelated root commit, a deleted branch, and a pre-receive hook that resets the branch after the client check (server-side refusal). The full publish flow is also exercised against a real repository with those transitions injected between verification and the update.

After an ambiguous GitHub failure (network error, timeout, 5xx) the publisher queries the real remote state before saying anything: branch creation, the commit mutation, the update and the staging deletion each have a lookup. A report says the PR branch is "still at the authorized commit" only when a lookup showed it; otherwise it raises `PartialPublication` ("could not be confirmed as unchanged"). The facts (trusted baseline, observed tips, staging branch, any lookup error) go to `publication-evidence.json`, uploaded even on failure, for manual recovery. If the update reported failure but the branch is at the new commit, that is recorded as a warning. Nothing is force-pushed, reset or rewritten automatically.

## Permissions

| Job | Token | Notes |
| --- | --- | --- |
| gate | `contents: read`, `pull-requests: read` | checkout of scripts, PR metadata |
| opencode | `contents: read` | checkout only; the token is not given to the agent, and the agent has no issue or PR write rights |
| publish | `contents: read` plus App token: contents, pull-requests, issues write | repo-scoped, one hour; the token reaches `git` through environment config, never argv or logs |
| review | `contents: read`, `pull-requests: read` | reads compare data before the sandbox starts; the token is not in the agent's environment |
| review_publish | `contents: read` plus App token: pull-requests write only | posts one `COMMENT` review |
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

## Validation

The test infrastructure follows architecture proposal v1.0 with addendum v1.1 and the corrections recorded in v1.2 (PR #180). The production controls above are unchanged except that the publisher also protects `tests/deployment/opencode_validation/`.

### Where tests run

- **Developer hosts.** `python3 -m unittest discover -s tests/deployment -p 'test_*.py'` runs every unprivileged test. Privileged tests (another UID, root-owned trees, real mounts) are skipped because `OPENCODE_DISPOSABLE_HOST` is unset.
- **Required CI** (`deploy-tools-test.yml`, job `Deployment tooling regressions`). This runs on a GitHub-hosted `ubuntu-24.04` VM, which GitHub discards after the job.
  - The job asserts `runner.environment == github-hosted` and passwordless sudo, installs `acl` and the pinned OpenCode binary, and runs every deployment test with `OPENCODE_DISPOSABLE_HOST=1`.
  - `opencode_validation/suite.py` records one outcome per test ID. `suite.py check-ci` fails the job on any infrastructure error (import, `setUpClass`, a test discovered but never reported), on any failure, and on any skip that is not listed in `allowed_skips.txt` (empty).
  - It also fails when any ID in `privileged_required.txt` is missing or did not pass. The log ends with `privileged required: N/N passed, 0 skipped` and the manifest's SHA-256.
- **Privileged fixtures.** These live in root-owned directories under `OPENCODE_DISPOSABLE_ROOT` and are never deleted or unmounted by a test; the VM teardown removes them.
  - The suite creates the `ocprobe` account once per host and refuses an account that already existed.
  - Unprivileged fixtures use `tempfile.TemporaryDirectory`, whose removal errors surface.
  - With `OPENCODE_DISPOSABLE_HOST=1` but no root or sudo, the runtime module fails to import instead of skipping.

### Mutation validation (`opencode-security-validation.yml`)

**Start and authorization.**
- The repository owner applies the `security-validation` label to the PR (`pull_request: labeled`, before merge), or dispatches the workflow once it exists on the default branch.
- The first job refuses any sender other than `github.repository_owner` and any runner that is not GitHub-hosted, before the matrix is scheduled. Applying a label is not taken as proof of write access.
- Only the `labeled` event starts a run, so a label left on the PR never covers a later head. To re-run, remove and re-apply the label. `pull_request_target` is never used.

**Isolation.**
- Each job checks out `github.event.pull_request.head.sha`, the exact head at label time, not GitHub's merge commit.
- The control run and each of the 84 mutants run in separate fresh `ubuntu-24.04` VMs, with no job containers and no caches. Mutant jobs start only after the control passes.
- **Control run:** the suite (all `test_opencode_*.py`, privileged and end-to-end included, nothing skipped) must pass completely on a disposable host. Otherwise there is no verdict.
- **Mutant job steps:**
  1. Apply one inventory entry (`mutants.py apply`). The snippet must occur once, the result must compile, and the change must be exactly the intended one.
  2. Run the same suite under a time limit.
  3. Judge the result against the control.
  4. Upload one verdict.

**Verdicts.**

| Verdict | Meaning |
|---|---|
| `CAUGHT` | the suite completed, discovered exactly the control's test IDs, had no infrastructure error and no skip, and at least one named test failed inside its own body |
| `MISSED` | every test passed under the mutation |
| `INVALID` | the mutant could not be applied, the set of test IDs changed, or a test was skipped or had an expected-failure outcome |
| `ERROR` | no usable report, an infrastructure error, or a failure outside a test body (setUp, setUpClass, import) |
| `TIMEOUT` | the suite hit the time limit |

**Aggregate.**
- A final job, which runs no mutated code, reads the verdicts as data.
- It requires a passed control and exactly the inventory's ID set: no missing, duplicate, extra or malformed results.
- It requires one run identity across all results: PR, head, base, run ID, run attempt, and the hashes of the inventory, both manifests and all validator files including the workflow.
- It passes only with 84 of 84 `CAUGHT`. Results are bound to that head; any later push makes them stale.

**Credentials.**
- Both test workflows declare `permissions: {}` at the top and `contents: read` per job. They use no secrets, OIDC, environments or write permissions, and every checkout sets `persist-credentials: false` (enforced by `test_opencode_validation.py`).
- Root code inside a job can still reach that job's read-only token and its artifact upload token. A deliberately malicious mutant could therefore forge its own verdict; the aggregate's identity checks do not make a compromised job's result cryptographically independent. The mutants are reviewed snippets in the inventory, not attacker input, so this residual risk is accepted.

### What was removed in v6.0, and what keeps the coverage

| Removed | Why | Coverage kept by |
|---|---|---|
| `strict_cleanup.py`, `test_strict_cleanup.py` (openat2/statx deletion, mountinfo parser, `/proc` scanner, root helper) | host-safety code for deleting what mutated code touched on a long-lived host; the disposable VM replaces it | nothing to keep: it tested only itself (37 harness mutants retired) |
| `account_lease.py`, `test_account_lifecycle.py` | shared test-account leasing across parallel suites on one host | `create_agent_account()` on a disposable host (11 harness mutants retired) |
| `mutation_check.py`, `ns_run.py` (namespaced root runner, process termination) | one VM per mutant replaces namespaces, cleanup and process management | `opencode_validation/`; the 78 production mutants carried over unchanged, 6 guard mutants added in v6.1 (32 harness mutants retired) |
| mount-probe, mountinfo-decoding, temp-dir and cleanup-guarantee test classes in `test_opencode_runtime.py` | they tested the removed helpers | `MountHelperTests` (production `mount_readonly` with a recording runner) and `RealMountTests` (real bind, remount, failure and CLI on a disposable host) |
| 8 harness mutants of runtime and sandbox test helpers | their targets are gone | nothing to keep |

All production assertions in `RuntimeFilesystemTests` (ownership, chmod, ACL, hard links, symlinks, modify-and-restore, non-destructive preflight on populated trees, destructive-probe refusal, the collector), the real-git publication and concurrency tests, proxy, end-to-end, guard fixtures, gate, router, review and publish tests are kept unchanged. The previous 166-entry local result is retired; acceptance uses only the exact-head GitHub run.

### Complexity budget

The test tree contains no custom host-safety subsystem: no process scanning or signalling, no namespaces, no account leasing, no custom recursive deletion and no `ctypes`. `ComplexityBudgetTests` enforces this with import, call and command-reference checks. Short calls to standard utilities (`useradd`, `setfacl`, `mount` through the production `fsisolate`, `findmnt`) remain. Privileged custom code exists only in production: `opencode_fsisolate.py` (scope frozen: `build`, `chain`, `probe`, `probe-parent`, `digest`, `mount`) and the root steps of `opencode.yml`. Any new privileged component needs owner approval.

### Acceptance checklist (finite)

Acceptance of an exact head requires every item below, with GitHub evidence on that SHA. Local runs are supporting evidence only. A demonstrated production security defect, or a defect that invalidates the validator's result, blocks acceptance even if every item holds.

1. Every result names the exact head SHA; nothing is inherited from an older head or from `main`.
2. Required CI is green on that SHA, and the branch has no conflict with its base.
3. `Deployment tooling regressions` exists on the SHA with conclusion `success`; its log shows `privileged required: N/N passed, 0 skipped`, the manifest hash and a GitHub-hosted runner. A missing check is not acceptance.
4. Only eligible registry models run; others fail closed.
5. The proxy rejects model substitution, unknown endpoints and redirected providers.
6. Implementer and reviewer differ in model, session and identity; reuse fails closed.
7. The reviewer cannot modify its source: root-owned tree, no agent-owned entry or ancestor, digest unchanged, and the read-only mount when available.
8. The agent UID has no network except the local proxy, and never sees publication credentials.
9. Inputs respect size, encoding, NUL and secret-scan limits.
10. The guard rejects every failing transcript fixture and accepts only the success fixtures.
11. Publication refuses a forged baseline, a non-descending head, stale reviews and unsigned commits, posts COMMENT reviews only, and records ambiguous outcomes.
12. Only an authorized commenter triggers the gate; per-job least privilege; actions pinned to full SHAs.
13. One owner-requested validation run whose recorded head equals the accepted head: control passed, the aggregate shows every inventory mutant (84 at v6.1) `CAUGHT` from separate jobs, and the validator and inventory hashes match the reviewed versions.
14. `tests/` contains no custom host-safety subsystem (`ComplexityBudgetTests`).
15. The change set contains no merge, deployment, OpenCode activation or credential provisioning.
16. Every `actions/checkout` in the OpenCode and test workflows sets `persist-credentials: false`; no test or validation job has a write permission, OIDC or a secret.

Items 4 to 12 are each shown by named negative tests in `test_opencode_*.py`, which the CI run executes. All six models stay disabled, and the capability matrix stays unqualified, until the owner approves them. App provisioning, real provider and App integration, and activation are separate operational gates.

### Residual risks (test infrastructure)

- Privileged behaviour is proven only on GitHub-hosted Ubuntu runners, not on developer machines.
- Mutation evidence is produced on request, not per push; regression tests run on every push.
- GitHub's per-job VM isolation is trusted for tests, as production already trusts it; self-hosted runners are refused.
- The validator itself is not mutation-tested; `test_opencode_validation.py` tests its logic directly.

## Remaining risks

- A model that reads an included file sends it to a free provider whose terms are unverified. The secret scan covers listed formats only.
- A prompt injection inside an included file can steer edits. Control files are protected and nothing merges, but ordinary code could reach a Draft PR and needs human review.
- The proxy allow-list is strict by design: if OpenCode or a provider sends a field or endpoint not on the list (for example the Responses API for some model), the run fails closed.
- OpenCode tries to install its plugin package on start; offline that fails after retries (about 24 seconds here with DNS retries, faster with the workflow's REJECT firewall). It does not block the model call.
- The read-only bind mount may be unavailable on a runner; the permission model and runtime probe remain the gate.
- The lease push relies on git's old-value check being honored by GitHub's receive-pack; local bare repositories confirm the client and server logic, GitHub itself is untested.
- Free-model health is a manual flag; availability can change without notice.
- A newer authorized comment can replace an older one still queued in the same concurrency group.
