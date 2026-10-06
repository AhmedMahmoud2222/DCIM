# Issue #101 handover (v1)

Branch: `feature/r3-network-profiles-discovery-101`. Base: `main@754484d9`. Do not merge, do not close #101.

## Done and pushed before this note
- Slice A `ab05ad5`: vendor/device profiles, matcher (ambiguity refused), metric mappings, API, migration 0036, tests.
- Slice B `0f7d23d`: SNMPv3 authPriv edge stack (USM, SHA-1/2, AES-128/192/256), v2c walk session, write-only credential API, backend/edge policy contract test. Interop-tested by hand against pysnmp 7.1.30 for 5 algorithm combinations (not part of CI).
- Slice C `997d0c8`: LLDP/CDP edge adapters, neighbor evidence, reconciliation, operator decisions, discovery-plan endpoint, migration 0037, tests.

## In this commit (work in progress: Slice D done locally, Slice E partial)
- Cables: migration 0038 (cable, cable_endpoint, integrity triggers, `cable:*` RBAC), `cable_service`, `trace_service`, `app/api/v1/cables.py`. Tests `test_cables.py`, `test_cables_scope.py` and `tests/integration/test_cable_constraints.py` passed locally.
- `access_control.py`: ids are coerced to plain `uuid.UUID` in `ensure_equipment_access` and `ensure_rack_access`. asyncpg returns a UUID subclass that `literal()` cannot type, which broke restricted users. This touches a shared file; call it out in review.
- Frontend `src/features/network/` (api, PortPicker, ProfilesPage, NeighborReviewPage, CablesPage, TracePage), routes in `App.tsx`, permission-gated nav in `AppShell.tsx`.

## Not finished
1. `npx tsc --noEmit` fails in `CablesPage.tsx`: the action mutation returns `Promise<void> | Promise<Cable>`. Make `mutationFn` return `Promise<unknown>`.
2. No Vitest tests, lint run, build or Playwright E2E yet.
3. The full backend regression was started and stopped. Rerun `pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py`, then that file separately, plus ruff, `alembic heads`, and `PYTHONPATH=. pytest -q edge_collector/tests`.
4. Push, wait for exact-head CI (Python 3.12/3.13/3.14, compose smoke, deployment validation), then request a non-author review of the items in the task brief.
5. Docs: edge `cryptography` dependency (CI edge job already updated), local credential file `edge_collector/credentials.py`, discovery plan, new permissions (`network_profile:*`, `cable:*`).

## Local test setup used
PostgreSQL 16 and Redis locally. Environment: `DATABASE_URL`, `TEST_ADMIN_DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, `PYTHONPATH=<repo root>`. Never run two pytest processes against one database (deadlocks).

## Caveats
- Central never sends secrets to the edge. SNMP credentials live in a local 0600 JSON file. No edge runtime loop calls `run_discovery_cycle` yet.
- Trace is single hop; no patch-panel pass-through.
- Nothing from Slices D and E has been through CI or independent review.
