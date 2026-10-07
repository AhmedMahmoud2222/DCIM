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
| Docker Compose | Optional reference deployment | Required-key forwarding corrected; configuration checked, full container startup still requires runtime validation |

## Environment isolation and secure bootstrap

Create `dcim` for development and `dcim_test` for test execution. Use distinct credentials for actual deployments and set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY` and `CREDENTIAL_ENCRYPTION_KEY` appropriately in a **local, untracked** environment file. See `backend/.env.example` for authoritative required fields.

Generate the credential-encryption key once per environment with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` after installing backend dependencies. This produces URL-safe base64 for 32 random bytes (normally 44 characters). Settings checks presence/minimum length; actual encryption/decryption constructs a Fernet key, which enforces the format. Share the identical value across migration, API, worker and beat, and retain it across restarts. Local processes read `backend/.env`; Compose reads the root `.env` and explicitly forwards the value. Keep both untracked and access-restricted. Preserve a protected key backup and follow [operations](OPERATIONS.md) for coordinated rotation/recovery; regenerating the key strands existing ciphertext.

The fresh test-database commands below generate a disposable test key. Do not use that generation step to restart an environment containing encrypted credentials.

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

Backend (from the repository root, in a separate test shell). The test fixtures use environment variables before `.env` values and require a superuser connection to reset protected audit tables. Replace the test-admin password placeholder with the actual password for your dedicated local test PostgreSQL instance; do not point these variables at development or production data:

