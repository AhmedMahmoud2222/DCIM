# OpenCode agent authority (v1.0)

Scope: `.github/workflows/opencode.yml`, `.github/scripts/opencode_gate.py`, `.github/scripts/opencode_verify_signatures.py`, `tests/deployment/test_opencode_workflow.py`. Delivered through PR #180.

Status: implemented and unit-validated offline. Not accepted. Live validation and the independent Codex review are open (see "Open items").

## Live-state findings (main 92be692, PR head e347ad5)

- The PR added one file with a `contains(body, '/oc')` trigger. Any commenter, including an anonymous user on a public repo, started a job with `contents: write`, `id-token: write` and `OPENCODE_API_KEY` in scope.
- The model line has no trailing quote on the PR head. `opencode-go/kimi-k3` matches the example in the pinned action's README. The provider catalog was not queried, so model availability is unconfirmed.
- The action is the third-party `dceoy/opencode-action` (pinned to 163f9a9, "v0.8.1"), not OpenCode's own action. Its input names (`model`, `prompt`, `use-github-token`, `timeout-minutes`) were checked against its `action.yml` at that SHA.
- With the default `use-github-token: false`, the action exchanges the OIDC token with `api.opencode.ai` for a token from an OpenCode GitHub App. That sends repo write authority through a third-party service and needs `id-token: write`. The new workflow sets `use-github-token: 'true'`.
- This repo has no Claude Code or Codex workflow. Both run outside Actions: Claude Code in the DCIM Cloud environment with SSH commit signing, Codex as an external reviewer. Parity is therefore a capability comparison, not a YAML comparison.
- Branch protection, rulesets and signing rules could not be read with the tools in this session. Their state is assumed from the task statement and must be confirmed by the owner.

## Authorization design

Two jobs. The first holds no secret; the second runs only if the first passes.

1. `gate` (permissions `contents: read`, `pull-requests: read`) checks out `.github/scripts` from the default branch and runs `opencode_gate.py`. The script reads the webhook payload file and, for PR conversation comments, the PR object fetched from the API. Comment text is never placed in shell.
2. `opencode` (`needs: gate`, `if: authorized == 'true'`, `environment: opencode-agent`) holds `OPENCODE_API_KEY` as an environment secret.

Gate rules (all must hold; any parse error fails closed):

- Event is `issue_comment` or `pull_request_review_comment`, action `created`, from this repository.
- `comment.user.type == User` and `comment.user.id` is in repo variable `OPENCODE_ALLOWED_ACTOR_IDS` (numeric IDs, comma separated; empty or malformed means deny all). IDs are immutable, unlike logins.
- `comment.author_association` is OWNER, MEMBER or COLLABORATOR.
- Body matches `^(/oc|/opencode)\s+<task>$` at the very start, case-sensitive. Quoted replies, code blocks, mid-text mentions, `/ocean` and bare `/oc` do not match. Tasks starting with `/` (slash-command expansion) or over 8000 characters are rejected.
- Pull request targets must be open with a head branch in this repository. Fork PRs are rejected.
- The action receives the gate's extracted task as a fixed `prompt`, so it does not re-parse the comment.

Prompt injection: the owner-approved author is the only source of the task text. The agent can still read PR and issue content that others wrote, so injected text in those files can steer it. Controls: the gate rejects fork PRs, the token cannot touch workflows, protected branches and signing rules stay enforced server-side, and the job has no deployment credentials. Residual risk is listed below.

Workflow changes cannot silently expand privileges: the `issue_comment` and review-comment triggers always run the workflow file from the default branch, and the gate script and checkout come from the default branch. A PR that edits `opencode.yml` changes nothing until the owner merges it. `tests/deployment/test_opencode_workflow.py` fails CI if permissions widen, `id-token` returns, a second secret appears, an action loses its SHA pin, or a loose `contains()` trigger comes back.

## Token permissions

| Scope | Level | Why |
| --- | --- | --- |
| Workflow default | none | `permissions: {}`; each job opts in |
| gate: contents | read | check out the gate script |
| gate: pull-requests | read | fetch the PR head repo, state and ref |
| agent: contents | write | create task branches and push commits |
| agent: pull-requests | write | open and update Draft PRs, post review findings |
| agent: issues | write | status comments on issues and PR threads |
| id-token | not granted | avoids the OpenCode App token exchange |

