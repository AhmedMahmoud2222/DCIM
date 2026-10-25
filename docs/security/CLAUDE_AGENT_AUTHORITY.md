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
- No earlier run has claimed the comment. The gate adds an `eyes` reaction as the claim; a second delivery of the same comment sees it and stops. Gate runs for one comment are serialized.

The `claude` job re-reads the PR before it starts and stops if the head SHA moved or the PR closed.

## Execution (job `claude`)

- `anthropics/claude-code-action` v1.0.262 pinned to commit `925afb53776cf1a2b738c6bdbed37b4d4000ef13`.
- Commits go through GitHub's API (`use_commit_signing: true`), so GitHub signs them, onto the existing PR head branch only. A step fails the job if the action reports another branch.
- Tools: the action's tag-mode defaults plus `--disallowedTools` for `gh`, `git push`, `git remote`, `curl`, `wget`, web fetch and web search. No merge, close, deploy or PR-creation tool is available.
- Permissions: `contents: write`, `pull-requests: write`, `issues: write`, `actions: read`. No `id-token`, so the Claude GitHub App token exchange is not used; the action uses `github.token`.
- One run per PR at a time, queued and never cancelled mid-edit.

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
