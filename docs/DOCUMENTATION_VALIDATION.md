# DCIM documentation validation

**Date:** 2026-11-13  
**Baseline:** `main@b6947782571d07c84c3fbd899ef5f28f484cdb04`  
**Branch:** `docs/history-link-integrity-20261113`

## Validator

`scripts/check_docs.py` — offline Markdown link validator with no third-party dependencies.

Behavior:
- Checks every tracked Markdown document's local links, images, heading anchors and HTML `href`/`src`.
- Verifies path and case must match against `git ls-files`; confirms the target exists in the worktree.
- Detects undefined reference labels, paths escaping the repository, and missing tracked targets.
- Skips external URLs (http/https/git/mailto) and code blocks / inline code / HTML comments (not navigable).
- Excludes bundled third-party documentation under `.claude/skills/archify/` (maintained upstream).
- `path_references()` inventories literal `.md` paths in all tracked text (including source-code comments), but reports them separately as `REVIEW` findings so intentional negative-test fixtures and code citations are not reported as production documentation defects.

Exit codes:
- `0` — no confirmed broken internal links.
- `1` — one or more confirmed broken internal links (validation fails).

## Results — full-repository validation

Command: `python scripts/check_docs.py --json`

| Metric | Value |
|---|---|
| Tracked Markdown documents | 167 |
| Checked documents (excludes 24 archify vendor docs) | 143 |
| Local references checked | 253 |
| External references skipped | 80 |
| Confirmed broken internal links | **0** |
| Exit code | 0 |

Command: `python scripts/check_docs.py --path-report`

The path/literal-reference inventory reports 0 unresolved literal `.md` paths in tracked text after the reconciliation; relocated reports are recorded in `docs/documentation-moves.json`. Any remaining literal citations are code-comment references that were updated to their new `docs/history/` paths.

Command: `python -m unittest discover -s tests/ci -p test_check_docs.py -v`

| Test | Result |
|---|---|
| test_valid_local_links_images_titles_and_encoded_spaces | pass |
| test_missing_file_wrong_relative_case_and_anchor_fail | pass |
| test_reference_styles_and_undefined_label | pass |
| test_html_anchors_and_links | pass |
| test_duplicate_setext_unicode_and_code_headings | pass |
| test_balanced_parentheses_and_angle_destination | pass |
| test_code_comments_external_and_vendor_are_excluded | pass |
| test_untracked_file_cannot_hide_missing_tracked_target | pass |
| test_cli_exit_status_fails_on_deliberate_break | pass |
| test_source_path_report_finds_missing_and_moved_paths | pass |
| **Total** | **10 / 10 passing** |

## Review category (uncertain textual references)

Textual references whose status cannot be machine-confirmed (e.g., code-comment citations to dated reports, GitHub issue/PR URLs, commit SHAs referenced in prose) are separated into a documented review category and are NOT reported as validation defects. They are listed in `docs/documentation-moves.json` (relocated reports) and cross-referenced from `docs/HISTORY_AND_EVIDENCE.md`. Each should be interpreted against its original baseline SHA, not as a current-state claim.

## Justified exclusions

- `.claude/skills/archify/*` — bundled third-party tool documentation; excluded from link validation (maintained upstream).
- `docs/documentation-moves.json` — excluded from literal-path scanning to avoid self-reference.
- Migration 0057 evidence (`docs/FK_INDEX_*_0057.md`, `docs/benchmark-results/0057/`) — retained at current paths as active Issue #214 production-hold evidence; links to these files remain valid.

## CI workflow

`.github/workflows/documentation.yml` runs on pull requests and pushes to `main`:
1. Proves broken documentation is rejected (validator regression tests).
2. Validates tracked documentation (`python scripts/check_docs.py --path-report`).
