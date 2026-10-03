# DCIM01 remediation and final-tree validation: handoff for independent review

**Document version: v1.2** (v1.0 draft; v1.1 heads refreshed and final-tree results filled; v1.2 owner-assigned fixes published for #81, #82, #84 with exact-head CI) (2026-10-01). Prepared by the remediation and validation agent for the independent Codex review. Documentation only. Nothing here is merged, deployed or closed. The repository owner keeps final merge authorization.

## 1. Live state and drift (observed 2026-10-01)

`origin/main` is `8f593e8793abc6a1faecadd692592af523f9d646`, not the last observed `f94f220`. The four commits in between are documentation only (`docs/PRODUCT_ROADMAP.md`, `docs/DOCUMENTATION_INDEX.md`, `README.md`; 3 files, 58 lines) and touch nothing under review. It is the reason four main-targeted PRs read `behind`. All listed PR heads match the last observed heads.

| PR | Head | Base | CI on head | Reviews | `mergeable_state` |
|---|---|---|---|---|---|
| #58 | `c57271f` | main | 10 success | 0 | behind |
| #59 | `6dac357` | main | 11 success, 1 failure (`github-advanced-security`) | 0 | behind |
| #60 | `41b3190` | main | 10 success | 0 | behind |
| #61 | `2e024cb` | main | 10 success | 0 | behind |
| #67 | `808707a` | #59 branch | 8 success | 0 | clean |
| #68 | `e150222` | #59 branch | 8 success | 0 | clean |
| #69 | `d26cc2d` | #59 branch | 8 success | 0 | clean |
| #70 | `b9ee791` | #59 branch (`feature/user-group-management`) | 22 success, 2 failure (`github-advanced-security`) | 0 | clean |
| #71 | `ff19127` | main | 18 success | 0 | behind |
| #79 | `28f7d48` | main | 10 success | 0 | behind |

`mergeable_state` for #71 is `behind`, not `blocked`; see section 8.

Tooling (checked before claiming dynamic validation): Git yes; PostgreSQL 16.14 yes (cluster started locally); Redis 7.0.15 yes; Docker CLI yes, the daemon had to be started by hand (`dockerd`), image pulls work, image **builds** fail because the container `apt-get` gets HTTP 403 from the sandbox egress policy; real `clamd` yes (apt package 1.5.4, local signature set, ClamAV signature download not possible); Chromium pre-installed (Playwright's expected build differs, so `PLAYWRIGHT_CHROMIUM_PATH` was used).

## 2. Remediation branches and PRs (all new; no existing PR branch changed, none retargeted)

| PR | Branch | Head | Base branch | Content |
|---|---|---|---|---|
| #80 | `claude/dcim01-pr70-clamd-fail-closed` | `1521104` | #70 | strict, bounded clamd reply parser |
| #81 | `claude/dcim01-pr67-strict-authority` | `e2df933` | #67 | strict outranking; reuses candidate `claude/pr67-equal-authority-fix-5vg7ee` (`70d0084`) |
| #82 | `claude/dcim01-pr68-import-job-acceptance` | `d92fd77` | #68 | acceptance matrix, contract note |
| #83 | `claude/dcim01-pr69-nonvacuous-authz-matrix` | `30d38db` | #69 | non-vacuous matrix, exact sweep |
| #84 | `claude/dcim01-pr71-lifecycle-verification` | `ea58739` | main | tests only |
| none | `claude/dcim01-audit-v1.3-corrections` | `967775c` | `security/dcim01-audit-v1.3` | v1.3.1 documentation, no PR opened on purpose |

## 3. #67, strict authority dominance (PR #81)

The candidate has three commits beyond #67, not one (`04e396f` reported, head `70d0084`). I re-verified it independently, found no blocking gap, and reused it. Every writer of authority tables lives in `users.py` and `groups.py` (grep of all ORM writes); the only writers that change which site a rack is in are `move_rack` and `retire_rack_placement`, and both take the shared lock. Nothing re-parents rooms, floors or buildings.

Rule, in plain terms, is in the PR. Deliberate difference to review: creation and delegation may confer authority up to the actor's own; an existing principal needs strict dominance. Consequence: two global Administrators are peers and cannot administer each other via the API; recovery is out of band (documented as "Break-glass").

Fail-before on official #67 head `808707a` with the same files: **52 failed, 128 passed**. 50 are authorization/race assertions, 2 are structural lock tests that need `authority_lock` and are not counted. Import errors and missing helper names were removed from the reproduction by splitting the two white-box tests into `test_pr67_authority_lock_protocol.py`. Pass-after: the same files pass; the branch's full backend run excluding the retention-hostile file: 1028 passed, 0 failed (progress-line count, no summary line was printed for that run). Final-tree counts are in section 7.

Races use independent sessions and a barrier inside the authorization decision; blocking is observed in `pg_locks`, not by timers. #57 stays open; the tenant statement is in `docs/USER_GROUP_MANAGEMENT.md`.

## 4. #70, scanner protocol (PR #80)

Reproduced on `b9ee791`: the verdict was `text.endswith("OK")`; a malformed line ending in OK, an unterminated `stream: OK` followed by a closed connection, and a clean line followed by a contradictory verdict were clean; the buffer was unbounded. Fix: one NUL-terminated clamd line, read to EOF within 1024 bytes and one total deadline; anything else is `ScannerProtocolError` (a `ScannerUnavailable`).

Fail-before: 30 of 57 new protocol tests fail on `b9ee791` (the 12 existing scanner tests pass); 3 of 6 HTTP upload cases return 201 and store the file. Pass-after: 57 + 12, 6 of 6. Real `clamd` (6 tests, none skipped): raw framing is one NUL-terminated line then EOF; a 25 MB stream is clean; 25 MB plus 1 KB gives no verdict; end-to-end upload passes.

## 5. #68 and #69 (PRs #82, #83)

#68. Operations: status, rows (and `?status=invalid`), report, commit, cancel, upload, templates. No list or retry endpoint. On #59 head `6dac357`, 72 attempts by restricted A/B users on foreign jobs: status 200 x10, rows 200 x10, invalid-rows preview 200 x10, report 200 x2, commit/cancel 403 x24. On #68: all 72 are a 404 identical to a missing id, state unchanged, 11 of 11 pass.

Contract conclusion: the import permissions are inactive for restricted users, so uploader-only ownership is reachable only for a historical owner. Ownership alone does not equal scope (such an owner still reads rows describing rooms outside their scope). Not decided and not changed. Decision options and affected operations are in the PR and `docs/USER_GROUP_MANAGEMENT.md`.

#69. Observed operations 178; refused 121 (119 by the permission layer, 2 refused for everyone); reachable 57, pinned exactly. New tests: 31 matrix + 4 sweep; 49 collected across the four #69 files. Ten mutants fail the targeted test for the intended reason.

## 6. #71 (PR #84)

#76 is contained: its head SHA equals #71's head. On #71: 12 of 12 pass (row lock observed in PostgreSQL; held until commit; six-way contention proven; audit chain checked). On the parent `dc858eb`: 10 failed, 2 passed, for the intended reasons (no `FOR UPDATE`, no wait on a held row, timestamp on a live asset, duplicate decommission rows, broken chain).

Write paths: `transition_lifecycle` is the only mover of an existing asset. Bulk-import equipment create sets any initial status and leaves `decommissioned_at` NULL (characterised by a test). `planned`, `installed` and `reserved` may move to `removed` with no timestamp. A CHECK stays a follow-up: only the one-directional "no live asset carries a timestamp" is safe today.

## 7. Local integration and final-tree validation (local simulation only)

**This is a throwaway local simulation. It was never pushed and never merged into remote main.**

Base `origin/main` `aadaa37` (already contains #71, merged by the owner; #84 now targets `main`). Merge order, each `--no-ff`:

| # | Source | Source SHA | Result |
|---|---|---|---|
| 1 | #58 `security/sec-auth-02-refresh-reuse` | `c57271f` | clean |
| 2 | #60 `security/sec-auth-01-login-throttle` | `41b3190` | clean |
| 3 | #61 `security/sec-ci-01-workflow-permissions` | `2e024cb` | clean |
| 4 | #59 `feature/user-group-management` | `6dac357` | clean |
| 5 | #81 `claude/dcim01-pr67-strict-authority` (carries #67 `808707a` and the candidate) | `e2df933` | clean |
| 6 | #82 `claude/dcim01-pr68-import-job-acceptance` (carries #68 `e150222`) | `d92fd77` | conflict in `docs/USER_GROUP_MANAGEMENT.md` (both append at the end); kept both sections |
| 7 | #83 `claude/dcim01-pr69-nonvacuous-authz-matrix` (carries #69 `d26cc2d`) | `30d38db` | clean |
| 8 | #80 `claude/dcim01-pr70-clamd-fail-closed` (carries #70 `b9ee791`, which already contains #59 through a merge commit) | `1521104` | clean |
| 9 | #84 `claude/dcim01-pr71-lifecycle-verification` (carries #71 `ff19127` and #76) | `ea58739` | clean |
| 10 | #79 `security/ci-alembic-single-head` | `28f7d48` | clean (its `ci.yml` change merges with #61's) |

`.github/workflows/ci.yml` is touched by #61 and #79 and did not conflict. #62 (audit report) was not part of the simulation.

Final simulated commit: `a9ab4b9923a94e4af8a4eaf494211d5f94cfd5fa`, tree `7dfbfe7fee2bb24ed6bf8539158b617b7bfed342` (branch `local-sim/final-tree2`, local only). Only the two #59-stack documentation merges conflicted (`docs/USER_GROUP_MANAGEMENT.md`, both sides append); I kept both sections. An earlier simulation (`557bf08`) predates the #81 stale-grant commit and the #84 revision; its frontend, Edge Collector and E2E results are marked as produced on that earlier tree.

Alembic: exactly one head, `0032_catalog_documents`, chain `0030_bulk_import_attempts -> 0031_user_groups -> 0032_catalog_documents`; gate `scripts/check_alembic_single_head.py` prints `OK: single Alembic head 0032_catalog_documents`.

Backend, exact final tree `a9ab4b9` (fresh PostgreSQL database at Alembic head, Redis, nothing running concurrently):

| Run | Passed | Failed | Skipped |
|---|---|---|---|
| Full backend suite excluding the retention-hostile file (as CI does) | **1352** | 0 | 0 |
| `tests/integration/test_mvp_retention_hostile.py` (as CI runs it separately) | **6** | 0 | 0 |

Single Alembic head confirmed on this tree (`OK: single Alembic head 0032_catalog_documents`). Frontend, Edge Collector, migration round trip, Compose config and browser E2E ran on the earlier simulation tree `557bf08` and were not repeated on `a9ab4b9`; the later differences touch backend tests and the authority scope code only. Compose runtime smoke was not run (section 11).

Ancestry and squash: #70's branch contains #59 through a merge commit, so after #59 is squash-merged its history does not contain main's squash commit; retargeting #70 (and #67, #68, #69, #80 to #84, which stack on those) to `main` shows #59's diff again until `main` is merged into each branch, which is content-neutral. `required_linear_history` forbids merge commits on `main`, so squash is the workable method for branches that contain merge commits (#70, #80). A squash creates one GitHub-signed commit per PR and loses per-commit authorship. Each child's CI must re-run after retargeting. #71 and #79 target `main` directly and are independent of the #59 stack. #79's gate passes on `main` alone and on the combined tree, and fails on the unfixed #59 plus #70 combination.

## 8. #71 merge requirements (history: #71 has since been merged by the owner)

Live: `behind` (4 documentation commits behind main). The active ruleset "Default Policy" (target: default branch; owner may bypass; I did not) requires: no deletion, no force push, **linear history**, **signed commits**, a **pull request** (0 approvals, thread resolution, "extra approval for unattributed changes"), and the checks **`backend` and `frontend`**, **up to date with main**. Checks `backend` and `frontend` are green on `ff19127`. Commit verification: `ff19127` is authored and committed by `t <t@t>` (reason `no_user`), `dc858eb` and `9bbd979` are `unsigned`.

Unmet today: (1) the branch is not up to date with `main` (strict status checks); (2) the three commits are not verified; (3) the "unattributed changes" rule most likely applies to `ff19127` but its exact effect is not readable here (classic branch protection returns 403). The signature requirement is not the only cause and may not be one at all if the owner squash-merges, because the squash commit is created and signed by GitHub; that was not tested here.

Compliant remediation: the owner updates the branch (not done here; I did not touch the PR branch), squash-merges through GitHub (allowed method, signed result), and reviews whether the unattributed-change rule needs an extra approval; or re-author and sign the three commits on a replacement branch. No bypass was used.

## 9. Code scanning (kept separate from product evidence)

On `b9ee791` the two current `github-advanced-security` runs (`36822804519`, `36822803744`, 2026-10-01 06:03 UTC) are dynamic runs "Code scanning AI findings on PR #70". Both fail in "Processing Request" with `CAPIError: 400 The requested model is not supported`; `COPILOT_AGENT_MODEL` is `sweagent-capi:claude-opus-5[ReasoningEffort=medium]`. Same failure on new PRs #80, #81 and #82 today. No file was analysed, so this is a tooling failure, not a scan result and not green.

The successes on #71 and #79 are not evidence of analysis: the #71 log shows no model call (`Sessions disabled: not supported for code scanning yet`, results reported in 3 s). Mandatory protection: only `backend` and `frontend` are required (ruleset); `github-advanced-security` is not required, and #70's base is a feature branch, so no ruleset applies to it. A dynamic run cannot be retried through the API (403 "cannot be retried"). Supported repair: the owner selects a supported model for the code-scanning agent in repository or organisation settings, then a new push triggers a new run. Scanning must not be disabled meanwhile. I did not change any setting.

## 10. Issue disposition (nothing closed)

#57 open (tenant model, outside this work). #63, #64, #65: addressed by #67 plus #81; leave open until merged. #66: #68 plus #82 (contract decision open); leave open. #72, #74: in #70; #73: lineage fixed and #79 gate; #75: row lock in #71 (CHECK follow-up and import-path finding B remain). New unnumbered findings for the owner to file: A (scanner reply handling, fixed in #80) and B (import creates terminal lifecycle statuses without a timestamp).

## 11. Remaining blockers

1. Owner decision on the import-job contract for restricted users (options a, b, c in #82).
2. #71 merge requirements (section 8).
3. A supported model for code scanning, then a clean scan on the final heads.
4. Compose runtime smoke (build, ClamAV health, shared media at runtime) was not run: image builds are blocked here. Run it on a host with registry and apt access.
5. Peer-recovery procedure for global Administrators (operational decision attached to the strict rule).
6. Product decision for finding B and the lifecycle CHECK.

## 12. Exact heads for review

PR heads and the simulation SHAs are in sections 2 and 7. Reproduce a fail-before by copying a PR's new test files onto the base head named in the table and running them; test files that import new helper names are split out and are not used as reproductions.

| PR | Head |
|---|---|
| #80 | `15211049e97ef8eb6f1bba964ff5cc6173357c3a` |
| #81 | `9f2b0f901b86985ac2b07ba60d8aff125c2fd8e4` |
| #82 | `7915febacf977d9250c01860b7b959447a40e3bd` |
| #83 | `30d38dbf47a804e4ac8a97ca5b77dd6e5fd86584` |
| #84 | `44da8799576d9d3d98ab4597cbe1bf79251752f5` |
| main | `8b6d004` (was `aadaa37` when the final-tree run was made) |

## 13. Owner-assigned fixes (2026-10-02), published on the existing branches

All three are normal fast-forward pushes or a merge commit; no force-push. All commits are **unsigned** (the sandbox signing key file is empty), so none is GitHub-verifiable. I authored all three, so none counts as independent acceptance; each head needs a reviewer who did not write it. No thread was resolved.

| PR | Head | Change | Evidence | Exact-head CI |
|---|---|---|---|---|
| #81 | `9f2b0f9` | `scope_contains` compares raw grants again; `current_only` drops stale inner ids and is used only in the reverse half of `actor_strictly_outranks`; new latent-group-grant test | Fails on `e2df933` (group assignment 200), passes on the fix; equal-peer stale test unchanged; authority, group and concurrency files 236 passed on PostgreSQL | 7 of 7 success |
| #82 | `7915feb` | Exact 403 on all upload routes; all template routes requested (rack, equipment 200 XLSX; catalog 403); committed historical-owner job with real XLSX report, exact 200 reads and exact 403 commit and cancel | 12 passed on head; 7 failed, 5 passed on `6dac357` | 7 of 7 success |
| #84 | `44da879` | Merge of main `8b6d004` into the branch; tree diff against main is still the one test file (+298) | 14 passed locally (12 verification and 2 existing lifecycle tests), single Alembic head `0030_bulk_import_attempts` | 9 of 9 success |

Remaining from section 11 unchanged: import-job contract decision, code-scanning model, Compose runtime smoke on a host with registry access (the #84 CI run shows `Compose smoke` green on GitHub), peer-recovery procedure, finding B and the lifecycle CHECK. The final-tree validation in section 7 was run on a tree based on `aadaa37` and has not been repeated on main `8b6d004` with the new heads.
