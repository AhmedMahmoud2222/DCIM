# Antigravity (agy) review agent

Workflow: `.github/workflows/agy.yml` (v1). Review only. It never pushes, merges or edits files.

## Use
Put the command at the start of a line in a comment.

    On a PR:     @agy review [optional focus]
    On an issue: @agy review #<PR number> [optional focus]

Allowed commenters: humans with OWNER, MEMBER or COLLABORATOR association, and agents whose exact login
(for example `claude[bot]`) is listed in the repository variable `AGY_ALLOWED_BOTS` (comma separated).
Fork PRs are refused. The focus text is one line, capped at 500 characters.

The prompt is `.github/agy/review-template.md`, read from the default branch. The workflow fills in the
PR, head and base SHA, linked issues (closing keywords in the PR body, plus the issue where you commented),
earlier review comments and CI results for the head SHA. Change the template through a normal PR.
The review names the reviewed head SHA, badges findings P1/P2/P3 and ends with one verdict line (AGENTS.md).
If the PR head moves during the run, the comment is marked stale.

## One-time setup (owner)
1. Settings > Environments > create `agy-agent`.
2. Add secret `GEMINI_API_KEY` to that environment (key from Google AI Studio).
3. Merge the workflow to the default branch. `issue_comment` workflows run only from there.

## Notes
- `agy -p` prints nothing without a TTY (upstream bug #76), so the job runs it under `script`.
- The agent workspace is the default-branch commit; the PR code reaches agy only as a diff file, so a PR cannot plant agent instructions. The diff itself is untrusted, so a crafted diff could still try to steer agy while GEMINI_API_KEY is set. Use a key with a low quota and restrict `agy-agent` to the default branch.
- Jobs hold no GitHub write token except `publish`, which holds no secret.
- Headless flags for `agy` change between releases. Check `agy --help` if the run step fails.
- Optional pin: set repository variable `AGY_INSTALLER_SHA256` to the sha256 of `https://antigravity.google/cli/install.sh`. The run then refuses a changed installer. Without it the run warns and continues.