```bash
cd backend
source .venv/bin/activate
export DATABASE_URL='postgresql+asyncpg://dcim_app:dcim_dev_password@localhost:5432/dcim_test'
export TEST_ADMIN_DATABASE_URL='postgresql+asyncpg://postgres:<test-superuser-password>@localhost:5432/dcim_test'
export REDIS_URL='redis://localhost:6379/1'
export ENVIRONMENT=test
export JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREDENTIAL_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export PYTHONPATH=..
ruff check app tests
mypy app
alembic upgrade head
sudo -u postgres psql -d dcim_test -f scripts/bootstrap_privileged_roles.sql
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

To seed non-interactively on the PR #29 branch, export both `E2E_ADMIN_EMAIL` and `E2E_ADMIN_PASSWORD` in the fixture-seeding shell, activate the backend environment, and from `backend/` execute `python scripts/create_admin.py --email "$E2E_ADMIN_EMAIL" --name "E2E Administrator" --password-from-env E2E_ADMIN_PASSWORD --if-exists skip`. Use the same isolated database configuration as the E2E API process. Do not put passwords directly in CLI arguments. Before launching Playwright, verify that `GET /api/v1/health/ready` is HTTP 200 and Vite serves its application. The [workflow](../.github/workflows/ci.yml) is the executable reference for its exact ephemeral PostgreSQL/Redis provisioning and readiness loops.

### Migration smoke test

On a **disposable migrated database only**, confirm the single Alembic head and the documented retention round trip: `alembic downgrade 0010_mvp_alarms` followed by `alembic upgrade head`. Validate the privileged retention bootstrap and permissions afterwards. This round trip does not guarantee that destructive rollback is safe on production data.

## Seven-job CI matrix on PR #29

### Actual Docker Compose startup check

The first live Docker run [#36221376961](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36221376961) reproduced an unprivileged Celery beat failure: `Permission denied: 'celerybeat-schedule'`. PostgreSQL/Redis were healthy, migration and privileged bootstrap exited zero, backend was healthy and worker ran; the smoke job failed as intended, emitted sanitized diagnostics and removed the isolated containers and volume. The proposed Compose correction moves beat's disposable local schedule to writable `/tmp/celerybeat-schedule`; check the subsequent run before treating it as validated.

The independent [Deployment validation workflow](../.github/workflows/deployment-validation.yml) runs on PRs to the integration branch or `main`, pushes to `main`, and manual dispatch after it is on the default branch. It contains a `Compose smoke` job and a dependent `Deployment validation gate` job. A runner creates one project-specific disposable volume and fresh credentials, builds and starts the complete stack, verifies one-shot migration/bootstrap exit codes, service health and HTTP 200 through both backend and frontend, checks worker/beat remain running, then tears down its project on either success or failure. The existing daemon-free `backend/scripts/check_compose_settings.py` check remains in the backend CI job and also runs in the smoke workflow.

Reproduce with Docker Engine and Compose from the repository root. Keep shell tracing disabled and copy the printed `export DCIM_SMOKE_...` lines only into your own shell:

```bash
eval "$(python3 .github/scripts/compose_smoke.py prepare | grep '^export DCIM_SMOKE_')"
trap 'python3 .github/scripts/compose_smoke.py cleanup' EXIT
python3 .github/scripts/compose_smoke.py config
python3 -m pip install -e ./backend
python3 backend/scripts/check_compose_settings.py
python3 .github/scripts/compose_smoke.py start
python3 .github/scripts/compose_smoke.py verify
```

Missing and empty `CREDENTIAL_ENCRYPTION_KEY` are explicitly rejected by `config`. If startup fails, run `python3 .github/scripts/compose_smoke.py diagnose` before the trap cleans up; inspect the sanitized health state and startup errors, fix the cause, and rerun from a fresh project. This checks disposable Compose runtime behavior, not production TLS, external devices, backup/restore, or deployment authorization. The owner must configure `Deployment validation gate` as a required passing check on the actual protected deployment branch and prevent bypass; until verified, this is evidence rather than an enforced production gate.

The primary `backend` job also runs `python scripts/check_compose_settings.py`. This daemon-free regression check invokes `docker compose config --format json` with ephemeral fixture values and an explicit empty env file, checks every required `Settings.model_fields` entry in all four Python service environments, validates Settings/Fernet, verifies the shared key, and requires missing/empty keys to fail Compose interpolation. It never prints resolved secrets. Run from the installed backend environment with Docker Compose available:

```bash
cd backend
python scripts/check_compose_settings.py
# Negative control: use an absolute path to the previous Compose configuration.
python scripts/check_compose_settings.py --compose-file /tmp/compose-before.yml
```

The negative control was run against the configuration at `2626acbd6fdb0fce301bc22c36930f2bd914e797` and failed for all four missing key mappings. Standalone Compose can be selected with `--compose-command /absolute/path/to/docker-compose`; no daemon is needed for these configuration checks. They are not container-startup tests.

| Job | What it executes |
|---|---|
| `backend` | Python 3.11, Ruff, mypy, migrated PostgreSQL/Redis, single-head retention round trip, privileged bootstrap, hostile validation, raw-socket tests and regression suite |
| `backend suite (Python 3.12)` | Migrated backend and Edge Collector suite with 3.12 |
| `backend suite (Python 3.13)` | Same on 3.13 |
| `backend suite (Python 3.14)` | Same on final 3.14, **blocking** |
| `frontend` | npm dependency install, high/critical audit gate, one Vitest step, TypeScript, ESLint and build |
| `edge-collector` | Ruff and full separately packaged collector test suite |
| `edge-interop` | Real Edge Collector SNMPv3 stack against a pysnmp reference agent over loopback (`edge_collector/interop`); not part of `pytest edge_collector/tests` |
| `browser-e2e` | Own PostgreSQL/Redis, least-privilege database role, migration/bootstrap, fixture, readiness, Chromium and both Playwright specs |

All seven jobs passed at the reviewed PR head in [GitHub Actions run #80](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36141826654). GitHub validates *the commit under test*, not unrelated later changes. After merging, recheck the new `main` SHA and its workflow. The browser job uploads logs, traces and screenshots on failure; its controlled failing artifact probe is documented locally in `PHASE10_INTEGRATION_REPORT.md`.

## Concurrency / telemetry regression guidance

The Phase 10C latest-status cache is keyed by `binding_id`, unlike the older series-based latest query. A PostgreSQL `ON CONFLICT ... DO UPDATE WHERE stored.sampled_at < incoming.sampled_at` preserves strictly-newer writes under concurrent conflict locking. Equal timestamps are first-writer-wins; stale/equal writes fall back to `SELECT` and `db.refresh()` and retain HTTP 200 with the authoritative row. Relevant tests: `backend/tests/unit/test_telemetry_port_status.py`, `backend/tests/integration/test_telemetry_ordering_concurrency.py`, `backend/tests/api/test_telemetry_port_status.py`.

SNMPv3 interoperability (needs pysnmp, kept out of the normal Edge dependencies):

```sh
pip install ./edge_collector -r edge_collector/interop/requirements.txt
PYTHONPATH=. python -m edge_collector.interop.run_interop
```

See `edge_collector/interop/README.md` and `docs/implementation/ISSUE_101_SUPPORTED_PROFILES.md` for scope and results.

For Edge BER regression coverage, use `edge_collector/tests/test_snmp.py`. The test responder decodes the request structurally and tests variable-length IDs, community bytes, malformed packets and trailing data; the production collector also validates `datagram.exhausted`.
