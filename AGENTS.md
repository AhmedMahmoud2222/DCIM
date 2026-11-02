# AGENTS.md

## Review guidelines

These rules apply to every independent reviewer (human, Codex, OpenCode, Jules or any other agent) reviewing a pull request in this repository.

1. **Name the exact reviewed commit.** Begin the review with the full head SHA that was reviewed. A review that does not name a SHA, or names a SHA that is not the current PR head, does not count for the current head.
2. **Badge every finding by severity.**
   - **P1**: security, data-loss, correctness or production-safety defect that must be fixed before merge.
   - **P2**: defect or missing coverage that should be fixed before merge.
   - **P3**: minor issue, style or follow-up that does not block merge.
3. **End the review with exactly one verdict line**, as the last line, using one of these exact strings:
   - `Verdict: Accepted`
   - `Verdict: Not accepted`
   - `Verdict: Changes required`
4. A review with any open P1 or P2 finding must not end with `Verdict: Accepted`.
5. Reviews do not authorize merging, workflow changes, secret or permission changes, or production actions.

Changes to this file and to other reviewer instructions are merged by Ahmed only.
