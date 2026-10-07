# Stabilization and release-readiness pass (v1)

Date: 2026-10-07. Baseline: `main` `7364e68626d5b27f1698d0d7268d74a0026dedb3`, fetched live from GitHub before any work. This document records verified facts only. Historical phase reports are untouched.

Authorship note: the code fixes referenced here were written by Claude. They have not had independent non-author review, so nothing here counts as acceptance of those changes.

## 1. Live baseline

| Item | Verified state |
|---|---|
| `main` | `7364e68626d5b27f1698d0d7268d74a0026dedb3` (#120, 2026-10-07). CI run 37565234006 and Deployment validation run 37565233967 both succeeded |
| Open PRs | #110 (R3/#101, draft), #111 to #116 (drafts for #102 to #107), #117 (action version comments, ready), #118 (review report, draft), #87 (docs), #83, #69, #67 (older review/test branches whose bases are not `main`) |
| PR #110 | Draft, base `main` `7364e68`, head `e7f5f2391c55e74458cd0d30e3af12e770a13ad4`, 19 commits, 115 files, +15826/-49. Merge state `blocked` |
| PR #110 checks at head | Success: Deployment validation gate, Compose smoke, Deployment tooling regressions, Disposable combined-stack startup and rollback, edge-collector, edge-interop, frontend, browser-e2e. **Failure: `backend`, `backend suite (Python 3.12)`, `(3.13)`, `(3.14)`** (step "Run regression tests"), and `github-advanced-security` |
| Open issues | #38, #51, #52, #57, #63 to #66, #72 to #74, #99, #101 to #107 |
| Ruleset | One active ruleset, "Default Policy", on the default branch. Rules: deletion block, non-fast-forward block, linear history, pull request (**0 required approvals**, dismiss stale reviews, thread resolution required), required status checks `backend` and `frontend` (strict). Bypass: five GitHub App integrations and the repository owner, all `always` |
| Deployment workflow | `.github/workflows/deploy.yml`, "validation only". See section 8 |

## 2. Idempotency stale-owner race (NEW-1): confirmed and fixed

**Confirmed on current main.** `complete_claim()` flipped the claim row by primary key with no ownership predicate, `release_claim()` deleted by id and `status = 'processing'` only, and the stale reclaim only refreshed `updated_at`. A script against PostgreSQL 16 on unmodified main reproduced it: request A claims, the claim is made stale, request B reclaims and completes with `{"owner": "B"}`, A resumes, calls `complete_claim()`, and the stored result becomes `{"owner": "A"}`.

**Fix (fencing token).** New column `idempotency_key.claim_generation` (integer, default 1, check `>= 1`, migration `0037_idempotency_claim_fencing`). The reclaim CAS compares the generation it read and increments it in the same `UPDATE`. `complete_claim()` and `release_claim()` now match on `id`, `status = 'processing'` and the caller's generation. A fenced-out owner gets `IdempotencyClaimLost` from `complete_claim()`, so its transaction (domain write included) rolls back, and its `release_claim()` deletes nothing. `IdempotencyClaimLost` maps to a retryable 503 problem response. The timeout is unchanged.

**Tests** (`backend/tests/integration/test_idempotency_fencing.py`, PostgreSQL, no sleeps for staleness): normal claim, complete and replay; reused key with a different body; fresh claim waits and times out; stale recovery bumps the generation; old owner cannot complete after reclaim; old owner cannot overwrite the new owner's completed result; old owner cannot release the new owner's processing or completed row; owner release lets a retry claim fresh; eight concurrent reclaimers produce exactly one owner; a completion holding the row lock while a reclaim waits leaves one winner; a rolled-back completion leaves a recoverable claim; claim loss maps to 503. Removing the generation predicate from `complete_claim()` and `release_claim()` makes two of these tests fail, so they are not vacuous.

**Sequencing risk.** PR #110 adds migrations `0037` to `0040` on the same parent. Whichever PR merges second must renumber; the Alembic single-head gate will catch it. PR #110 also calls `release_claim(db, claim_id)` and `_release_claim_safely(db, claim_id, ...)` in `collectors.py`; the fix changes both to take a `ClaimRef`.

## 3. Scoped RBAC functional completeness

Finding: the code fails closed. For a user without a `global` role assignment, `load_effective_access()` keeps only `SCOPE_AWARE_PERMISSIONS` (`organization:read`, `location:read`, `rack:read`, `rack:manage`, `rack:place`, `equipment:read`) and `user:*`/`group:*`. Everything else lands in `inactive_permissions` and `require_permission()` returns 403. Issue #57 is stale as a vulnerability. What remains is a product gap: a site-restricted operator cannot use alarms, telemetry, power, floor plans, equipment mutation, collectors, integrations, discovery, impact simulation, the dashboard, imports or the catalog.

The full route-by-route matrix (195 operations, permission, active-for-restricted, scope guard) is in [RBAC_SCOPED_OPERATIONS_MATRIX_v1.md](RBAC_SCOPED_OPERATIONS_MATRIX_v1.md). Summary by family:

| Family | Permissions | Active for restricted? | Natural scope source | Needs new scope-aware work? |
|---|---|---|---|---|
| Locations, organization (read) | `location:read`, `organization:read` | Yes, filtered | site ancestry | No |
| Racks | `rack:read/manage/place` | Yes, object guards | current placement site, per-rack grants | No. `rack:import` is inactive |
| Equipment read | `equipment:read` | Yes, filtered | placement in a visible rack or room of an `all` site | No |
| Equipment create, update, instantiate, move, retire, port connect, import | `equipment:manage/place/import` | No | rack or room visibility | Yes. Unplaced equipment has no site, so ownership needs an owner decision |
| Managed assets | `managed_asset:*` | No | none (asset has no site column) | Yes |
| Alarms, events | `alarm:read/manage` | No | `Alarm.managed_asset_id`, `integration_id` to `Integration.site_id` | Yes |
| Telemetry, monitoring settings | `telemetry:read/manage` | No | `TelemetryReading.managed_asset_id`, `integration_id` | Yes. `GET /settings/monitoring` is global configuration |
| Dashboard | `dashboard:read` | No | aggregates over all sites | Yes. Every aggregate query needs a scope filter |
| Power, capacity | `power:*`, `capacity:*` | No | power node room or owning asset; connections can span sites | Yes. Upstream and downstream traversal must mask hidden hops |
| Impact simulation | `power:read` | No | same graph | Yes, after power |
| Floor plans, spatial | `floor_plan:*`, `spatial:read` | No | `FloorPlan.room_id` | Yes. With `selected` rack scope a room view must hide other racks' objects |
| Collectors, integrations | `collector:*`, `integration:*` | No | `Collector.site_id`, `Integration.site_id` (null means central) | Yes. Central (null-site) rows need a rule |
| Discovery, reconciliation | `discovery:*` | No | integration site | Yes. Accept creates inventory, so it depends on equipment scoping |
| Bulk import | `rack:import`, `equipment:import`, `catalog:import` | Create inactive; job read, commit and cancel limited to the uploader | row target room or rack | Yes for rack and equipment create |
| Catalog | `catalog:*` | No (global reference data; legacy `GET /equipment-models` and `/rack-models` use `equipment:read`/`rack:read` and are active) | none | Owner decision: treat read as scope independent |
| User, group admin | `user:*`, `group:*` | Yes | delegation containment | No. Covered by #63 to #66 tests |

**Decomposition (each step is its own PR; a permission joins `SCOPE_AWARE_PERMISSIONS` in the same PR that adds list, get, create, update and delete enforcement, a cross-site negative test per verb, and a route-sweep assertion):**

1. Resolver foundation: `integration_visible_clause`, `asset_visible_clause` (equipment, rack, power node, unmanaged), partial-visibility masking helper reused from the #110 trace service. No permission changes.
2. Read paths: `telemetry:read`, `alarm:read`, `dashboard:read` (scoped aggregates), `capacity:read`.
3. Collectors, integrations, discovery read (`collector:read`, `integration:read`, `discovery:read`). Owner rule for central collectors.
4. Floor plans and spatial read, including per-object filtering for `selected` rack grants.
5. Power read and impact simulation with masked traversal.
6. Mutations: alarm rules and acknowledgement, telemetry bindings and mappings, collector and integration management, discovery reconcile.
7. Equipment and rack mutation, placement and import. Blocked on the owner decision for unplaced equipment ownership (add a site owner column, or require creation directly into a visible rack or room).
8. Catalog read and managed-asset endpoints, per owner decision.

Owner decisions needed: central (site-less) collectors and integrations; ownership of unplaced equipment; catalog read for restricted users; whether the dashboard shows only scoped totals.

## 4. Stale issue reconciliation against `main` 7364e68

Verdicts use the requested vocabulary. No issue was closed or edited.

| Issue | Verdict | Evidence |
|---|---|---|
| #63 rank check ignores scope, group-membership route skips it | REMEDIATED BUT ISSUE STALE | `user_admin_service.assert_actor_outranks_users`, `assert_can_modify_group`, `assert_group_visible` (`user_admin_service.py:346-378`); `groups.py` calls them on update, delete, membership and permission routes (lines 258-387). Tests: `tests/api/test_pr59_delegation_boundary.py` (peer, wider scope, deny-group lockout, group-route membership). Commits `e319392` (#91), `2cc4750` (#88) |
| #64 group assignment and site grants widen rack scope | REMEDIATED BUT ISSUE STALE | `scope_contains`, `assert_can_assign_group`, `assert_can_grant_sites`, `assert_not_changing_own_membership`. Tests: `test_rack_limited_admin_cannot_assign_a_group_with_wider_rack_scope`, `..._add_self_to_wider_group_through_group_route`, `..._grant_all_racks_on_a_group`, plus the allowed-within-scope cases |
| #65 delegated admin alters and reads other sites' groups | REMEDIATED BUT ISSUE STALE | Same files. Tests: `test_site_a_admin_cannot_modify_or_delete_a_site_b_group`, `test_restricted_admin_cannot_read_other_sites_grants`. The issue's residual (directories stay readable) remains an owner decision |
| #66 import jobs have no site or owner check | REMEDIATED BUT ISSUE STALE | `bulk_import._assert_job_visible` (`bulk_import.py:50-72`) returns 404 to restricted non-uploaders on read, commit and cancel. Tests: `tests/api/test_pr59_import_job_scope.py` |
| #72 PDF active content in compressed object streams | REMEDIATED BUT ISSUE STALE | `catalog_documents/pdf_validation.py` walks `xref_objStm`. Tests: `tests/unit/test_pr70_pdf_hostile.py` (hidden content rejected, clean control accepted, `/URI` allowed). Landed in `1ba8d17` (#93) |
| #73 two Alembic heads from #59 and #70 | REMEDIATED BUT ISSUE STALE | Both merged on one chain; `alembic heads` returns a single head; the CI gate `backend/scripts/check_alembic_single_head.py` (`a7d7594`, #79) runs in the `backend` job. Note: PR #110 and the idempotency fix both add `0037`, so the gate will fire again until one is renumbered |
| #74 four catalog document hardening gaps | REMEDIATED BUT ISSUE STALE | (1) audit stores `sanitize_filename(...)` (`catalog_documents.py:147`), test `test_malware_rejection_audit_does_not_store_a_raw_hostile_filename`; (2) duplicate lookup uses `with_for_update(read=True)` (`service.py:63`), test `test_duplicate_upload_never_reports_a_document_the_purge_is_deleting`; (3) `sweep_orphan_objects` (`service.py:197`), test `test_object_left_by_a_failed_transaction_is_eventually_swept`; (4) `skipped` documents refused when scan mode is `required` (`catalog_documents.py:262`), test `test_documents_that_were_not_scanned_in_required_mode_are_not_served_in_production`. All in `1ba8d17` |
| #99 canonical units and metric registry | REMEDIATED BUT ISSUE STALE (implementation). Owner should confirm the independent-review acceptance criterion | Commit `754484d`: migration `0035_units_metric_registry`, `domain/telemetry/registry.py`, ADR-0012, round-trip and migration tests (`test_physical_units.py`, `test_units_metric_registry_migration.py`). The issue also requires independent review; this pass found no recorded one |
| #57 RBAC global only | PARTIALLY REMEDIATED (security) / OPEN (functional completeness) | Section 3. Fail-closed enforcement exists; scoped operations remain unavailable |
| #51 pool recovery | OPEN AND VALID before the fix; see section 6 | |

Recommended issue comments (for the owner to post; nothing was posted):

* #63 to #66: "Verified on main 7364e68. The behavior described here is covered by `tests/api/test_pr59_delegation_boundary.py` / `test_pr59_import_job_scope.py` and the code in `user_admin_service.py` and `bulk_import.py`. Suggest closing as fixed by #91 and #59 after the owner confirms the #65 residual (readable user and group directories)."
* #72 to #74: "Verified on main 7364e68. Each item has a passing regression test in `tests/unit/test_pr70_pdf_hostile.py` and `tests/api/test_pr70_hostile_api.py`. Suggest closing as fixed by #93."
* #73: "Single head on main; guarded by `check_alembic_single_head.py` in CI. Suggest closing."
* #99: "Implementation merged in `754484d`. Closing needs the independent review the acceptance criteria require."

### Issue #38, item by item

| Item | Verdict | Evidence |
|---|---|---|
| SEC-05 nonce and heartbeat retention | Resolved | PR #46; `infrastructure/tasks/maintenance.py` bounded retention, `docs/SEC05_COLLECTOR_RETENTION.md` |
| SEC-07 ingest log sanitization | Resolved | PR #45 and #49; `collectors.py` logs fixed event names only (`ingest_batch_record_processing_failed`), claim-release and rollback failures contained |
| SEC-01 telemetry batch transaction boundary | Partially superseded. Open as a contract decision | Since `754484d`, `ingest_collector_telemetry` runs each record in `begin_nested()` and rejects known domain errors per record. An unexpected exception still rolls back the whole request. Owner decides whether all-or-nothing stays the contract |
| SEC-02 static Fernet key | Open architecture limitation | `core/secrets.py` builds one `Fernet` from `credential_encryption_key`. No key version tag, no rotation tooling, no KMS |
| SEC-04 SNMPv2c cleartext | Open on `main`; handled in draft PR #110 | `edge_collector/snmp.py` has only `SNMPv2cCollector`. PR #110 adds an SNMPv3 authPriv stack and an interoperability harness. It is not merged and its backend checks fail |
| SEC-06 unencrypted edge SQLite queue | Open deployment decision | `edge_collector/queue.py` unchanged. No LUKS or SQLCipher requirement in `docs/OPERATIONS.md` |

## 5. PR #110 current-head review (head `e7f5f23`)

Independence: 10 of the 19 commits are authored as Claude, so this review is not independent acceptance. A non-Claude reviewer must accept the final head.

**Blocking: the exact head is red.** `backend` and all three `backend suite` jobs fail in "Run regression tests". Every other job, including edge-interop and browser-e2e, passes. I ran the full backend suite against the head on PostgreSQL 16: 1891 passed, 10 skipped, 4 failed. Three failures are deterministic and share one cause. The `/api/v1/pass-throughs` routes added by the last commit (`e7f5f23`, B5/B6) are reachable by site-restricted users but are missing from the reviewed sets in `tests/api/test_pr69_route_sweep_strict.py` (`test_the_reachable_set_is_exactly_the_reviewed_set`, `test_the_sweep_detects_a_route_that_wrongly_becomes_reachable`) and `tests/api/test_user_groups_authz.py::test_every_route_using_a_scope_aware_permission_has_been_reviewed`. These exact-set guards did their job: the new routes need an explicit review entry, plus cross-site tests, before the sets are updated. The fourth failure, `test_login_throttle.py::test_repeated_failed_logins_are_throttled`, passed when re-run alone and probably reflects shared Redis state in my environment; CI's unreadable log could not confirm it.

What I checked and found sound:

* **USM correctness** (`edge_collector/snmp_usm.py`, `snmp_v3.py`): RFC 3414 password-to-key and localization, RFC 7860 truncated MAC lengths (12/16/24/32/48), RFC 3826 AES-CFB IV (`boots || time || salt`), constant-time MAC comparison, MD5, DES, noAuthNoPriv and authNoPriv refused, AES-192/256 only with a hash at least as long as the key, secrets excluded from `repr`.
* **Response handling**: strict bounded BER cursor, MAC verified over the zeroed message, engine ID, msgID, request ID, user, context engine, context name and time window (150 s, boots must match) all checked, and non-matching datagrams dropped instead of aborting the poll. `not_in_time_window` resynchronizes only on an authenticated report.
* **Real wire tests**: `edge-interop` runs the stack against pysnmp over loopback and passed at this head.
* **Hostile neighbor isolation**: neighbor records are bounded per record (`INVALID_PAYLOAD` instead of a batch-level 422), run in savepoints, and a rejected record releases its claim; text is control-character stripped and length bounded.
* **Authority**: neighbors become cables only through an explicit action that needs `discovery:reconcile` and `cable:manage`. Cable mutation requires both endpoints visible; a partly visible cable masks the far end; the trace service returns `restricted` hops with no identifiers. `cable:read` and `cable:manage` join `SCOPE_AWARE_PERMISSIONS` together with `test_cables_scope.py`.
* **Migrations**: `0037` to `0040` upgrade, downgrade to `0036`, and re-upgrade cleanly on an empty database (run twice). Data-bearing downgrade was not exercised.
* **Edge suite**: `pytest edge_collector/tests` passes locally at the head.

Findings and observations (non-blocking unless noted):

1. **Unauthenticated Report PDUs can abort a poll.** An on-path host that sees the cleartext `msgID` can forge an unauthenticated `usmStatsWrongDigests`, `usmStatsUnknownUserNames` or `usmStatsDecryptionErrors` report and make the session raise `SNMPv3AuthenticationError`. The module docstring says an attacker can only delay a poll. Either adjust the claim or retry before raising on an unauthenticated report. Impact is denial of one poll.
2. **Bidirectional and invisible Unicode survives `clean_text`.** The control-character regex does not remove U+202A to U+202E, U+2066 to U+2069 or zero-width characters, so a hostile LLDP system name can spoof how a neighbor renders in the UI. Hardening only.
3. **Migration sequencing.** See section 2: migration numbers collide with the idempotency fix.
4. **Idempotency API change.** `collectors.py` in this PR uses the pre-fix `release_claim` signature and will conflict with the fix.
5. **GHAS failure.** `github-advanced-security` fails at the head; the cause was not retrievable through the available API proxy. Read the check before acceptance.

Not verified in this pass: browser-level frontend workflow beyond the green browser-e2e job, concurrency of duplicate discovery replays beyond the PR's own tests, regression of v2c collectors beyond the existing edge and backend suites.

**Is #110 technically ready for independent final acceptance?** No. The head fails four required-class backend checks. The route-sweep sets must be updated with a recorded review of the pass-through routes (list, get, create, delete under `cable:read`/`cable:manage`), CI must go green on the exact head, the GHAS failure must be read, and a non-Claude reviewer must then review that head.

## 6. Pool recovery when snapshots and cleanup fail (Issue #51)

**Reproduced.** With `pool_size=1`, `max_overflow=0`, `AsyncSession.connection()` failing, and `Transaction.rollback`/`Transaction.close` failing, `get_db()` logs `db_session_cleanup_snapshot_failed` and `db_session_cleanup_rollback_failed`, re-raises the route's own exception, and leaves `pool.checkedout() == 1`. A following request waits for the pool timeout and raises `TimeoutError`. The slot returns only when the session object is garbage collected (SQLAlchemy then terminates the connection and emits an `SAWarning`).

**Fix.** `app/db/session.py` records the sync `Connection` in the session's public `info` dict from an `after_begin` event. When the pre-failure snapshot is unavailable, cleanup invalidates that remembered connection through `session.run_sync(...)`. It skips a connection that is already closed, so it never touches a pooled connection another request owns. Only public SQLAlchemy APIs are used and no exception text is logged.

**Tests** (`tests/integration/test_db_session_cleanup.py`): both snapshot-fail paths with rollback and close failing (exception path and successful-response path) free the single slot, the next request succeeds within 5 s, fixed events are logged, no synthetic secret appears in the log; the fallback leaves an already released connection alone (same `pg_backend_pid()` after reuse).

## 7. Repository governance

Live ruleset: required checks `backend` and `frontend` only; 0 required approvals; bypass `always` for the owner and five App integrations.

Checks that exist and always run on a PR to `main`: `backend`, `backend suite (Python 3.12)`, `(3.13)`, `(3.14)`, `frontend`, `edge-collector`, `browser-e2e` (CI workflow), `Compose smoke` and `Deployment validation gate` (Deployment validation workflow). `Deployment Tools Test` is path-filtered and cannot be required. `edge-interop` exists only in PR #110.

Recommended required checks: `backend`, `backend suite (Python 3.12)`, `backend suite (Python 3.13)`, `backend suite (Python 3.14)`, `frontend`, `edge-collector`, `browser-e2e`, `Deployment validation gate`. Add `edge-interop` after #110 merges.

Recommended review policy: at least 1 approving review from someone who is not the PR author and not an automated agent that wrote the change; approval of the latest push required; stale approvals dismissed (already on); code-owner review for `backend/app/application/access_control.py`, `rbac.py`, `idempotency.py`, `backend/migrations/`, `edge_collector/` and `.github/`; bypass limited to "pull request" mode for emergencies with a logged reason. A bypass actor merging does not count as review. These are recommendations. No rule was changed.

## 8. Deployment readiness truth

See the table in [PROJECT_STATUS.md](PROJECT_STATUS.md#deployment-status-verified-2026-10-07-from-github-and-repository-source). In short: the disposable CI Compose stack is validated; the deploy script exists and accepts only `staging`; no persistent staging deployment, no deployed SHA, no production support and no production deployment are evidenced. `deploy.yml` validates the SHA and runs `docker compose config`; it starts no services.

## 9. Validation record

Environment: PostgreSQL 16, Redis 7, Python 3.13 on a local runner. GitHub CI has not run on the fix branches yet.

| Check | Result |
|---|---|
| `ruff check app tests`, `mypy app` (fencing and pool changes together) | clean |
| Alembic single head; `0037` upgrade, downgrade to `0036`, re-upgrade (twice) | pass |
| Focused: `test_idempotency_fencing.py`, `test_idempotency_concurrency.py`, `tests/unit/test_idempotency.py` | 20 passed |
| Mutation check: removing the generation predicate from `complete_claim`/`release_claim` | 2 tests fail, as intended |
| Pool tests (`test_db_session_cleanup.py`, including the 3 new) | included in the full run |
| Full backend suite on main with both fixes applied (excluding the hostile retention file, as CI runs it separately) | 1697 passed, 10 skipped |
| PR #110 head, full backend suite | 1891 passed, 10 skipped, 4 failed (section 5) |
| PR #110 `edge_collector/tests` | all passed |
| Frontend, browser E2E, Compose | not re-run; no frontend, edge or deployment code changed in the fix branches |

Branches: `claude/stabilization-idempotency-fencing` (`0e2e099`), `claude/stabilization-pool-recovery-51` (`c7e6cfa`), `claude/stabilization-docs-v1`. No PRs were opened. Both code branches need independent non-Claude review.
