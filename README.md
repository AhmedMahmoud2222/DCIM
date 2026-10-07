# DCIM Platform

**In-house Data Center Infrastructure Management (DCIM)** — FastAPI/PostgreSQL backend, React/TypeScript frontend and an independently packaged Edge Collector. The repository has progressed beyond Phase 1: the foundation, operational rack/power/telemetry modules, driver/collector functionality and Phase 10A–10C catalog, equipment/cabling and live overlay/failure-impact features are present in source.

> **Status at this documentation baseline (2026-10-07, README revision v2):** `main` is `7364e68626d5b27f1698d0d7268d74a0026dedb3`. Its post-merge [CI run 37565234006](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/37565234006) and [Deployment validation run 37565233967](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/37565233967) succeeded. PR #29 merged on 2026-09-26 (`39151d8`) and is no longer pending. Delivered R2 functionality (bulk import, user and group management with site/rack scope, catalog datasheet storage, extraction and apply, canonical units and metric registry) is merged to `main`. R1 staging acceptance has not happened. R3 work (#101 in draft PR #110, and #102 to #107 in draft PRs #111 to #116) is **not merged**. The deployment workflow validates configuration only. No persistent staging or production deployment is recorded in GitHub, so no deployed SHA exists. Green CI does **not** establish production readiness or independent security certification. See [current implementation status](docs/PROJECT_STATUS.md).

[Current implementation status](docs/PROJECT_STATUS.md) · [Architecture overview](docs/ARCHITECTURE_OVERVIEW.md) · [Development and testing](docs/DEVELOPMENT_AND_TESTING.md) · [Operations and deployment](docs/OPERATIONS.md) · [Documentation index](docs/DOCUMENTATION_INDEX.md)

[Consolidated product roadmap](docs/PRODUCT_ROADMAP.md) — original vision, delivered capabilities, and proposed R1–R5 releases. Roadmap milestones are not deployment claims.

## Implemented capabilities

