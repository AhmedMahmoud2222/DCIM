# Antigravity (agy) review agent

Workflow: `.github/workflows/agy.yml` (v1). Review only. It never pushes, merges or edits files.

## Use
Comment on an open, same-repository PR, with the command at the start of a line:

    @agy review [optional focus]

Allowed commenters: OWNER, MEMBER, COLLABORATOR. Bots and fork PRs are refused.
The review names the reviewed head SHA, badges findings P1/P2/P3 and ends with one verdict line (AGENTS.md).
If the PR head moves during the run, the comment is marked stale.

## One-time setup (owner)
1. Settings > Environments > create `agy-agent`.
2. Add secret `GEMINI_API_KEY` to that environment (key from Google AI Studio).
3. Merge the workflow to the default branch. `issue_comment` workflows run only from there.

## Notes
- `agy -p` prints nothing without a TTY (upstream bug #76), so the job runs it under `script`.
- Jobs hold no GitHub write token except `publish`, which holds no secret.
- Headless flags for `agy` change between releases. Check `agy --help` if the run step fails.
