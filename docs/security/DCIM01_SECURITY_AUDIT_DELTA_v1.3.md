# DCIM01 security audit, delta v1.3

**Document version: v1.3.1** (2026-10-01). Revision log at the end. This revision corrects three statements of v1.3 and adds the scanner finding and the verified code-scanning evidence. Documentation only; unreviewed; not for publication until the owner approves.

Adds to `DCIM01_SECURITY_AUDIT_REPORT_v1.2.md`. Covers PR #70 (catalog datasheet PDFs), PR #71 (decommission timestamp), findings #72 to #75, and the migration lineage. Exploit-relevant detail is left out on purpose. Regression tests carry it, generated at runtime, with no sample files.

Baseline: `main` `f94f220` (since moved to `8f593e8` by four documentation-only commits; no code overlap). Heads reviewed: #70 `b9ee791`, #71 `ff19127`. Remediation branches that supersede parts of this report are listed in section 16. No merge or deployment has taken place.

## 9. New attack surface (PR #70)

An authenticated administrator can upload a PDF up to 25 MB. The server parses it, scans it, stores it by content hash, and serves it back as a download. Parsing untrusted PDFs is the new exposure. The server never renders or executes PDF content.

## 10. Verified protections

| Area | What holds | Evidence |
|---|---|---|
| Parser isolation | pypdf runs in a child interpreter: 15 s CPU, 1 GB address space, 20 s wall clock, sockets and DNS disabled, 25 MB and 100-page caps | resource probes: millions of objects, cyclic and deep page trees, stream and object-stream bombs end in a clean rejection |
| Malware scanning | `required` mode fails closed (HTTP 503) when the scanner is absent, hung, crashed or answers with an error line. Infected files are rejected and audited. Production refuses to start in any other mode. **Correction (v1.3.1): a malformed or truncated reply was not rejected in #70 `b9ee791`; see finding A** | scanner policy matrix plus real `clamd` tests; corrected by the strict reply parser in section 16 |
| Storage | key is `{sha256}.pdf`, never a client name. Writes are atomic. 20 hostile filenames tested | hostile API suite |
| Versioning | published and retired revisions are frozen by database triggers. A newer datasheet cannot change a published revision | schema tests, mutation testing |
| Concurrency | per-object advisory locks serialize upload and purge. Independent database sessions, lock-span probes from a second connection | hostile API and concurrency suites |
| Authorization | anonymous, ordinary, restricted, site admin and Administrator tested on every endpoint, with draft-disclosure and IDOR checks | hostile API suite |
| Migration | upgrade, downgrade with linked data, re-upgrade; RESTRICT foreign keys and immutability trigger fire | section 12 |

## 11. Findings

| # | Finding | Severity | Status |
|---|---|---|---|
| 72 | Active-content screening did not cover objects stored in compressed object streams | Medium | Fixed in #70 head (PR #77). 5 of 5 fixtures failed before, all pass after |
| 73 | #59 and #70 both added a `0031` migration on the same parent, producing two heads | Medium (blocks startup) | Lineage corrected; permanent gate in PR #79 |
| 74 | Raw client filename in a malware-rejection audit record; duplicate upload could return a row a purge was deleting; orphan objects never swept; unscanned documents served after a move to `required` | Low to Medium | Fixed in #70 head (PR #78). 4 tests failed before, all pass after |
| 75 | Lifecycle transition had no row lock: duplicate decommission events, and a status/timestamp mismatch under a race | Medium | Fixed in #71 head (PR #76). 2 race tests failed before, pass after |
| A (no issue number yet) | The `ClamdScanner` reply handling treated any reply whose text ended in `OK` as clean, stopped reading at the first terminator, accepted an unterminated reply when the connection closed, and buffered without a size or time bound. A malformed, truncated or contradictory reply could therefore be recorded as a clean verdict | Medium to High (a scanner or network fault can turn "no verdict" into "clean") | Fixed on `claude/dcim01-pr70-clamd-fail-closed`: one NUL-terminated line, read to EOF within 1024 bytes and a total deadline. 30 of 57 new protocol tests and 3 of 6 HTTP upload cases fail on `b9ee791` (the 12 existing scanner tests pass on both); all pass with the fix; real `clamd` controls pass |
| B (no issue number yet) | Bulk-import equipment CREATE accepts any lifecycle status as the initial status and does not set `decommissioned_at`, so an equipment asset can exist as `decommissioned` with a NULL timestamp and no lifecycle audit row | Low to Medium (integrity) | Open; product decision (reject non-`planned` initial statuses on import, or stamp the timestamp). Characterised by a test on `claude/dcim01-pr71-lifecycle-verification`. Not a reason to change #71 |

Issues stay open until the owner confirms the merged state.

## 12. Migration lineage

