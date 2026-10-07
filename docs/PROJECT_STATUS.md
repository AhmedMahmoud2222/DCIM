# Current implementation and release status

**Revision v2, verified 2026-10-07 against `main` `7364e68626d5b27f1698d0d7268d74a0026dedb3`.** Post-merge [CI run 37565234006](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/37565234006) (seven jobs) and [Deployment validation run 37565233967](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/37565233967) (`Compose smoke`, `Deployment validation gate`) succeeded for that SHA. The 2026-09-26 baseline this file previously described (documentation branch based on PR #29 `c2d60b3`, `main` `d96676c`) is superseded: PR #29 merged as `39151d8` on 2026-09-26. Historical phase reports are unchanged.

## Phase and capability ledger

| Phase / stream | Implemented source | Integration record | Verification boundary |
|---|---|---|---|
| 1 — Foundation | Authentication (JWT/refresh/CSRF), global RBAC, audit/retention, outbox, locations and managed assets | Merged to `main`; see `PHASE1_IMPLEMENTATION.md` and its correction report | Historical red-team corrections and automated backend/migration CI; not a production audit |
| 2/3 — Physical and operational capabilities | Racks, equipment, placements and floor plans, power models/topology, alarms/dashboard | Code is exposed by `backend/app/api/v1/router.py` and corresponding React routes | Consult phase-specific reports for exact gate status; no blanket Phase 2/3 certification claimed |
| 8 — Monitoring, discovery and collectors | Collector registration/polling and drivers, integration/device profiles, metric mappings, discovery, telemetry pipeline | Code in `backend/app/` plus separate `edge_collector/` | Vendor/metric coverage is incremental; `PHASE8_ARCHITECTURE_CLARIFICATION.md` distinguishes the driver framework from persistent mapping layers |
| 10A — Catalog designer | Versioned manufacturer/model/revision catalog, draft/publish/retire, Administrator authoring, template editors, graphics and markers | Merged via [#16](https://github.com/AhmedMahmoud2222/DCIM/pull/16), [#20](https://github.com/AhmedMahmoud2222/DCIM/pull/20), [#21](https://github.com/AhmedMahmoud2222/DCIM/pull/21) | Source merged; follow catalog permissions, optimistic concurrency and database-trigger tests |
| 10B — Physical instantiation | Published revision instantiation, snapshot ports/inlets, cabling, rack-elevation faceplates | [#22](https://github.com/AhmedMahmoud2222/DCIM/pull/22), merge `e50314e` | Merged into `main`; browser suite exercises instantiation/faceplates |
| 10C — Telemetry and failure impact | Port/power/environment bindings, one-row latest-status cache, polling overlays, bounded power/network failure traversal | [#23](https://github.com/AhmedMahmoud2222/DCIM/pull/23), merge `d96676c` | Source merged; subsequent timestamp-ordering correction is **only** in PR #29 until merged |
| Phase 10 post-audit integration | Conditional upsert, SNMP BER/UDP validation, consolidated Vitest, Edge, browser and 3.11–3.14 CI | [#29](https://github.com/AhmedMahmoud2222/DCIM/pull/29), merged 2026-09-26 as `39151d8` (included #27 and #28 history) | Merged. Its exact-head CI evidence (run #80) predates later main changes; use the current main CI run above |
| 11 — Edge Collector security backlog ([#38](https://github.com/AhmedMahmoud2222/DCIM/issues/38)) | SEC-07 log sanitization (#45, #49), SEC-05 nonce/heartbeat retention (#46), SHA-pinned Actions (#92), Alembic single-head gate (#79) | Merged | SEC-01, SEC-02, SEC-04 and SEC-06 remain open. SEC-04 is addressed by draft PR #110, which is not merged |
| R2 delivered functionality | Excel bulk import (#50); user and group management with site/rack scope and delegation limits (#59, `e319392`, `2cc4750`); catalog datasheet storage, OCR extraction and reviewed apply (`1ba8d17`, `19f91af`, `825c3e4`); canonical units and versioned metric registry (`754484d`, [ADR-0012](architecture/ADR-0012-canonical-physical-units.md)); decommission guard (`4000a60`); telemetry numeric validation (`dfbd2f1`); atomic `If-Match` row locks (`7364e68`) | Merged to `main` | Merged is not independently accepted or deployed. Real-device interoperability, manual accessibility checks and staging acceptance (R1) are open |
| R3 — network and power intelligence | Issue #101 (profiles, SNMPv3, LLDP/CDP, cables) in draft [PR #110](https://github.com/AhmedMahmoud2222/DCIM/pull/110); issues #102 to #107 in draft PRs #111 to #116 | **Not merged.** All are drafts. PR #110 head `e7f5f23` has failing `backend` and `backend suite` checks at the time of writing | No R3 to R5 feature is delivered. Draft code is not accepted code |

## CI gate (current `main`)

Seven jobs in `ci.yml`: Python 3.11 primary backend with lint, mypy, the Alembic single-head gate, a migration downgrade and re-upgrade round trip, retention and raw-socket suite; Python 3.12, 3.13 and **blocking** 3.14 migrated backend suites; frontend npm high/critical audit, Vitest/typecheck/lint/build; independent Edge Collector tests; PostgreSQL/Redis-backed Playwright browser tests. CI's moderate npm audit is reported but non-blocking. The browser's failure-artifact logic was exercised locally with a controlled failure; hosted success does not independently validate an actual hosted failure upload.

The `Deployment validation` workflow adds `Compose smoke` (builds and starts the whole stack on a disposable runner) and the `Deployment validation gate`. `Deployment Tools Test` runs only when deployment-related paths change. Test counts change with every merge; the figures in `PHASE10_INTEGRATION_REPORT.md` are historical. Use the current Actions job logs. Branch protection requires only `backend` and `frontend` (see the [stabilization report](STABILIZATION_PASS_2026-10-07_v1.md)).

## API and data-contract notes

- `POST /api/v1/telemetry/port-status/ingest` invokes `record_latest_status()`. Unique conflict target: **binding_id**. Incoming samples update the cached row only when their timestamp is **strictly later** than the stored one. An equal timestamp is first-writer-wins; a stale/equal ingest returns the retained status without changing `received_at`. Its API response remains HTTP 200 with the current row.
- The historical `GET /api/v1/telemetry/latest` is a **different code path**: it orders readings by `occurred_at DESC` and applies a result limit; it does not use a window function or select one winner per series. It cannot establish the Phase 10C cache's conflict-ordering behavior.
- The Edge Collector SNMP v2c code parses request/response BER and validates trailing bytes **outside** the outermost SEQUENCE; the structural test responder is independently implemented in the test module.
- The telemetry ingest endpoint returns per-record ACKs for handled rejections such as `NOT_ASSIGNED`, `UNKNOWN_METRIC_MAPPING`, `INVALID_TELEMETRY_VALUE` and `INCOMPATIBLE_TELEMETRY_UNITS`. Each record runs in a savepoint, so a handled rejection does not discard valid peers. An unexpected exception still aborts and rolls back the whole request. The device ingest endpoint (`/collectors/{id}/ingest`) isolates every record with its own savepoint and commit. Any change to the unexpected-failure contract needs an owner decision (Issue #38, SEC-01).

## Known limits, open evidence and follow-up

1. **PR state:** #29 merged on 2026-09-26 (`39151d8`). #27 and #28 reached `main` through #29's history. Open PRs on 2026-10-07: #110 (R3, draft), #111 to #116 (R3 to R5 drafts), #117 (action-version comments), #118 (review report, draft), #87, #83, #69 and #67 (older review and test branches, some targeting non-main bases).
2. **Audit report currency:** the original `PHASE10_INDEPENDENT_AUDIT_REPORT.md` is a **historical audit of `d96676c`**. Use [AUDIT_STATUS.md](AUDIT_STATUS.md) (a dated snapshot of 2026-09-26) and the [integration report](../PHASE10_INTEGRATION_REPORT.md) for the Phase 10 findings. For current security and defect status, use the [stabilization report](STABILIZATION_PASS_2026-10-07_v1.md).
3. **Collector F3 / SEC-01:** whole-request rollback on an unexpected telemetry failure still needs an owner decision on whether all-or-nothing is the intended API contract. Handled per-record rejections already use savepoints (see API notes).
4. **Production validation:** production secrets, backup/restore, monitored queues, external device interoperability, capacity tests, high availability and disaster recovery have not been established solely by green CI.
   All four Python services receive the same required `CREDENTIAL_ENCRYPTION_KEY`, and CI validates this. CI also starts the full Compose stack on a disposable runner (see Deployment status below).
5. **Future capability:** AI-assisted orchestration, autonomous remediation/self-healing, comprehensive vendor integrations and full CFD/3D capabilities are roadmap goals unless a specific implemented module and release gate proves otherwise.

## Deployment status (verified 2026-10-07 from GitHub and repository source)

| Statement | True? | Evidence |
|---|---|---|
| A disposable CI Compose stack is validated | Yes | `Compose smoke` and `Deployment validation gate` succeeded on `main` at `7364e68` (run 37565233967); `Disposable combined-stack startup and rollback` runs when deployment paths change |
| A deployment script exists | Yes | `scripts/deploy-docker-compose.sh` (lock, backup, `up -d --build`, health check, rollback). It rejects any target other than `staging` and requires the SHA to be on `origin/main` with verified CI evidence |
| A persistent staging deployment was performed | **No evidence** | The GitHub Deployments API lists zero deployments. `deploy.yml` has no `workflow_dispatch` run. Its eight recorded runs are `push` failures from 2026-09-26 on the earlier branch `claude/intelligent-edison-94mvbo` |
| An exact deployed staging SHA is known | **No** | Nothing records one |
| Production deployment is supported | **No** | `deploy.yml` is named "validation only" and never calls the deploy script. The script's `validate()` fails with "production deployment is disabled" |
| A production deployment was performed | **No** | No deployment record exists |

Staging execution still needs owner authorization and the [staging VM plan](STAGING_VM_DEPLOYMENT_PLAN.md).

## What to update when a PR merges

The merger updates the status block in the README and the baseline line at the top of this file to the new `main` SHA, links the successful **post-merge main CI run**, and records any new deployment with its own change record and environment-specific evidence. Issues #24 to #26 and the stale issues listed in the [stabilization report](STABILIZATION_PASS_2026-10-07_v1.md) need owner-approved dispositions. Never rewrite old phase reports to imply features existed at their historical baseline.