`GITHUB_TOKEN` is enough for the Actions-hosted path and cannot be granted `workflows`, so the agent cannot edit workflow files. Two limits apply to a `GITHUB_TOKEN`: pushes it makes do not trigger `pull_request` CI, and git pushes from the runner are unsigned. If the owner wants CI to run on agent pushes, or wants agent-published commits without a manual step, the minimum fix is a repository-scoped GitHub App installation token (Contents, Pull requests, Issues write; no Workflows, Administration or Actions) stored in the `opencode-agent` environment. That needs an owner decision. No personal access token is used or proposed.

## Commit signing

Equivalent signing is not established in Actions. The DCIM Cloud environment signs with an SSH key that must not be copied into GitHub secrets without an explicit owner decision, and this PR does not do that. What the workflow does:

- A final step (`if: always()`) compares every branch the agent can publish (the PR head branch, plus `opencode/*`) against the default branch and fails if any commit lacks `verification.verified == true`.
- If the repository ruleset requires signed commits, an unsigned push from the runner is rejected by GitHub. The agent then reports failure instead of bypassing the rule.

Approved alternatives for the owner to choose from: (a) an Actions step that publishes the agent's tree through the GraphQL `createCommitOnBranch` mutation with a GitHub App token, which GitHub signs; (b) a dedicated SSH signing key stored as an environment secret with a deploy-key-scoped identity registered as a signing key; (c) keep the agent on local or Cloud execution where signing already works and use the workflow for review and analysis only.

## Capability comparison

| Capability | Claude Code (DCIM Cloud) | Codex | OpenCode (this workflow) |
| --- | --- | --- | --- |
| Read code, analyze | yes | yes | yes |
| Backend, frontend, migrations | yes | yes | yes, limited by runner tooling: no database service, Node and Python must be installed by the prompt or a setup step (open item) |
| Run tests | yes | yes | yes, in the runner |
| Create branches | yes | yes | yes (`opencode/*`, PR head) |
| Draft PRs | yes | yes | yes, via `pull-requests: write` |
| Review PRs | yes | yes | yes |
| Verified signed commits | yes (SSH) | per environment | blocked pending owner choice above |
| Trigger CI on its pushes | yes | yes | no with `GITHUB_TOKEN` |
| Merge, deploy, edit workflows | no | no | no (no `workflows`, no deploy credentials, branch rules apply) |

## Validation evidence

Offline, in `tests/deployment/test_opencode_workflow.py` (run by `Deployment Tools Test`): 19 tests passed locally, covering authorized owner success, unauthorized rejection (wrong ID, wrong association, Bot, claimed authority in text), empty allowlist, no-command and embedded-command bodies, review comments, fork and closed PRs, repository mismatch, secrets absent from the gate job and from unauthorized runs, pinning, `persist-credentials: false`, minimal permissions and the signature verifier. The full `tests/deployment` suite (53 tests) passes.

## Open items

Not validated, because they need secrets, repository settings or a live run:

1. Agent reads and modifies code, creates a Draft PR (disposable branch, no merge).
2. Signing enforcement against the real ruleset.
3. Branch protection and merge restrictions as configured on GitHub.
4. Model `opencode-go/kimi-k3` availability and `OPENCODE_API_KEY` validity.
5. Independent Codex security review against the final PR head.

Owner actions required: create the `opencode-agent` environment (add `OPENCODE_API_KEY` there, require reviewer approval if wanted), set variable `OPENCODE_ALLOWED_ACTOR_IDS`, and confirm the ruleset requires signed commits, pull requests and status checks.

## Remaining risks

- A GitHub-hosted runner holds `contents: write` while the model reads untrusted issue or PR text.
- A non-owner comment on the same issue can queue a run that replaces an owner's pending run in the concurrency group (the gate then rejects it, and the owner re-posts).
- The third-party action downloads OpenCode from `opencode.ai` at run time.
- Without signing support the agent cannot publish accepted commits from Actions.
