# Repository protections for the Claude agent

Review of `main` protections and a proposed ruleset for Claude PR branches. Observed read-only on
2026-10-29 through the GitHub API; nothing here has been applied. Settings changes are owner actions.

## Observed state

| Item | Value | Concern |
|---|---|---|
| Ruleset `Default Policy` (id 23651422) | Active, targets the default branch | |
| Rules | `deletion`, `non_fast_forward`, `required_linear_history`, `pull_request`, `required_status_checks` | No `required_signatures` |
| `pull_request` rule | 0 approvals, thread resolution required, stale reviews dismissed, no code-owner review, no last-push approval | Nothing forces a second human to review workflow or agent changes |
| Required checks | `backend`, `frontend` from integration 15368, strict | The tests that guard `.github/**` (`Deployment tooling regressions`, `ci-policy`) are not required |
| Bypass actors | Integrations 842251, 946600, 1144995, 1236702, 4229212 and user 285010264, all `always` | Six identities can skip every rule above; five are unnamed in the API output |
| Classic branch protection | None | |
| Actions default `GITHUB_TOKEN` | `write` | Every workflow without its own `permissions:` block gets repository write |
| Actions may approve PRs | `true` | A workflow token can supply a review |
| `delete_branch_on_merge` | `false` | Merged PR branches stay in place (this is what keeps a late publish from recreating a branch) |

The Claude workflow does not depend on any of the loose settings: it sets `permissions: {}` at the top
and a minimum per job. `main` protection is part of its trust boundary, because the gate refuses any
base that is not a protected `main`; a bypass actor can change what the agent is allowed to propose.

## Recommended changes to `main`

1. **Name and prune bypass actors.** In Settings > Rules > Default Policy, identify the five integrations (the ids above) and remove any that are not needed. Switch the remainder from `always` to `pull_request` so that even bypass goes through a PR. Keep the human owner as the single break-glass identity.
2. **Require a review that is not the author.** Set `required_approving_review_count` to 1 and `require_last_push_approval` to true. With count 0, the agent's own PR can merge with no human review.
3. **Add code owners for the trust boundary** and enable `require_code_owner_review`. Suggested `CODEOWNERS` entries: `/.github/` , `/.claude/`, `/CLAUDE.md`, `/docs/security/`, `/tests/deployment/test_claude_*.py`.
4. **Require the policy tests**: add `ci-policy` and `Deployment tooling regressions` to the required status checks, with the same integration id as the existing checks, so a change to `.github/**` cannot merge without its workflow tests.
5. **Add `required_signatures`** to the ruleset (the publisher's commits are GitHub-signed and the repository already uses signed commits).

## Repository settings (safe, no effect on this workflow)

```
gh api -X PUT repos/AhmedMahmoud2222/DCIM/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

Audit first: any workflow that needs write and relies on the old default must declare `permissions:`
explicitly. List them with `grep -L '^permissions:' .github/workflows/*.yml`.

## Ruleset for Claude PR branches

`docs/security/rulesets/claude-pr-branches.json` protects `refs/heads/claude/**`:

- `deletion`: a PR branch cannot be deleted, so the publisher can never be asked to write a vanished branch back into existence.
- `non_fast_forward`: no force-push or history rewrite, which matches the publisher's non-forced update.
- `required_signatures`: every commit must be verified. The publisher's commits are signed by GitHub for the workflow token.
- No `update` rule and no bypass actors, so the trusted publisher (the `github-actions` token) is not blocked: it only fast-forwards an existing branch.

Apply (owner):

```
gh api -X POST repos/AhmedMahmoud2222/DCIM/rulesets \
  --input docs/security/rulesets/claude-pr-branches.json
```

Optional stricter variant: add `{ "type": "creation" }` to the rules and list the repository admin
role as a bypass actor, so only a human can create `claude/**` branches. Note that the Claude Code
web app creates such branches on the owner's behalf, so test that flow before enabling it.

The publisher can only update a branch that already exists, so the `creation` rule is not needed for
the workflow itself; it limits what any other credential can do.

## Verification after applying

1. `gh api repos/AhmedMahmoud2222/DCIM/rulesets --jq '.[]|{id,name,enforcement}'` lists both rulesets as `active`.
2. A force-push to a `claude/test-protection` branch is rejected.
3. After the workflow is activated, a comment on a scratch PR produces one verified commit authored by the workflow token and no new branches.
