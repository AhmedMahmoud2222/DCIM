# DCIM01 Hostile Security Audit Report

**Version:** 1.0
**Date:** 2026-09-29 (executed on demand; the 23:30 UAE scheduled trigger was disabled at the owner's request)
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
- SNMP and Modbus wire parsing beyond the BER exhaustion checks cited in existing reports. Modbus code was not located in this repository.
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
