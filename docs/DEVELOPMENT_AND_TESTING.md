# Developer setup, testing and CI

**Applies to:** the reconciled Phase 10 source in PR #29, baseline `c2d60b34f0ccc546c809a1d3ff15c11befb79a26`. Commands below assume a Linux development host and a **dedicated non-production database**. Windows contributors can use WSL2 or Docker Compose; native Windows setup may require alternative service/capability steps.

## Toolchain

| Dependency | Documented target | Notes |
|---|---|---|
| Python | 3.11 primary; 3.12, 3.13 and **final 3.14** in blocking CI matrix | The original backend Docker image uses 3.11. Earlier local 3.14.0rc2 failures were pre-release-specific and are not a supported interpreter test |
| PostgreSQL | 16 | The application user must not own the database; bootstrap privileged retention roles separately |
| Redis | 7 | Used by Celery and readiness checks |
| Node.js | 22+ | Frontend CI uses Node 22 |
| Chromium | Installed through Playwright | Required only for browser E2E |
| Docker Compose | Optional | Development services and full-stack alternative |

## Environment isolation and secure bootstrap

Create `dcim` for development and `dcim_test` for test execution. Use distinct credentials for actual deployments and set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY` and `CREDENTIAL_ENCRYPTION_KEY` appropriately in a **local, untracked** environment file. See `backend/.env.example` for authoritative required fields.

```bash
sudo -u postgres psql -c "CREATE USER dcim_app WITH PASSWORD 'dcim_dev_password';"
for db in dcim dcim_test; do
  sudo -u postgres psql -c "CREATE DATABASE ${db};"
  sudo -u postgres psql -d "${db}" -c "GRANT CREATE, USAGE ON SCHEMA public TO dcim_app;"
  sudo -u postgres psql -d "${db}" -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS btree_gist;'
done
```

Use the PostgreSQL superuser or an explicitly privileged deployment identity for `backend/scripts/bootstrap_privileged_roles.sql`. It changes audit/retention privileges; do not grant such privileges to the ordinary API role merely to make migrations pass.

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # Edit for your local, dedicated database and secrets.
alembic heads          # Expect exactly one head in the reviewed Phase 10 snapshot.
alembic upgrade head
sudo -u postgres psql -d dcim -f scripts/bootstrap_privileged_roles.sql
python scripts/create_admin.py --email admin@example.com --name "Admin User"
uvicorn app.main:app --reload
```

Never use production data for fixture seeding, destructive migration tests or hostile-retention tests. For a clean production migration, follow a change-controlled backup and rollback plan rather than copying this example unchanged.

## Local validation commands

Backend (use the test database and Redis before executing):

```bash
cd backend
source .venv/bin/activate
ruff check app tests
mypy app
alembic upgrade head
pytest -q tests/integration/test_mvp_retention_hostile.py
pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py
```

The real ICMP socket tests require Linux `CAP_NET_RAW`. CI grants the capability to its interpreter. In local development, use a dedicated test interpreter and follow your environment's least-privilege policies; avoid running all tests as root.

Edge Collector, from repository root (note the explicit repository-root import path, necessary for the `pytest` console script):

```bash
PYTHONPATH=. pytest -q edge_collector/tests
ruff check edge_collector
```

Frontend:

```bash
cd frontend
npm ci
npm audit --audit-level=high
npm test
npm run typecheck
npm run lint
npm run build
```

### Browser E2E

The two existing specs cover Phase 10B instantiation/faceplates and Phase 10C telemetry/impact flows. Use an **isolated** migrated database, Redis, seeded Administrator, running backend and Vite dev server, then:

```bash
cd frontend
npx playwright install --with-deps chromium
E2E_ADMIN_EMAIL=e2e-admin@example.com E2E_ADMIN_PASSWORD='<local-test-password>' \
  BACKEND_URL=http://127.0.0.1:8000 BASE_URL=http://127.0.0.1:5173 \
  npm run test:e2e
```

To seed non-interactively on the PR #29 branch, set `E2E_ADMIN_PASSWORD` in the **environment** and execute `backend/scripts/create_admin.py --email "$E2E_ADMIN_EMAIL" --name "E2E Administrator" --password-from-env E2E_ADMIN_PASSWORD --if-exists skip`. Do not put passwords directly in CLI arguments. Before launching Playwright, verify that `GET /api/v1/health/ready` is HTTP 200 and Vite serves its application. The [workflow](../.github/workflows/ci.yml) is the executable reference for its exact ephemeral PostgreSQL/Redis provisioning and readiness loops.

### Migration smoke test

On a **disposable migrated database only**, confirm the single Alembic head and the documented retention round trip: `alembic downgrade 0010_mvp_alarms` followed by `alembic upgrade head`. Validate the privileged retention bootstrap and permissions afterwards. This round trip does not guarantee that destructive rollback is safe on production data.

## Seven-job CI matrix on PR #29

| Job | What it executes |
|---|---|
| `backend` | Python 3.11, Ruff, mypy, migrated PostgreSQL/Redis, single-head retention round trip, privileged bootstrap, hostile validation, raw-socket tests and regression suite |
| `backend suite (Python 3.12)` | Migrated backend and Edge Collector suite with 3.12 |
| `backend suite (Python 3.13)` | Same on 3.13 |
| `backend suite (Python 3.14)` | Same on final 3.14, **blocking** |
| `frontend` | npm dependency install, high/critical audit gate, one Vitest step, TypeScript, ESLint and build |
| `edge-collector` | Ruff and full separately packaged collector test suite |
| `browser-e2e` | Own PostgreSQL/Redis, least-privilege database role, migration/bootstrap, fixture, readiness, Chromium and both Playwright specs |

All seven jobs passed at the reviewed PR head in [GitHub Actions run #80](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36141826654). GitHub validates *the commit under test*, not unrelated later changes. After merging, recheck the new `main` SHA and its workflow. The browser job uploads logs, traces and screenshots on failure; its controlled failing artifact probe is documented locally in `PHASE10_INTEGRATION_REPORT.md`.

## Concurrency / telemetry regression guidance

The Phase 10C latest-status cache is keyed by `binding_id`, unlike the older series-based latest query. A PostgreSQL `ON CONFLICT ... DO UPDATE WHERE stored.sampled_at < incoming.sampled_at` preserves strictly-newer writes under concurrent conflict locking. Equal timestamps are first-writer-wins; stale/equal writes fall back to `SELECT` and `db.refresh()` and retain HTTP 200 with the authoritative row. Relevant tests: `backend/tests/unit/test_telemetry_port_status.py`, `backend/tests/integration/test_telemetry_ordering_concurrency.py`, `backend/tests/api/test_telemetry_port_status.py`.

For Edge BER regression coverage, use `edge_collector/tests/test_snmp.py`. The test responder decodes the request structurally and tests variable-length IDs, community bytes, malformed packets and trailing data; the production collector also validates `datagram.exhausted`.
