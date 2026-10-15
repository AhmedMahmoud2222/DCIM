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

Reconciled against `origin/main` HEAD `47141e825...` (original PR base); re-confirmed
unchanged on `origin/main` `0b7df185dc50eb260e11d5e2e9173a5ab1fd0749` (2026-10-15, branch updated
by merge, no force-push). Since PR #91 (`e319392f`) the only change to the three guard files is
PR #127 (#63), which altered only the wording/docstring of `assert_administrator_remains`; the
guards below and `test_pr59_delegation_boundary.py` are byte-identical to PR #91. Note: the
test results below were produced at the original base, not re-run locally on the new base
(CI at the updated head is the fresh evidence). Original reference — see `gh pr view`/`gh
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

## Update 2026-10-15 — re-verification against `main` `1aa77f6c5c31e555905b5d126e4e179e3a0bd2d2` (includes #134)

PR #134 (SEC-ARCH-01 / #57) merged after the review above. This section separates what was
already verified from what is new.

### Previously verified (unchanged, not re-run locally)
- The 13-test delegation-boundary module and the 104-test RBAC/authority surface were run at
  `main` `47141e83` (see above). Those results are not re-claimed for the new base.

### New: code-level reassessment of #134 (reading, not execution)
- `groups.py`, `users.py`, `user_admin_service.py` and `test_pr59_delegation_boundary.py` are
  **not modified** by #134. The #64 guards (`scope_contains`, `assert_can_assign_group`,
  `assert_can_grant_sites`, `assert_not_changing_own_membership`) are unchanged.
- The guards compare against `ctx.scope`, which comes from `load_effective_access`. #134 changes
  that function: a non-global `organization` RoleAssignment adds every site under its
  organization as `rack_scope=all` to the holder's reach, only when no global role exists, the
  user names exactly one organization, and all group sites already lie inside it.
  Consequences for #64: an actor's delegation ceiling is their own effective reach, so an
  organization-assigned actor can delegate within, and only within, sites it genuinely holds.
  Selected-rack actors without an organization assignment are unaffected (the widening path
  requires the assignment).
- Non-global RoleAssignments cannot be created through the API (`users.py` only writes
  `scope_type="global"`), so the organization widening requires a database-level assignment and
  is not reachable by a rack-limited delegated admin acting through the routes.
- Scoped role names are dropped when not contained (`Administrator` checks), and organization
  assignments never confer role names or `unrestricted`, so they cannot satisfy the
  `assert_can_assign_role` / last-administrator logic.
- Migration `0054` only swaps the `role_assignment.scope_type` check constraint
  (`building` -> `organization`) and refuses to run if `building` rows exist; it does not touch
  group/site-access tables.

### New: execution evidence
- No local test execution was possible for this update (Docker daemon unavailable; no local
  PostgreSQL/Redis). **No claim is made that the 13 tests were re-run locally on `1aa77f6c`.**
- Execution evidence for the updated head is the repository CI (backend suites on Python
  3.12/3.13/3.14 with PostgreSQL), which includes `test_pr59_delegation_boundary.py`,
  `test_sec_arch_01_role_assignment_scope.py` and the 0054 migration test. CI results are
  recorded on the PR.