Final: `0030_bulk_import_attempts` -> `0031_user_groups` (#59) -> `0032_catalog_documents` (#70).

Revision id, `down_revision`, filename, docstrings and references were updated. The migration SQL is unchanged. Checked on a database seeded at 0030 with a user, manufacturer, catalog model and revision, then groups at 0031, then a linked document at 0032: one head; downgrade to 0031 removes only the document tables and permission; downgrade to 0030 removes only the group tables; re-upgrade restores 3 document grants and 4 user/group grants; existing user, group and catalog rows survive every step.

A repository gate (PR #79, `scripts/check_alembic_single_head.py`) fails CI when the graph has other than one head. It fails on the unfixed #59 plus #70 combination and passes on the corrected chain.

## 13. Decommission lifecycle (PR #71)

The column is `TIMESTAMP WITHOUT TIME ZONE`; the fix stores naive UTC. The transition endpoint is the only writer of the timestamp, and it raised before commit under the original bug, so no stored row carries a bad timestamp from that path and no backfill is needed. **Correction (v1.3.1): it is not the only writer of the status.** Bulk-import equipment create sets an initial status directly (finding B). `planned`, `installed` and `reserved` may also move to `removed` without a timestamp, so a rule "timestamp set exactly when status is decommissioned or removed" does not hold today; a safe CHECK is the one-directional "no live asset carries a timestamp". A row lock now serializes transitions. A CHECK tying the timestamp to the status needs a migration and is not included.

## 14. Remaining decision: tenant scope (#57)

Catalog documents are global manufacturer and catalog data. They have no site, rack or tenant link, and this report does not add one. Tenant visibility of catalog documents is open until the tenant model is designed. #57 stays open.

## 15. Untested

- ClamAV container start and signature download on a real host (CI Compose smoke covers startup; no signature update was exercised).
- Behavior under sustained concurrent uploads at production volume.
- Browser-side rendering of downloaded datasheets (served as `attachment`, `nosniff`).
- Object storage backends other than local disk.
- Code scanning on #70: see section 17. It is not an analysis result.

## 16. Remediation branches and results (v1.3.1)

All are separate branches based on the PR heads they extend; none is merged and no existing PR branch was changed.

| Branch | Base | Content |
|---|---|---|
| `claude/dcim01-pr70-clamd-fail-closed` | #70 `b9ee791` | strict, bounded clamd reply parser; 57 protocol tests (fake TCP peer; 69 with the 12 existing), 6 HTTP upload cases, real `clamd` framing and size-limit controls |
| `claude/dcim01-pr67-strict-authority` | #67 `808707a` (reuses candidate `claude/pr67-equal-authority-fix-5vg7ee`) | strict outranking, post-state validation, self-membership prohibition, serialised authority changes; HTTP-only regression suite plus independent-session races |
| `claude/dcim01-pr68-import-job-acceptance` | #68 `e150222` | acceptance matrix over real HTTP (72 operation checks); contract conclusion in `docs/USER_GROUP_MANAGEMENT.md` |
| `claude/dcim01-pr69-nonvacuous-authz-matrix` | #69 `d26cc2d` | object matrix with valid payloads and exact denials (31 tests), exact route sweep (4 tests), ten mutants all caught |
| `claude/dcim01-pr71-lifecycle-verification` | #71 `ff19127` | tests only: row lock observed in PostgreSQL, lock held to commit, contention proven |

## 17. Code scanning evidence (corrected)

On the exact head of #70 (`b9ee791`) the two `github-advanced-security` runs of 2026-10-01 (06:03 UTC) are dynamic runs named "Code scanning AI findings on PR #70", not repository workflows. Both fail in the step "Processing Request" with `CAPIError: 400 The requested model is not supported`, before any file is analysed; the configured agent model in both is `claude-opus-5`. That is a tooling failure and not a clean or a failed scan.

The runs that report success on #71 and #79 are not evidence of analysis: the #71 log shows no model call (`Sessions disabled: not supported for code scanning yet`, then results reported) and the same model setting. Treat the check as unverified for every PR until a run completes analysis.

Mandatory protection visible to the audit: the active ruleset "Default Policy" on the default branch requires linear history, signed commits, a pull request (0 approvals, conversation resolution), and the status checks `backend` and `frontend` from GitHub Actions, up to date with `main`. `github-advanced-security` is not one of the required checks, and #70 targets a feature branch, so no ruleset applies to its base today. A dynamic run cannot be re-run through the API (HTTP 403 "cannot be retried"); the repair is the owner selecting a supported model for the code-scanning agent in the repository or organisation settings, then pushing a commit to trigger a new run. Scanning must not be disabled meanwhile.

## 18. Merge state of #71

`mergeable_state` is `behind` (4 commits behind `main`, documentation only), and the ruleset requires branches to be up to date. The commits on the branch are not verified: `ff19127` has author and committer `t <t@t>` (reason `no_user`), and `dc858eb` and `9bbd979` are unsigned. The ruleset requires signed commits and has an extra-approval rule for unattributed changes whose exact effect could not be read. A squash merge by GitHub produces a GitHub-signed commit; whether that satisfies the unattributed-changes rule could not be confirmed from here. No requirement was bypassed.

## Revision log

| Version | Date | Change |
|---|---|---|
| v1.3 | 2026-09-30 | PR #70, PR #71, findings #72 to #75, migration lineage |
| v1.3.1 | 2026-10-01 | scanner finding A, import-path finding B, corrected statements in sections 10, 13 and 15, remediation branches, code-scanning evidence, #71 merge state |
