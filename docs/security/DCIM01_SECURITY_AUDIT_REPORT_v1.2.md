# DCIM01 Hostile Security Audit Report

**Version:** 1.2
**Date:** 2026-09-30 (v1.2 adds section 8, the PR #59 review; v1.1 dated 2026-09-29) (executed on demand; the 23:30 UAE scheduled trigger was disabled at the owner's request)
**Baseline:** `main` at `f94f22078ae0316b1706fb86ba6705a0a364de44`
**Open PRs at start:** #53 (docs only, head `674fc1bb74a4d8d8b2adbe8a84c5a533e2ea9ded`)
**Reviewer note:** this is a single-pass review by an automated agent. It is not an independent penetration test, and CI status was not used as evidence.

## 1. Scope actually covered

| Area | Depth | Method |
|---|---|---|
| Authentication, sessions, JWT, CSRF | Full read | Code read, live tests on PostgreSQL 16 and Redis |
| RBAC model | Full read of `rbac.py`, user creation route | Code read |
| Per-router authorization (all 20+ routers) | Sampled only | Not every route checked for a missing dependency |
| Collector HMAC trust boundary, replay, body bounds | Full read of `collector_auth.py`, ingest size checks | Code read |
| SVG / raster / XLSX upload handling | Full read of sanitizer, zip-bomb limits, storage backend | Code read |
| SQL injection | Grep for string-built SQL across `backend/app` and `edge_collector` | Every `text(` call is parameterized |
| Deserialization, shell, eval | Grep | No `pickle`, `yaml.load`, `subprocess`, `eval`, `shell=True` in application code |
| Frontend XSS / token storage | Grep of `frontend/src` | No `innerHTML`/`dangerouslySetInnerHTML`; access token in memory only |
| Dependencies | `pip-audit` on `pyproject.toml` (unpinned, latest resolution), `npm audit` | 0 known vulnerabilities in both |
| Secret scanning | `detect-secrets` on tracked files | Hits are placeholders, test fixtures, CI-only credentials; none verified as live |
| Static analysis | `bandit` on `backend/app`, `edge_collector` | 0 medium/high; low: `assert` use (B101), token-type string literals (B105/B106 false positives), `xml.etree` type import (B405, parsing uses defusedxml) |
| Docker / compose / nginx / workflows | Read | See findings |

## 2. Commits reviewed

Every remote branch, with its head SHA. "Ahead" counts commits not in `main`. Branches showing zero or squash-merged content (#47, #49, #50) are covered by the `main` review; the open work branches are listed in section 3.

| Branch | Head SHA | Last commit | Ahead of main |
|---|---|---|---|
| `security/sec-ci-01-workflow-permissions` | `2e024cbf09811555d7dfbce0158357062cbfaff4` | 2026-09-29 | 1 |
| `security/sec-auth-01-login-throttle` | `41b319007706bb600bd110f1edbf9c48b714e95d` | 2026-09-29 | 1 |
| `security/sec-auth-02-refresh-reuse` | `c57271fe9c2420fa256107f303e397c136312d58` | 2026-09-29 | 1 |
| `claude/pdf-datasheet-import-plan` | `eaadb00968719dcca75e18c825bc7383b2969ff8` | 2026-09-29 | 2 |
| `docs/consolidated-product-roadmap-20260929` | `674fc1bb74a4d8d8b2adbe8a84c5a533e2ea9ded` | 2026-09-29 | 3 |
| `main` | `f94f22078ae0316b1706fb86ba6705a0a364de44` | 2026-09-29 | 0 |
| `excel-bulk-import` | `c81438ffd54ad658f6d96937bb12ebdf1ebc24e1` | 2026-09-29 | 11 |
| `claude/dcim-phase-11-remediation-v95d8s` | `d8a98fd63442a0654943a50dbe6523e9ca3bd12b` | 2026-09-29 | 8 |
| `claude/restore-events-3d-layout` | `69d11df781746409a100e2fcc071e412c8ff9688` | 2026-09-28 | 3 |
| `claude/windows-docker-compat` | `27abe926d6923bead3f29a491deb89d1da6957b7` | 2026-09-28 | 2 |
| `codex/frontend-accessibility-v1` | `c14f79e32cfa5009e56e5f4dd109a8deaece3a09` | 2026-09-28 | 9 |
| `codex/sec05-nonce-heartbeat-retention` | `07f44b526f130ca7c6284cd24578db9ba4b5c5fa` | 2026-09-27 | 0 |
| `codex/sec07-ingest-log-sanitization` | `743cbb38508e16ea7a4d0a2886f305870e791185` | 2026-09-27 | 0 |
| `docs/phase11-edge-collector-security-reconciliation-8222213073254116158-8880952212103376471` | `5a28ba0fd306c008fc3eba10fb5b50c4e48b9f4f` | 2026-09-27 | 0 |
| `docs/dedicated-staging-vm-plan` | `4f54b1f2b809b814b3fa05e71cfd76bfe74f67c0` | 2026-09-27 | 0 |
| `docs/phase11-edge-collector-security-reconciliation-8222213073254116158` | `c74540e496983eea102c5d90934e95918aff0d82` | 2026-09-27 | 1 |
| `docs/deployment-governance-reconciliation` | `dacd7f01ef477d3b8f1c130936c4950a380727d8` | 2026-09-27 | 0 |
| `audit/phase10-post-merge-14423236032059353664` | `5c7eb5e691036e5ae30f4710bf0da10ea90cd5c2` | 2026-09-27 | 4 |
| `claude/intelligent-edison-94mvbo` | `06589f1c7692d76b667349812fb84fc83ed09666` | 2026-09-26 | 0 |
| `codex/pr35-disposable-staging-validation` | `6abac29c3f5cf04302006febe07f9caadbff6158` | 2026-09-26 | 0 |
| `docs/phase11-edge-collector-security-reconciliation` | `5dc249fac31bad05374ee3d0452bfb9635b27856` | 2026-09-26 | 3 |
| `codex/pr35-deployment-security-remediation` | `185bb01511eb36c77a2fa68e114c252c91883164` | 2026-09-26 | 0 |
| `deployment-gate-enforcement` | `6ac96cf7e8067acf1365ac8b47bbe648cacfd043` | 2026-09-26 | 2 |
| `codex/phase10-report-evidence-correction` | `e6d6b65f0fed7040de73e2f701bb17a12bf66a27` | 2026-09-26 | 0 |
| `codex/compose-smoke-gate-v1` | `48f371800832226f8bad88c31cebe00149cdba51` | 2026-09-26 | 0 |
| `docs/current-platform-guide-20260926` | `e00475703da38a50c793cc53e66295409a24e96c` | 2026-09-26 | 0 |
| `codex/phase10-ci-coverage-runtime-docs` | `c430f8445f1e82f941a6fb0192df4b7440335b15` | 2026-09-25 | 0 |
| `claude/phase10c-telemetry-impact-engine-uolue5` | `1c474e12e2dacd914de84be306746f9c1de3b3d4` | 2026-09-25 | 3 |
| `claude/phase10b-instantiation-rack-integration-47zpiv` | `89af59232bdf47c95a036a2e4b35cea8ac6b5559` | 2026-09-24 | 1 |
| `claude/phase10a-graphics-marker-editor` | `0d48e2675c9d9b0af418167aa9f1e7676f3429ff` | 2026-09-24 | 1 |
| `claude/phase10a-catalog-designer-ui` | `bd8aa857ae6527b33a835e571761b80d8b499433` | 2026-09-24 | 2 |
| `claude/phase10a-lifecycle-backend` | `5b1f59a52408c29d635964622aab6eafcdc5ae29` | 2026-09-24 | 8 |
| `claude/phase10a-docs-refresh` | `316181eae23c3b013eb1524dec7a517e400a3c76` | 2026-09-24 | 39 |
| `claude/phase10a-rbac-closure` | `75d19c6e3f2c6d031ba9b0cbc04312f4e03cb927` | 2026-09-24 | 31 |
| `claude/phase10a-upstream-reconcile` | `17766c96959a76718f87707ba1a0f1ab9c37b12a` | 2026-09-24 | 37 |
| `claude/phase10a-catalog-schema` | `f1f4416670b470829ab4f5343751d1d218fe4993` | 2026-09-23 | 29 |
| `claude/phase10a-catalog-designer-doc-alignment` | `022f7e12fc2fc605fb6448bbfda99beb44fdecda` | 2026-09-23 | 27 |
| `claude/phase10a-asset-catalog-designer-plan` | `5871d22c8b6c43334c0d33bbc28b8e5362ae9072` | 2026-09-23 | 26 |
| `codex/commercial-ui-uplift-v1` | `5f39b20b61a22bcd7ba7eb902f2e42b1ac51ac01` | 2026-09-23 | 23 |
| `claude/phase10a-asset-catalog-design` | `13165d3aa595272234dae7edd3587c6f997da468` | 2026-09-23 | 24 |
| `codex/implement-network-topology-and-3d-layout` | `6af51fe86197ff973305529a67f6fd26e7318e10` | 2026-09-23 | 21 |
| `fix/pr9-vitest-vite-security-reconciliation` | `ad3918f92e0999ba94fed9eb8c300659c44162f7` | 2026-09-23 | 20 |
| `fix/frontend-dependency-security` | `758322ade4a5c81f5627c34fd0eadca3ffbc8ac3` | 2026-09-23 | 1 |
| `Claude-Code-—-DCIM-Frontend-Security-Remediation` | `c4ef633d87ad7ed7a782d31104b5df4335763701` | 2026-09-23 | 16 |
| `codex/pr9-network-3d-interactions` | `c4ef633d87ad7ed7a782d31104b5df4335763701` | 2026-09-23 | 16 |
| `codex/verified-review-corrections` | `5b14e6e380915bfebf7dfafe4b09cc5019a90dc2` | 2026-09-19 | 2 |
| `codex/dcim-mvp-v0.1-signed` | `51c0cee777e73dd60fe875868e959162e0c59d21` | 2026-09-19 | 0 |
| `claude/pre-mvp-consolidation` | `3def6e9047c87c4f5b32ed46894ab6a41f39e62f` | 2026-09-18 | 1 |
| `codex/dcim-mvp-v0.1` | `f79689fcf3e2df889ff47364e84b1e504cf4712d` | 2026-09-18 | 3 |
| `claude/inspiring-edison-akoeoc` | `5b94bfab435f66da86cd9116e08a590f25c1b59c` | 2026-09-18 | 5 |
| `claude/phase8-independent-red-team` | `6af4e4ecec8f4bed3553cb35f16654d99f50c11c` | 2026-09-18 | 0 |
| `codex/phase8-finding-verification` | `b52aaeb54cbfb24fe3afe5a8a2168248130e46bb` | 2026-09-18 | 1 |
| `codex/phase8-independent-red-team` | `cb2f0f2add2eddca13698de8154b5306b84c2a5d` | 2026-09-18 | 1 |
| `claude/new-session-1vutvy` | `fc5fd57e59f71836828decb66dcdfb669f44b47c` | 2026-09-18 | 0 |

### Branch disposition
- `excel-bulk-import` (11 ahead), `claude/dcim-phase-11-remediation-v95d8s` (8 ahead), `claude/restore-events-3d-layout` (3 ahead): the content landed on `main` as squash commits #50, #49 and #47, so the review of `main` covers it. The branch heads themselves were not diffed line by line.
- `claude/pdf-datasheet-import-plan` (2 ahead): 2 files, 525 lines, planning documents only. PDF upload and parsing is **not implemented**, so it could not be tested.
- `codex/frontend-accessibility-v1`, `claude/windows-docker-compat`, `docs/consolidated-product-roadmap-20260929`: not security reviewed beyond a file-list and diff-size check (`.gitattributes` only for docker-compat).

## 3. Vulnerability matrix

| ID | Severity (CVSS est.) | Status | Component | Issue | Remediation PR | Test evidence | Outstanding |
|---|---|---|---|---|---|---|---|
| SEC-AUTH-01 | Medium (6.5) | **Confirmed**, fix proposed | `auth.py`, `auth_service.py` | #54 | #60 | 4 of 6 new tests fail on `main`, all pass with fix; full suite 811 passed | Per-IP limit, distributed guessing; fail-open on Redis outage |
| SEC-AUTH-02 | Medium (5.9) | **Confirmed**, fix proposed | `auth_service.py` | #55 | #58 | 2 of 2 new tests fail on `main` (6 concurrent refreshes all succeeded), pass with fix; full suite 806 passed | Benign double-submit now ends the session |
| SEC-CI-01 | Low (3.7) | **Confirmed** (missing `permissions:` in `ci.yml`), partial fix | `.github/workflows/ci.yml` | #56 | #61 | Awaiting the PR's own CI | Actions still pinned by tag, not SHA |
| SEC-ARCH-01 | Medium now, High with a second site or tenant | **Confirmed**, architectural, documented as decision H7 | `rbac.py`, all routers | #57 | none | `grep` evidence | Needs design and owner decision; not a patch |
| Existing #38 (SEC-01/02/04/05/06/07) | Low to Medium | Previously tracked | Edge Collector, secrets, ingest | #38 | #49 (SEC-07), branches for SEC-05 | not re-tested | SEC-02 (static Fernet key, no rotation), SEC-04 (SNMPv2c cleartext), SEC-06 (unencrypted edge queue) remain design decisions |
| Existing #51 | Low | Previously tracked, not re-tested | `db/session.py` | #51 | none | not re-tested | Pool recovery when all four failures coincide |
| Existing #52 | Process gap | Previously tracked | GHAS code-scanning AI check | #52 | none | n/a | Scan never completed on PR #50; treat as no result |

## 4. Suspected or informational (not confirmed exploitable)

- **No security headers and no TLS in the frontend nginx config.** TLS is stated to terminate at a reverse proxy outside the repo, which was not reviewed. No CSP, `X-Frame-Options`, `X-Content-Type-Options`, HSTS.
- **`X-Forwarded-For` is appended, not replaced,** so any future IP-based control must define which proxy hops are trusted.
- **FastAPI `/docs` and `/redoc` stay enabled** in production on the backend port. Reachable only via the loopback binding in the production compose file.
- **Redis has no password;** it binds to loopback in production compose. Any host-local process can reach it, and login throttle state lives there.
- **Access tokens are not revocable** for their 15-minute lifetime after logout; role changes do take effect immediately because permissions load per request.
- **Production `assert` statements** in route code (Bandit B101). If Python runs with `-O` they vanish; reviewed sites narrow types rather than guard authorization, but that was not checked for every one.
- **`jwt_algorithm` is configurable** by environment. A misconfiguration to a weak or asymmetric mode is not validated at startup.
- **Static Fernet key** (issue #38 SEC-02) means one leaked `.env` exposes all stored integration and collector secrets.

## 5. Not tested

- Live application behavior in staging or production (no deployment access, and none was authorized).
- Repository settings: branch protection, default workflow permissions, Dependabot, secret scanning and push protection (no admin access).
- SNMP wire parsing beyond the BER exhaustion checks cited in existing reports, and the central `snmp.py`, `icmp.py` and `rest.py` drivers (REST network policy was read only through prior reports). No Modbus driver exists in the repository; the string appears only as a protocol label in the telemetry mapping model and migration 0024.
- Cross-router IDOR and mass-assignment testing of every endpoint; Pydantic model strictness per route.
- Celery task authorization and payload trust; broker exposure.
- PostgreSQL role grants beyond reading the bootstrap SQL and CI provisioning.
- Load and resource-exhaustion testing (limits were read, not exercised).
- Container image CVEs (no scanner available in this environment).
- PDF upload and parsing: not implemented.
- The GitHub Advanced Security scan (#52) did not complete; no substitute SAST beyond Bandit was run.

## 6. Requires architectural change

1. **Scoped RBAC** (SEC-ARCH-01, #57). Site, building and tenant isolation does not exist today.
2. **Secret management** (#38 SEC-02). Key versioning, rotation and KMS or Vault integration.
3. **Edge-to-device transport** (#38 SEC-04). SNMPv3 depends on fleet hardware support.
4. **Edge queue encryption at rest** (#38 SEC-06). OS-level versus application-level choice.

## 7. Remediation status and priorities

| Priority | Work | State |
|---|---|---|
| 1 | Review and merge #60 (login throttling) and #58 (refresh reuse) after independent review | Open, not merged |
| 2 | Decide the scope model for SEC-ARCH-01 before a second site is onboarded | Open, needs owner |
| 3 | Add trusted-proxy handling and per-IP limits; add security headers at the reverse proxy | Not started |
| 4 | Pin GitHub Actions by SHA, enable Dependabot for actions, merge #61 | Partly done |
| 5 | Resolve #52 so a GHAS scan completes on future PRs | Not started |
| 6 | Route-by-route authorization test matrix and IDOR pass | Not started |
| 7 | Password-protect Redis; disable `/docs` in production | Not started |
| 8 | Owner decisions on #38 items SEC-02, 04, 06 | Not started |

No PR was merged, no branch protection touched, and nothing was deployed.


---

# 8. PR #59 review (added in v1.2)

**Scope:** PR #59, "user & group management with RBAC and site/rack access", treated as a security boundary implementation and as the candidate remediation for issue #57.
**This section deliberately omits reproduction steps and payloads.** Exploit-relevant detail sits in the regression tests of the remediation PRs, which are unmerged, and in the linked issues at the level of the defect class only.

## 8.1 Exact baseline

| Item | SHA / state |
|---|---|
| `main` | `f94f22078ae0316b1706fb86ba6705a0a364de44` (unchanged since v1.1) |
| PR #59 head / base | `6dac357eccb9a01aefaa9ff104ef987a1e654461` / `f94f22078ae0316b1706fb86ba6705a0a364de44`, mergeable, 2 commits, 24 files, +3078 / -86 |
| PR #59 CI on that head | 11 of 12 checks pass; `github-advanced-security` fails before analysing any file (`CAPIError 400`, unsupported model, issue #52) |
| PR #59 reviews | none; one author comment about the scanner failure |
| Migration lineage | `0030_bulk_import_attempts` to `0031_user_groups`, single head |
| #58 / #60 / #61 / #62 heads | `c57271f...` / `41b3190...` / `2e024cb...` / `39fe287...` (unchanged; all merge textually clean with #59 and with each other in that order) |

Drift since v1.1: none on `main`. PR #59 has two commits, `8d22dfd` (feature) and `6dac357` (mypy fix); the author's scanner comment cites the first. Findings refer to head `6dac357` only.

## 8.2 Method

1. Full read of the #59 diff: access engine, RBAC context, user and group routes, the three scoped routers, migration.
2. Dynamic tests on PostgreSQL 16 and Redis 7 through the real ASGI app: hostile-actor tests per the brief, an object-level Site A / Site B / Rack A / Rack B matrix, permission-composition and revocation tests, real overlapping transactions for administrator changes.
3. Route sweep: all 178 operations in the OpenAPI schema called as a Site A user granted every permission the administrator holds. 121 refused (401/403); results explained in 8.5.
4. Migration 0031 upgrade, downgrade and re-upgrade on a scratch database seeded with one user per default role.
5. Interaction check against #58, #60, #61.
Non-vacuity was checked: an early version of the sweep exercised no routes and was corrected before any result was used.

## 8.3 Verdict on PR #59

**Security status: BLOCKED until the remediation PRs below (or equivalents) are merged into it.** Four defects were confirmed dynamically against `6dac357`. The core object-level scoping of locations, racks and equipment held up under test.

| ID | Severity (est.) | Issue | Status | Remediation PR |
|---|---|---|---|---|
| SEC-RBAC-59-01 rank check ignores scope; group route skips it | High (8.8) | #63 | Confirmed, fix proposed | #67 |
| SEC-RBAC-59-02 rack scope can be widened | Medium (6.5) | #64 | Confirmed, fix proposed | #67 |
| SEC-RBAC-59-03 cross-site group tampering and disclosure | Medium-High (7.1) | #65 | Confirmed, fix proposed | #67 |
| SEC-RBAC-59-04 import jobs readable across scope | Medium (5.3) | #66 | Confirmed, fix proposed | #68 |

## 8.4 Verified properties (tested, passing on `6dac357`)

- Site A user gets 404 for Site B racks, equipment, sites, rooms, organizations, elevation and ports, and for writes against them (update, move, retire, create into a Site B room, move a Site A rack into Site B).
- List endpoints exclude Site B objects and report totals that match the returned items; a `site_id` filter for another site returns nothing.
- Rack-limited users see only their selected racks and the equipment in them; equipment placed directly in a room needs `rack_scope=all`.
- Unknown and foreign ids are indistinguishable (same status and title).
- A rack grant does not follow a rack moved to another site.
- Union of sites and racks across groups; `all` wins over `selected`; a group `deny` beats an allow from another group and from a role; a user with permissions but no sites sees nothing.
- Removing a rack, site or membership, deleting a group, and deactivating a user all take effect on the next request.
- Password policy (12 to 256 characters) applies to create and reset; passwords never appear in responses or the audit log; login errors are identical for unknown, wrong and deactivated accounts; email case is normalised the same way as login.
- Two administrators deactivating each other concurrently (5 rounds, separate sessions) always leave one active.
- Migration 0031 (see 8.6).

## 8.5 Authorization coverage matrix (restricted Site A user)

"Reachable" means the route answers with anything other than 401/403 for a restricted user holding every permission. Evidence: route sweep plus object-level tests.

| Resource family | Restricted-user behaviour | Coverage |
|---|---|---|
| Organizations, countries, cities, sites, buildings, floors, rooms | Scoped lists; by-id reads 404 outside scope | **Remediated by #59** (tested) |
| Racks (read, update, move, retire, elevation, create) | Scoped; out of scope is 404 | **Remediated by #59** (tested) |
| Equipment (list, get, ports) | Scoped | **Remediated by #59** (tested) |
| Equipment mutations, placement, cabling, instantiate | Refused: permissions not site-aware, so inactive | Blocked, not scoped |
| Users and groups (administration) | Reachable | Defects #63, #64, #65; **partially remediated** |
| Import jobs (read, rows, report, commit, cancel) | Reachable for read of any job | **Newly exposed** #66 |
| Import upload | Refused (import permissions inactive) | Blocked, not scoped |
| Catalog reference data (rack and equipment models, revisions, graphics) | Reachable, read only | Global reference data, no site content; accepted |
| Collector machine endpoints (heartbeat, ingest, telemetry) | HMAC-authenticated, not user sessions | Separate trust boundary; collectors are not site-scoped |
| Alarms, telemetry queries, power, capacity, dashboard, floor plans, spatial, impact, discovery, integrations, collector management, managed assets, audit, settings | Refused | **Not remediated**: unavailable to restricted users; unrestricted users unchanged |
| Celery and background jobs | Reachable only through the import routes above | Covered by #66 |
| Realtime or WebSocket paths | None found in the API | n/a |

## 8.6 Migration 0031 assessment

- Upgrade from `0030`, downgrade, re-upgrade: all succeed with data present; users preserved; single Alembic head.
- Adds five tables and three permissions; no existing user, role or grant changes. After upgrade, on a seeded scratch database: Administrator 46 to 49 permissions (only the three new ones), DCIM Manager 37, Engineer 28, Operator 19, Viewer 16 (unchanged), all still unrestricted; a user with no role still holds zero permissions and no site access. No existing non-administrator gains site or rack access.
- Foreign keys cascade correctly (group, member, permission, site access, rack access); `effect` and `rack_scope` are CHECK-constrained; group names are unique case-insensitively.
- Locking: new tables only; adding foreign keys takes brief locks on `app_user`, `permission`, `site` and `managed_asset`. No table rewrite. Not load-tested on a large database.
- Not covered: PostgreSQL versions other than 16; behaviour with an Administrator role that has been renamed or removed (the migration would fail loudly).

## 8.7 Reconciliation of issue #57

| Part of #57 | Classification |
|---|---|
| Site scope enforced for locations, racks, equipment | **Remediated by #59** for restricted users (tested) |
| Rack-level scope inside a site | **Partially remediated**: enforced on reads and mutations; group assignment could widen it until #64 is fixed |
| Delegated administration respects scope | **Newly exposed** (#63, #64, #65) |
| Imports | **Newly exposed** (#66) |
| Alarms, telemetry, power, dashboard, floor plans, spatial, impact, discovery, integrations, collectors, managed assets, audit | **Not remediated**: blocked for restricted users |
| Tenant isolation | **Requires architectural work**: there is no tenant entity; organizations are visible when they contain a granted site |
| Collector and telemetry ingestion by site | **Requires architectural work**: collectors and integrations carry no site link |
| Cross-cutting: scope for global reference data, audit log, caching of effective access | **Requires architectural work** |

**Do not close #57.** #59 delivers scoping for three modules and a fail-closed default for the rest.

## 8.8 Interactions with #58, #60, #61

- No textual or semantic conflict found: #59 touches neither `auth_service.py`, `auth.py` nor `errors.py`. All four merge cleanly in sequence, and the combined tree passes the auth, throttle, refresh and group suites (see 8.9).
- #59 deactivation revokes refresh tokens; with #58 a later replay of such a token triggers the reuse path, which is harmless.
- #60 throttles by email: an administrator password reset does not clear a locked account's counter; an anonymous caller can lock a known account for 15 minutes (already recorded as the accepted cost in #60).

## 8.9 Test and CI evidence

| Check | Result |
|---|---|
| Baseline: #59's own 28 tests on its head `6dac357` (local PostgreSQL 16, Redis 7) | pass |
| Hostile tests against `6dac357` before any fix | **10 of 10 fail** (delegation boundary) and **2 of 2 fail** (import jobs); control tests pass |
| #67 (`808707a`): new tests | 13 pass (10 fixed defects plus 3 positive controls) |
| #67 full backend suite, local | **845 passed** |
| #67 GitHub CI (8 checks: backend on Python 3.12, 3.13, 3.14, frontend, browser E2E, edge collector, code scanning) | all success |
| #68 (`e150222`): new tests, `test_bulk_import*`, `test_user_groups` | 36 and 40 pass; GitHub CI 8 of 8 success |
| #69 (`d26cc2d`): coverage tests | 14 pass |
| Combined tree: #58, #60, #61, #59, #67, #68, #69 merged in order (`8bedbf4`), full backend suite, local | **871 passed**, no textual conflicts |
| Route sweep (178 operations as a restricted user with every permission) | 121 refused; reachable set limited to reviewed modules, catalogs, collector machine endpoints, import jobs |
| Migration 0031 upgrade, downgrade, re-upgrade with seeded users | pass |
| `ruff`, `mypy app` on every fix branch | clean |

Not run: `test_mvp_retention_hostile.py` locally (the earlier v1.1 runs also excluded it; GitHub CI runs it), Playwright locally, the code-scanning AI check on PR #59 itself (fails on the scanner's model configuration, issue #52).

## 8.10 Untested, and residual risk

- User and group directories stay readable to delegated administrators (needed to assign members).
- Effective access is computed per request; no load test.
- Access tokens stay valid for their 15-minute life after a password reset (deactivation is immediate because the user row is checked on every request).
- Frontend `/admin` pages were not security-reviewed beyond confirming the server enforces every rule independently.
- Playwright end-to-end, container image scanning, and non-PostgreSQL-16 versions were not run.
- Static analysis beyond the earlier Bandit run was not repeated on the #59 code.
- Group `site_count` in group lists still reflects grants to sites the actor cannot see.

## 8.11 Recommended next actions

1. Review the delegation-boundary fix (#67) and import-job fix (#68), merge them into PR #59, and consider the coverage tests (#69) alongside, then re-run its CI, before #59 merges.
2. Decide whether delegated administrators should see the full user directory.
3. Add site linkage to import jobs (uploader-only is the interim rule).
4. Scope the remaining modules one at a time and add each to the sweep test's expected-reachable list.
5. Design tenant and collector-to-site models before enabling a second organization.
6. Fix the code-scanning check (#52) so #59 gets a completed scan.

## 8.12 Remediation PR index

| PR | Purpose | Head | Base |
|---|---|---|---|
| #67 | Delegation boundary: scope and rank (fixes #63, #64, #65) | `808707a` | `feature/user-group-management` |
| #68 | Import-job access (fixes #66) | `e150222` | `feature/user-group-management` |
| #69 | Authorization coverage tests (refs #57) | `d26cc2d` | `feature/user-group-management` |

All three are stacked on PR #59 and unmerged. Nothing was merged, deployed, or changed in branch protection.
