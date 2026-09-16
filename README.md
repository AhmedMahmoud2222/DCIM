# DCIM Platform

In-house Data Center Infrastructure Management platform. This repository currently
implements **Phase 1 — Foundation** only: authentication, RBAC, audit, the location
hierarchy, the bare `ManagedAsset` identity/lifecycle anchor, the transactional Outbox,
and the surrounding cross-cutting infrastructure (observability, configuration,
migrations, Docker, CI). Rack/equipment/power/network/telemetry/alarm/spatial/AI
functionality is later-phase work and is intentionally not present — see
`PHASE1_IMPLEMENTATION.md` for exact scope and `ARCHITECTURE_REVIEW.md` for the full
platform design this phase implements a foundation of.

## Architecture Summary

Modular monolith: FastAPI (async, Python 3.11) + PostgreSQL 16 (system of record) +
Redis (Celery broker/cache) + Celery (background jobs) on the backend; React 18 +
TypeScript + Vite + Tailwind + TanStack Query on the frontend. Full rationale for every
decision is in `ARCHITECTURE_REVIEW.md` (canonical spec) and its companion revision/
red-team/validation documents.

## Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL 16 (server + client)
- Redis 7
- Docker + Docker Compose (for the containerized path; optional for local dev)

## Local Setup (without Docker)

### 1. Database

```bash
sudo -u postgres psql -c "CREATE USER dcim_app WITH PASSWORD 'dcim_dev_password';"
sudo -u postgres psql -c "CREATE DATABASE dcim OWNER dcim_app;"
sudo -u postgres psql -d dcim -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS btree_gist;'
```

Repeat for a `dcim_test` database if you'll run the test suite (see Testing below).

### 2. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then edit DATABASE_URL/JWT_SECRET_KEY for your environment

alembic upgrade head
sudo -u postgres psql -d dcim -f scripts/bootstrap_privileged_roles.sql   # one-time, superuser
python scripts/create_admin.py --email admin@example.com --name "Admin User"

uvicorn app.main:app --reload
```

API docs: `http://localhost:8000/docs`. Health: `http://localhost:8000/api/v1/health/ready`.

### 3. Background workers

```bash
cd backend && source .venv/bin/activate
celery -A app.infrastructure.celery_app worker --loglevel=info -Q default,maintenance
celery -A app.infrastructure.celery_app beat --loglevel=info   # separate terminal
```

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api` to `http://localhost:8000`.

## Local Setup (Docker Compose)

```bash
cp .env.example .env   # fill in POSTGRES_PASSWORD and JWT_SECRET_KEY
docker compose up --build
```

This starts Postgres, Redis, runs migrations (`migrate` service, runs once), then starts
the API, Celery worker, Celery beat, and the built frontend (served by nginx on
`:8080`, proxying `/api` to the backend). **Not executed in this development session**
(no Docker daemon available in this sandbox) — verified by config review and by every
container's Dockerfile/compose definition matching the independently-verified local
setup, but not by an actual `docker compose up` run. Treat as requiring a real
environment's validation before being relied upon for a first deployment.

After first startup, run once (superuser):

```bash
docker compose exec postgres psql -U dcim_app -d dcim -f /dev/stdin < backend/scripts/bootstrap_privileged_roles.sql
docker compose exec backend python scripts/create_admin.py --email admin@example.com --name "Admin User"
```

## Database Migrations

```bash
cd backend && source .venv/bin/activate
alembic upgrade head           # apply
alembic downgrade -1           # roll back one revision
alembic revision --autogenerate -m "description"   # generate a new migration
```

Migrations never run automatically at application startup — see `PHASE1_IMPLEMENTATION.md`
for the migration/startup safety rationale.

## Tests

Requires a running `dcim_test` database (extensions enabled, same as `dcim`) and Redis.

```bash
cd backend && source .venv/bin/activate
alembic upgrade head   # against dcim_test — see conftest.py for the DATABASE_URL default
pytest -q
```

59 tests: unit (pure logic — lifecycle rules, concurrency helpers, JWT, idempotency
hashing), integration (real PostgreSQL — DB constraints, outbox atomicity, audit
append-only enforcement), and API (real HTTP contract through FastAPI's ASGI transport —
auth, RBAC, concurrency, security).

## Lint / Type Checking

```bash
# backend
cd backend && source .venv/bin/activate
ruff check app tests
mypy app

# frontend
cd frontend
npm run typecheck
npx eslint . --ext ts,tsx
```

## Troubleshooting

- **`alembic upgrade head` fails with "permission denied to create role"** — this is
  expected if you're running it as the application's own `dcim_app` role (deliberately
  not granted `CREATEROLE`, §33/§40 of the Phase 1 scope — least privilege). Run
  `scripts/bootstrap_privileged_roles.sql` separately, as a superuser, once per
  environment; it is not part of the Alembic migration chain on purpose.
- **`/api/v1/health/ready` reports `"redis": "down"`** — confirm `redis-server` is
  running and `REDIS_URL` in `.env` points at it.
- **A `409 Conflict` on `PATCH /api/v1/rooms/{id}`** — this is the optimistic-concurrency
  mechanism working as designed (§17 of the Phase 1 prompt): your `If-Match` header
  carried a stale `version`. Re-fetch the resource and retry with its current version.
- **`428 Precondition Required` on `PATCH /api/v1/rooms/{id}`** — that endpoint requires
  an `If-Match` header; there is no unconditional update path for a concurrency-tracked
  resource.

## Further Reading

`ARCHITECTURE_REVIEW.md` (canonical architecture, v1.3), `PHASE1_BASELINE.md` (repository
assessment before this phase began), `PHASE1_IMPLEMENTATION.md` (what Phase 1 actually
built, mapped to architecture sections), `PHASE1_TRACEABILITY_MATRIX.md`,
`PHASE1_DEVIATIONS.md`, `PHASE1_IMPLEMENTATION_REPORT.md` (full Phase 1 completion report
and gate verdict).