| Area | Present in repository | Primary code / evidence |
|---|---|---|
| Phase 1 foundation | JWT authentication, roles and permissions, audit/retention protection, transactional outbox, locations and managed-asset identity | `backend/app/domain/`, `backend/app/api/v1/`; [Phase 1 implementation](PHASE1_IMPLEMENTATION.md) |
| Operational DCIM modules | Rack and equipment management, room floor plans and placement, power topology, alarms, dashboard, collectors, integrations, discovery, metric mappings and telemetry | [API router](backend/app/api/v1/router.py), `frontend/src/app/App.tsx` |
| Driver and Edge Collector | Central collector/driver integration, Edge Collector scheduler and SNMP v2c implementation; separately packaged collector tests | `backend/app/application/drivers/`, `edge_collector/`; [Phase 8 clarification](PHASE8_ARCHITECTURE_CLARIFICATION.md) |
| Phase 10A — Asset Catalog | Versioned manufacturer/model/revision lifecycle, Administrator authoring UI, graphics and marker editor, template management, draft/publish/retire controls | [PR #16](https://github.com/AhmedMahmoud2222/DCIM/pull/16), [#20](https://github.com/AhmedMahmoud2222/DCIM/pull/20), [#21](https://github.com/AhmedMahmoud2222/DCIM/pull/21) |
| Phase 10B — Physical instantiation | Published-catalog equipment instantiation, port/power-inlet snapshots, cabling and rack-elevation faceplates | [PR #22](https://github.com/AhmedMahmoud2222/DCIM/pull/22) |
| Phase 10C — Live status and impact | Port/inlet telemetry bindings, cached latest status, rack marker overlays, bounded power/network failure-impact simulation | [PR #23](https://github.com/AhmedMahmoud2222/DCIM/pull/23) |
| Post-Phase-10 integration | Strictly timestamp-ordered latest-status upsert, SNMP BER test hardening and outer-datagram validation, expanded CI | [PR #29](https://github.com/AhmedMahmoud2222/DCIM/pull/29), merged 2026-09-26 as `39151d8` |
| R2 delivered functionality | Excel bulk import ([#50](https://github.com/AhmedMahmoud2222/DCIM/pull/50)), user/group management with site and rack scope ([#59](https://github.com/AhmedMahmoud2222/DCIM/pull/59), hardened by `e319392`), catalog datasheet storage, extraction and reviewed apply (`1ba8d17`, `19f91af`, `825c3e4`), canonical units and metric registry (`754484d`), decommission guard (`4000a60`), telemetry value validation (`dfbd2f1`), atomic `If-Match` concurrency (`7364e68`) | Merged to `main`; see [project status](docs/PROJECT_STATUS.md) for the assurance boundary |

The code and documentation describe **implemented source**, not a production SLA, live industrial integration, complete vendor coverage or autonomous/self-healing operation. Phase 0 sections in `ARCHITECTURE_REVIEW.md` are retained as dated historical design material; use the current-state addenda and the documents linked above to interpret the actual repository.

## Technology and repository layout

- **Backend:** Python 3.11 (primary CI job) and 3.12 to 3.14 (blocking full-suite jobs), FastAPI, SQLAlchemy async, PostgreSQL 16, Alembic, Redis 7 and Celery.
- **Frontend:** React 18, TypeScript, Vite, Tailwind, TanStack Query; Vitest unit tests and Playwright browser tests.
- **Edge Collector:** separate `edge_collector/` source and tests, including real loopback UDP SNMP protocol tests.
- **Top-level layout:** `backend/`, `frontend/`, `edge_collector/`, `.github/workflows/ci.yml`, historical phase reports and `docs/` for current guidance.

## Start a development environment

Use a dedicated **development database**. Never use the sample passwords or CI fixture credentials in a deployed environment. PostgreSQL must own the database as `postgres`; the ordinary `dcim_app` role must **not** be database owner, because privileged audit-retention ownership depends on that separation.

### Prerequisites

Python 3.11 is the baseline used by the primary backend CI job. Full migrated suites also run on Python 3.12, 3.13 and 3.14 as separate CI jobs. Install Node.js 22+, PostgreSQL 16 and Redis 7. Docker Compose is an alternative to installing services locally.

### Local backend

```bash
# From the repository root; provision PostgreSQL 16 and Redis 7 first.
sudo -u postgres psql -c "CREATE USER dcim_app WITH PASSWORD 'dcim_dev_password';"
sudo -u postgres psql -c "CREATE DATABASE dcim;"
sudo -u postgres psql -d dcim -c "GRANT CREATE, USAGE ON SCHEMA public TO dcim_app;"
sudo -u postgres psql -d dcim -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS btree_gist;'

cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # Set unique local DATABASE_URL / JWT_SECRET_KEY etc.
alembic upgrade head
sudo -u postgres psql -d dcim -f scripts/bootstrap_privileged_roles.sql
python scripts/create_admin.py --email admin@example.com --name "Admin User"
uvicorn app.main:app --reload
```

API: `http://localhost:8000/docs`. Liveness: `/api/v1/health/live`; readiness (PostgreSQL **and** Redis): `/api/v1/health/ready`. The latter returns HTTP 503 if a dependency is unavailable.

### Frontend and workers

```bash
cd frontend
npm ci
npm run dev
# Browse http://localhost:5173; Vite proxies /api to localhost:8000.
```

For asynchronous jobs, run the Celery worker and beat as **separate processes** from `backend/`:

```bash
celery -A app.infrastructure.celery_app worker --loglevel=info -Q default,maintenance
celery -A app.infrastructure.celery_app beat --loglevel=info
```

### Docker Compose

The Compose encryption-key omission identified at `2626acb` is corrected on this branch. From the repository root, copy `.env.example` to an untracked `.env`, restrict its permissions (`chmod 600 .env` on Linux), and replace the password/JWT placeholders. In the installed backend Python environment, generate **one key per environment**:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the output as `CREDENTIAL_ENCRYPTION_KEY` in your private root `.env`. Compose forwards that **same** value to `migrate`, `backend`, `celery-worker` and `celery-beat`; a missing or empty value stops configuration with a named error. Fernet requires URL-safe base64 encoding of 32 random bytes (normally 44 characters). A long arbitrary password is not a Fernet key. Never commit the generated value, regenerate it per service/restart, or publish resolved Compose output containing secrets.

```bash
docker compose config --quiet
docker compose up --build
# In another terminal, after startup:
docker compose exec backend python scripts/create_admin.py --email admin@example.com --name "Admin User"
```

Keep a protected recovery copy of the key: database ciphertext alone is insufficient for recovery. Rotation requires coordinated re-encryption, not just editing `.env`; see [operations](docs/OPERATIONS.md). The `Compose smoke` job in the Deployment validation workflow builds and starts the full stack on a disposable CI runner and checks readiness, ClamAV, the OCR sandbox and shared media. That proves the Compose definition, not a persistent staging or production environment. Complete the operations checks before deployment.

## Test and quality gates

The CI workflow (`.github/workflows/ci.yml`) has **seven** jobs: primary backend on 3.11 (lint, mypy, Alembic single-head gate, migrations with a downgrade and re-upgrade round trip, retention and regression tests), full migrated backend suites on 3.12, 3.13 and 3.14, frontend (dependency audit, Vitest, TypeScript, ESLint and build), Edge Collector and isolated PostgreSQL/Redis-backed Playwright E2E. A separate Deployment validation workflow runs `Compose smoke` and a `Deployment validation gate` job. All of them succeeded on `main` at `7364e68` (links in the status block above). Only `backend` and `frontend` are currently required by the branch ruleset; see [stabilization report](docs/STABILIZATION_PASS_2026-10-07_v1.md).

```bash
# First export the test environment and bootstrap dcim_test as described in
# docs/DEVELOPMENT_AND_TESTING.md (including TEST_ADMIN_DATABASE_URL).
# From the repository root:
cd backend
PYTHONPATH=.. pytest -q
ruff check app tests
mypy app

# Return to repository root:
cd ..
PYTHONPATH=. pytest -q edge_collector/tests
ruff check edge_collector

# Frontend:
cd frontend
npm ci
npm test
npm run typecheck
npm run lint
npm run build
npm run test:e2e      # Requires a live migrated backend, seeded test admin and Chromium.
```

The real ICMP raw-socket tests require `CAP_NET_RAW` on Linux; the CI workflow grants it for the testing interpreter. See [development and testing](docs/DEVELOPMENT_AND_TESTING.md) for isolation, setup, fixtures, migrations and Playwright details.

## Security and operating boundaries

Authentication and authorization are enforced by the backend, not frontend menu visibility. Catalog mutation uses Administrator-specific permission checks; telemetry and impact routes require their documented permissions. The Phase 10C latest-status cache accepts a sample only when its `sampled_at` is **strictly newer** than the stored row; equal timestamps retain the first writer. This is distinct from the historical series-based `/telemetry/latest` API.

The **telemetry** batch endpoint (`POST /api/v1/collectors/{collector_id}/telemetry`) emits per-record ACKs for handled rejections/duplicates and commits accepted records at the end of a successful request. An **unexpected** exception aborts the whole transaction and no successful ACK body is returned. The separate discovery `/collectors/{collector_id}/ingest` endpoint already uses per-record savepoints. See [audit status](docs/AUDIT_STATUS.md) for this contract distinction and the remaining owner decision.

## Documentation

Begin with the [documentation index](docs/DOCUMENTATION_INDEX.md) rather than assuming a historical Phase 1 file describes the whole current repository. [Architecture overview](docs/ARCHITECTURE_OVERVIEW.md) explains component boundaries, [project status](docs/PROJECT_STATUS.md) distinguishes merged code from pending integration, [operations](docs/OPERATIONS.md) addresses runtime checks, and [audit status](docs/AUDIT_STATUS.md) distinguishes baseline findings from subsequent fixes. Historical reports remain unchanged unless a clearly dated addendum is required.
