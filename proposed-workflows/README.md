# proposed-workflows (drafts only, inactive)

Files in this directory are inert: GitHub only executes `.github/workflows/`. Every draft
needs independent Codex review and an owner-run Jules Tier 2 review on its exact SHA; only
the owner applies it.

**Current state (PR #224; the branch head moves, so check the PR for the exact SHA):** PR #224 is not merged. `.github/workflows/refresh-branch.yml`
exists on the PR branch (not on `main`) and contains the v3 content (P2-1..P2-4 candidate fixes),
so merging the PR activates it. It does not run on this PR because `issue_comment` uses the
default-branch copy. `proposed-workflows/refresh-branch.yml` is now v4 = v3 + F1 (422 handling)
and is not yet in the live file; owner copy is pending. The live header comment still carries the
old "NOT ACTIVE" wording (F2); the v4 header corrects it, so copying v4 fixes it. No secrets or
variables are configured by this PR.

## PR #224 findings (P2-1..P2-4: candidate fixes are present in the PR-branch workflow; threads stay open until independent confirmation. Column "Draft remedy" describes `proposed-workflows/refresh-branch.yml`)

| # | Finding | Draft remedy (`refresh-branch.yml`) | Status |
|---|---------|--------------------------------------|--------|
| 1 | `contains(fromJSON(var), login)` does substring matching when var is a JSON string | `gate` job: `jq` requires non-empty array of login-shaped strings, exact match; unset/malformed/string/object deny; no secret in `gate` | Candidate, untested |
| 2 | Workflow-level concurrency lets unauthorized comments displace authorized pending runs | `concurrency` moved to job `refresh`, which runs only after `gate` passes | Candidate, untested |
| 3 | update-branch merges whatever base the PR has | Requires open PR, `base.repo == repo`, `base.ref == main`, `head.repo == repo` | Candidate, untested |
| 4 | Live 438ed15: `jq -e` on a stream; `null ["x"]` lets the last array authorize (Codex thread r4423421202) | Draft v3 `gate`: `jq -s -e`, requires `length == 1` document, then nonempty array of login strings with exactly one exact match. Malformed, empty, multi-document, duplicate-actor entries deny | Candidate, untested; owner copy pending |
| F1 | OpenCode: `gh api -X PUT` under `set -e` fails the step on benign 422 (already current / conflict) | v4 captures the PUT; "no new commits" -> exit 0, "merge conflict" -> `::error::` exit 1, other -> print and fail. `expected_head_sha` kept | Must-fix; candidate, untested; owner copy pending |
| F2 | OpenCode: live header says "NOT ACTIVE"; README said live file unmodified | v4 header and this README describe the current state accurately | Must-fix; candidate; owner copy pending for header |
| F3-F8 | Duplicate draft, base/state TOCTOU, duplicate-actor diagnostic, runner-minute pre-filter, PR feedback, case-insensitive match | Not changed; tracked in #242 (deferral needs Codex agreement) | Open |
| - | Race between SHA check and update | Server-side `expected_head_sha` CAS retained; local compare is only a fast-fail | Candidate |
| - | Noisy red runs on bare `/refresh-branch` | Pre-filter `startsWith('/refresh-branch ')` plus single-line strict regex | Candidate |
| - | Does PAT push start required CI? | Unproven; script prints the new head to verify manually | Open, needs real run |

## Other drafts

- `claude.yml`: secret-free `prepare` job requires OWNER/MEMBER/COLLABORATOR association and validates a default-empty exact `CLAUDE_ALLOWED_BOTS`; `claude` job needs it.
- `claude-code-review.yml`: same-repo PR gate and default-empty exact bot allowlist (`CLAUDE_REVIEW_ALLOWED_BOTS`).

## Not drafted (unresolved, blocker: source not available)

- `review-gate.yml`: no such file exists on this branch or in `.github/workflows/`.
- The #207 workflow: PR #207's content is not on this branch and I cannot fetch it here. `docs/security/AUTONOMOUS_OPERATIONS.md` only says it adds a separate Claude command gate.
- `agent-claude.yml` "test services/allowed commands": the live file has no test services, and the allowed-command grammar lives in `.github/scripts/orchestration_claude_adapter.py`, which I did not change or assume. Say which change you want and I will draft it.

## Owner application steps (after both reviews accept the exact SHA)

1. Copy the accepted draft over the matching `.github/workflows/` file in a PR you author.
2. For refresh: create variable `REFRESH_ALLOWED_ACTORS` (JSON array) and secret `REFRESH_PAT`; optionally an environment-scoped secret.
3. After merge, run `/refresh-branch <sha>` on a throwaway same-repo PR and confirm `backend`/`frontend` start on the new head. Also test a denied actor, a non-main-base PR, a stale SHA, and an unset variable.
