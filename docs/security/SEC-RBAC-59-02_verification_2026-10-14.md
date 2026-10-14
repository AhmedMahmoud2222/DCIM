# SEC-RBAC-59-02 (Issue #64) — verification report, no code change

**Scope:** Issue #64 only. Issue #63 (SEC-RBAC-59-01) is already closed with its own
acceptance verdict. Issue #65 (SEC-RBAC-59-03) is open under a separate owner assignment
and is explicitly **not** addressed, deployed, or closed by this PR.

## Summary

Issue #64 was originally filed against PR #67's head. PR #67 was later closed unmerged,
superseded by PR #91 (merged 2026-10-04 as `e319392f8c41c02159037e451865f68a532accbf`),
which carried the strict-outranking / serialised-authority remediation onto main. Issue
#63's own closure comment already credits PR #91 (+ PR #127) for the related rank/authority
fix, but issue #64 itself was never given an explicit acceptance verdict the way #63 was.

This PR adds **no application code change**. Its purpose is to supply the fresh,
exact-HEAD evidence the owner's execution gate requires before #64 can close, and to give
an independent reviewer (Jules) a concrete artifact to review.

## What was checked

Reconciled against `origin/main` HEAD `47141e825...` (see PR base) — see `gh pr view`/`gh
issue view` output for #59, #63, #64, #65, #67, #91, #127 for the full reconciliation
(not reproduced here). PR #67 confirmed closed/superseded; issues #69/#83 (stale, stacked
on the pre-#91 base, refs #57) and #134 (SEC-ARCH-01/#57) confirmed out of scope for #64.

All three affected call sites named in #64 were inspected directly in
`backend/app/application/user_admin_service.py`:

- `assert_can_assign_group` (around line 181) and `assert_can_grant_sites` (around line
  150) both route through `scope_contains()` (around line 86), which compares rack scope
  **per site** (all vs. selected, including current rack placement), not just site ids.
- `assert_not_changing_own_membership` (around line 312) unconditionally blocks a user
  from adding or removing themselves from a group, closing the self-join path #64
  described.
- `groups.set_site_access` and `groups.set_members` (backend/app/api/v1/groups.py) and
  `users.create_user` (backend/app/api/v1/users.py) all call these guards.

## Fresh test evidence (not merely a code reading)

A disposable PostgreSQL 16 + Redis 7 pair, fully isolated from any other session's
database, was stood up locally and migrated to head, then:

```
pytest -q tests/api/test_pr59_delegation_boundary.py
# 13 passed in 69.82s
```

All three original SEC-RBAC-59-02 hostile reproductions pass:
`test_rack_limited_admin_cannot_assign_a_group_with_wider_rack_scope`,
`test_rack_limited_admin_cannot_add_self_to_wider_group_through_group_route`,
`test_rack_limited_admin_cannot_grant_all_racks_on_a_group` — plus the positive controls
for legitimate subset delegation and unrestricted-administrator behavior.

Broader regression surface (RBAC, API, PostgreSQL integration):

```
pytest -q tests/api/test_pr59_delegation_boundary.py tests/api/test_user_groups.py \
  tests/api/test_user_groups_authz.py tests/api/test_pr59_import_job_scope.py \
  tests/api/test_pr91_write_visibility.py tests/unit/test_pr91_write_visibility.py \
  tests/api/test_bulk_import_rbac.py tests/api/test_catalog_rbac_closure.py \
  tests/api/test_spatial_authority.py tests/integration/test_pr67_authority_lock_protocol.py \
  tests/integration/test_pr67_authority_relocation_race.py \
  tests/integration/test_pr67_authority_serialization.py
# 104 passed, 19 skipped, 0 failed, 0 errors in 438.66s
```

`ruff check app tests` — all checks passed.

### A methodological note on flakiness ruled out

An initial run against a database shared with another concurrent local session (also
working in this repository, on issue #65) produced two different spurious failures
(a stale SQLAlchemy instance-refresh error, then an unrelated 401) on two different test
functions across two attempts. Re-running against a database and Redis instance
dedicated solely to this verification — nothing else attached — passed cleanly and
reproducibly. The two spurious failures are recorded here for transparency but are not
evidence of a defect in this code path: they do not reproduce in isolation.

## Acceptance criteria from Issue #64

> The three tests pass; an `all`-scope administrator can still assign `all` and
> `selected` groups; a `selected` administrator can assign only groups whose racks are
> within their own set.

All satisfied per the evidence above.

## Request

@jules — please perform an independent security, database, and regression review of
this PR at its exact head, covering whether the no-code-change conclusion above is
correct (i.e., whether the rack-scope containment #64 asked for is genuinely already
enforced on current main) and whether the test evidence is sufficient to close #64.
Per the owner's gates, #64 will not be closed, and nothing will be merged, until that
review records an explicit ACCEPTED verdict at this exact head.
