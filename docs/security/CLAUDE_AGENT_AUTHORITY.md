# Claude agent authority (workflow v2.0)

`.github/workflows/claude-agent.yml` runs Claude on a pull request when an authorized user
comments `@claude <task>`. It is independent of the OpenCode workflow: separate gate
(`.github/scripts/claude_gate.py`), separate allowlist variable, separate secret and
separate concurrency groups. `/oc` and `/opencode` stay with `opencode.yml`.

The agent never holds write access. Claude runs in a read-only job and produces a change manifest;
a separate trusted job, which has no Claude and no PR code, validates the manifest and publishes it.

```
gate (no secrets)  ->  claude (Claude secret, READ-ONLY token)  ->  publish (write token, no Claude)
authorizes, claims     edits ./pr, emits changes.json              re-validates, one signed commit,
the comment                                                         fast-forward of the PR branch
```

## Authorization (job `gate`, no secrets)

A comment runs Claude only when all of these hold:

- Event is `issue_comment` / `created` in this repository (the workflow file always comes from the default branch).
- Commenter is a `User` account whose numeric id is in the repository variable `CLAUDE_ALLOWED_ACTOR_IDS` (comma-separated ids; empty or malformed fails closed) and whose `author_association` is OWNER, MEMBER or COLLABORATOR.
- The comment starts with `@claude` followed by whitespace and a task (case-sensitive, 8000 characters at most). Quotes, code blocks, mid-text mentions, `@claude-x` and `/oc ... @claude` do not match.
- The target is an open pull request whose head and base are both in this repository, with a valid head SHA and branch name.
- The PR base is `main`, the repository default branch is still `main`, and the branches API reports `main` as `protected: true`.
- No earlier run has claimed the comment. The gate adds an `eyes` reaction as the claim; a second delivery of the same comment sees it and stops. Gate runs for one comment are serialized.

The task text reaches the Claude prompt only as a job output consumed by the action's `prompt` input, written with a random heredoc delimiter so it cannot forge other outputs, and never through a shell.

## Read-only execution (job `claude`)

- `anthropics/claude-code-action` v1.0.262 pinned to `925afb53776cf1a2b738c6bdbed37b4d4000ef13`, in **agent mode** (selected by the explicit `prompt` input): no tracking comment, no branch setup, no file-ops server.
- Permissions: `contents: read`, `pull-requests: read`. No comment, issue, actions, checks or `id-token` permission. The repository token cannot push, comment or create refs, so there is no comment permission to retain.
- Tools: an explicit allowlist `Read,Glob,Grep,Edit,Write`, with `Bash`, `WebFetch` and `WebSearch` denied. Claude edits files but cannot run commands.
- The trusted script is installed from the workflow commit into `$RUNNER_TEMP` before the PR is checked out.
- The PR head is checked out into `./pr`, not the workspace root. The action (which runs `bun` in the workspace root) and the Claude CLI read startup configuration from the root (`.claude/`, `.mcp.json`, `bunfig.toml`, `.env`, `CLAUDE.md`), so none of it can come from the PR. Executable configuration inside `./pr` is also deleted before Claude starts. In agent mode the action does not restore configuration from the base branch, which is why the checkout location is the control.
- The live PR, protected `main` base and head branch are re-verified before the checkout and again immediately before Claude starts.
- After a successful run, `collect` compares `./pr` with the recorded tree without invoking `git` and writes `changes.json`, at most 100 files, 1 MiB per file and 5 MiB in total. Symlinks, special files and oversized files fail the job. Edits under `.github/`, executable configuration or agent instructions (`CLAUDE.md`) are dropped.

## Trusted publication (job `publish`)

- Permissions: `contents: write`, `pull-requests: read`. Holds no Claude secret and checks out no PR code: only `claude_publish.py` from the workflow commit.
- The manifest is untrusted. The publisher re-validates it: it must be bound to this authorization (SHA, branch, PR number), every path must be safe and unprotected, modes must be `100644` or `100755`, contents must be valid base64 within the limits, and there must be no duplicates. A hostile manifest causes no API request at all.
- It re-checks live state (open and unmerged, same repository, protected `main` base, head branch present and exactly at the authorized commit), creates blobs, a tree on the authorized commit's tree and **one commit with the authorized commit as sole parent**, and refuses to continue unless GitHub reports the commit `verified` (signed by GitHub for the workflow token).
- It checks the live state once more and then updates the existing branch with `PATCH git/refs/heads/<head>` and `force: false`. That update is the atomic guard: GitHub rejects it unless the new commit descends from the branch's current head, so a moved HEAD cannot be overwritten. It is a PATCH, so it can only move a ref that exists; the script never creates a branch or pull request, and never merges, closes, approves or deploys.
- Concurrency uses a group separate from the `claude` job so a queued publish is never replaced by a later run.

Residual risk: GitHub has no atomic compare across the PR API and the Git data API. If the PR closes or merges in the milliseconds between the last check and the ref update, the commit still lands on that existing branch (never on a new one). See `CLAUDE_AGENT_PROTECTIONS.md` for the ruleset that limits what such a branch can become.

## Setup (owner)

1. Create the repository variable `CLAUDE_ALLOWED_ACTOR_IDS` with the numeric ids of approved users.
2. Create the repository secret `CLAUDE_CODE_OAUTH_TOKEN`. Run `claude setup-token` locally with the Claude subscription account to generate it. This uses the existing subscription; no API key is needed.
3. Apply the protections in `CLAUDE_AGENT_PROTECTIONS.md`.
4. Merge the workflow to the default branch. `issue_comment` workflows run only from there.

## Known limits

- Commits created with `github.token` do not start other workflows, so CI does not re-run on Claude's commits until someone pushes or re-runs checks. Branch protection still gates merge.
- Claude cannot run tests or commands in this design; it edits files only. Maintainers run CI on the published commit.
- Edits to workflows, scripts, `.claude/` and `CLAUDE.md` are never published; make those changes by hand.
- Re-running a finished run is rejected as a duplicate. Post a new `@claude` comment instead.
- Live execution depends on the secret and variable above; offline tests cannot prove it.

## Tests

`tests/deployment/test_claude_agent.py` covers authorization, command parsing, routing against the OpenCode gate, duplicate claims, workflow policy and isolation.

`tests/deployment/test_claude_write_boundary.py` covers untrusted base configuration, the live-state check (closed and merged PRs, HEAD changes, deleted or renamed head branches), hostile manifests, a real-git `prepare`/`collect` flow, the publisher against a scripted GitHub API (including a PR closing between the last check and the ref update, a rejected non-fast-forward update, unsigned commits, and proof that no ref is ever created), and the workflow permission and ordering invariants.
