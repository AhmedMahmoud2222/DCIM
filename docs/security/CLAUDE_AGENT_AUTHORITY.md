# Claude agent authority (workflow v1.0)

`.github/workflows/claude-agent.yml` runs Claude on a pull request when an authorized user
comments `@claude <task>`. It is independent of the OpenCode workflow: separate gate
(`.github/scripts/claude_gate.py`), separate allowlist variable, separate secret and
separate concurrency groups. `/oc` and `/opencode` stay with `opencode.yml`.

## Authorization (job `gate`, no secrets)

A comment runs Claude only when all of these hold:

- Event is `issue_comment` / `created` in this repository (the workflow file always comes from the default branch).
- Commenter is a `User` account whose numeric id is in the repository variable `CLAUDE_ALLOWED_ACTOR_IDS` (comma-separated ids; empty or malformed fails closed) and whose `author_association` is OWNER, MEMBER or COLLABORATOR.
- The comment starts with `@claude` followed by whitespace and a task (case-sensitive, 8000 characters at most). Quotes, code blocks, mid-text mentions, `@claude-x` and `/oc ... @claude` do not match.
- The target is an open pull request whose head and base are both in this repository, with a valid head SHA and branch name.
- The PR base is the approved default branch `main`, the repository default branch is still `main`, and the branches API reports `main` as protected (see Trusted configuration).
- No earlier run has claimed the comment. The gate adds an `eyes` reaction as the claim; a second delivery of the same comment sees it and stops. Gate runs for one comment are serialized.

The `claude` job re-verifies the live PR, base protection and head branch twice before Claude starts (once before and once after checking out the PR head), then again before every write (see Write boundary).

## Execution (job `claude`)

- `anthropics/claude-code-action` v1.0.262 pinned to commit `925afb53776cf1a2b738c6bdbed37b4d4000ef13`.
- Commits go through GitHub's API (`use_commit_signing: true`), so GitHub signs them, onto the existing PR head branch only. A step fails the job if the action reports another branch.
- Tools: the action's tag-mode defaults plus `--disallowedTools` for `gh`, `git push`, `git remote`, `curl`, `wget`, web fetch and web search. No merge, close, deploy or PR-creation tool is available.
- Permissions: `contents: write`, `pull-requests: write`, `issues: write`, `actions: read`. No `id-token`, so the Claude GitHub App token exchange is not used; the action uses `github.token`.
- One run per PR at a time, queued and never cancelled mid-edit.

## Trusted configuration (P1)

The pinned action restores `.claude/`, `.mcp.json` and related startup files from the PR **base** branch, because the CLI executes hooks and MCP servers from them before any tool-permission check. That is only safe if the base is reviewed code, so:

- Only `main` is an acceptable base. The gate rejects any other base ref, a default branch other than `main`, and a base that the branches API does not report as `protected: true`. The same check is repeated live by the write guard.
- The PR head is never a source of executable configuration: the action overwrites its `.claude/` and `.mcp.json` from `main`, and the hooks the workflow adds come from the workflow commit (below), not the checkout.
- `main` protection is therefore part of the trust boundary. Its bypass actors can change what Claude executes; review that list when the ruleset changes.

## Write boundary (P2)

Preflight checks alone are not enough, because the PR can close, merge, move HEAD or lose its branch while Claude runs. Writes are enforced at the point they happen:

- `.github/scripts/claude_write_guard.py` is installed from the default-branch workflow commit into `$RUNNER_TEMP` before the PR head is checked out, so a PR cannot alter it.
- The workflow passes user-level Claude settings with a `PreToolUse` hook on every `mcp__github_file_ops__*` tool, the only path by which this workflow writes to GitHub (`git push` and `git remote` are denied and credentials are not persisted). Before each commit the hook re-reads the live PR and blocks (exit code 2) unless: it is open and unmerged; head and base are in this repository; the head branch name is unchanged; the base is the protected `main`; the PR head and the head ref both equal the commit the guard expects; and the head ref exists. Any API error, timeout or malformed input blocks.
- The guard never creates or moves a ref. If the head branch was deleted (for example by auto-delete on merge) the file-ops server would otherwise recreate it from the base; the hook blocks that write.
- A `PostToolUse` hook advances the expected head only when the new head is a direct child of the previous one. Any other head change locks all later writes, and the final `verify` step fails the job.
- `branch_prefix` is deliberately an invalid ref name. If the PR closes before the action starts, its closed-PR fallback would create a `claude/pr-N-...` branch; the action's own name validation rejects the prefixed name and aborts before anything is created.
- Token scopes are unchanged; the guard reads PR, branch and ref state only.

Residual risk: GitHub offers no atomic compare-and-write across the PR API and the Git data API, so a state change in the milliseconds between the hook's last read and the commit request cannot be excluded from inside this workflow. The hook narrows the window to one tool call and the commit still lands only on the existing PR head branch with signed provenance. Fully closing it needs a repository ruleset restricting branch creation to humans, which is an owner decision outside this PR.

## Setup (owner)

1. Create the repository variable `CLAUDE_ALLOWED_ACTOR_IDS` with the numeric ids of approved users.
2. Create the repository secret `CLAUDE_CODE_OAUTH_TOKEN`. Run `claude setup-token` locally with the Claude subscription account to generate it. This uses the existing subscription; no API key is needed.
3. Merge the workflow to the default branch. `issue_comment` workflows run only from there.

## Known limits

- Commits pushed with `github.token` do not start other workflows, so CI does not re-run on Claude's commits until someone pushes or re-runs checks. Branch protection still gates merge.
- Re-running a finished run is rejected as a duplicate. Post a new `@claude` comment instead.
- Live execution depends on the secret and variable above; offline tests cannot prove it.

## Tests

`tests/deployment/test_claude_agent.py` covers authorization, command parsing, routing against the OpenCode gate, duplicate claims, workflow policy and isolation.

`tests/deployment/test_claude_write_boundary.py` covers untrusted base configuration, closed and merged PR races, HEAD changes, deleted or renamed head branches (unauthorized branch creation), guard failures that must fail closed, the hook exit-code contract against a scripted GitHub API, and the workflow ordering that keeps the guard outside the PR checkout.
