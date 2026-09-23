# DCIM Platform

In-house Data Center Infrastructure Management platform.

**Current status, in one paragraph — see
[`docs/2026-09-23-current-state-and-roadmap.md`](docs/2026-09-23-current-state-and-roadmap.md) for the
full, dated breakdown.** The `main` branch implements authentication/RBAC/audit, the location hierarchy,
`ManagedAsset`, rack/equipment inventory with legacy catalog authoring, floor plan import and 2D spatial
layout, power topology, integrations/collectors (ICMP/SNMP/REST) with discovery, an isolated Edge Collector
feeding telemetry and threshold/availability alarms, and an operational dashboard — this is the historical
Phase 1–10 roadmap from `ARCHITECTURE_REVIEW.md` §47, fully implemented and tested. Network topology and a
3D room layout exist **only** on `codex/commercial-ui-uplift-v1` (not yet on `main`). A structured,
versioned Asset Catalog Designer ("Phase 10A") exists **only** as a stack of draft, unmerged pull requests
(`claude/phase10a-*` branches, [#14](https://github.com/AhmedMahmoud2222/DCIM/pull/14),
[#15](https://github.com/AhmedMahmoud2222/DCIM/pull/15),
[#16](https://github.com/AhmedMahmoud2222/DCIM/pull/16)) — backend only so far, no UI, none of it merged
or ready for review. See the addendum linked above before assuming any capability is present on a branch
you haven't checked.

## Architecture Summary

Modular monolith: FastAPI (async, Python 3.11) + PostgreSQL 16 (system of record) +
Redis (Celery broker/cache) + Celery (background jobs) on the backend; React 18 +
TypeScript + Vite + Tailwind + TanStack Query on the frontend. Full rationale for every
decision is in `ARCHITECTURE_REVIEW.md` (canonical spec) and its companion revision/
red-team/validation documents.

## Authoritative Branches

| Branch | What it is |
|---|---|
| `main` | The merged, deployable baseline — the historical Phase 1–10 roadmap only (§2 of the addendum). No network topology, no 3D layout, no catalog designer. |
| `codex/commercial-ui-uplift-v1` | `main` plus network topology and the 3D room layout. Not yet merged back into `main`. |
| `claude/phase10a-*` | The in-progress Asset Catalog Designer, as a stack of draft PRs ([#14](https://github.com/AhmedMahmoud2222/DCIM/pull/14) → [#15](https://github.com/AhmedMahmoud2222/DCIM/pull/15) → [#16](https://github.com/AhmedMahmoud2222/DCIM/pull/16)) on top of the same base `codex/commercial-ui-uplift-v1` branched from. Backend only; none of it is merged or marked ready for review. |

See [`docs/2026-09-23-current-state-and-roadmap.md`](docs/2026-09-23-current-state-and-roadmap.md) for the
full capability breakdown per branch, the Phase 10A PR chain and its known open design issue, and precise
language on what the network/3D and telemetry features do and do not claim.

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
sudo -u postgres psql -c "CREATE DATABASE dcim;"
sudo -u postgres psql -d dcim -c "GRANT CREATE, USAGE ON SCHEMA public TO dcim_app;"
sudo -u postgres psql -d dcim -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS btree_gist;'
```

**Do not use `CREATE DATABASE dcim OWNER dcim_app`.** PostgreSQL grants the *database
owner* an implicit right to `DROP TABLE` any table in that database, regardless of who
owns the individual table or what has been REVOKEd — empirically confirmed during the
Finding C1 correction (`PHASE1_CORRECTION_REPORT.md`): with `dcim_app` as the database
owner, it could still `DROP TABLE` `audit_log`'s partitions even after `audit_log` itself
was transferred to `dcim_retention_admin` and every ordinary privilege was revoked. Only
the superuser (`postgres`) owns the database; `dcim_app` gets exactly `CREATE, USAGE` on
the `public` schema, which is enough to run migrations and create/own its own tables, but
not enough to bypass another role's table ownership.

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
cp .env.example .env   # fill in POSTGRES_PASSWORD, DCIM_APP_PASSWORD, and JWT_SECRET_KEY
docker compose up --build
```

This starts Postgres (bootstrapping the `dcim_app` application role as an ordinary,
non-superuser role — see `backend/scripts/docker-initdb/01-create-app-role.sh` and
`PHASE1_CORRECTION_REPORT.md` Finding C1), Redis, runs migrations (`migrate` service),
runs the one-time privileged `bootstrap-privileges` service (audit_log ownership/grants,
as the real Postgres superuser), then starts the API, Celery worker, Celery beat, and the
built frontend (served by nginx on `:8080`, proxying `/api` to the backend). **Not
executed in this development session** (no Docker daemon available in this sandbox) —
verified by config review and by every container's Dockerfile/compose definition matching
the independently-verified local setup, but not by an actual `docker compose up` run.
Treat as requiring a real environment's validation before being relied upon for a first
deployment.

After first startup, run once:

```bash
docker compose exec backend python scripts/create_admin.py --email admin@example.com --name "Admin User"
```

(The privileged audit-log bootstrap step above already runs automatically as part of
`docker compose up`, via the `bootstrap-privileges` service — no manual superuser step is
needed in the Docker path, unlike the non-Docker local setup.)

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
PYTHONPATH=.. pytest -q   # PYTHONPATH is the repo root — see the note below
```

Hundreds of tests and growing every phase — run the command above for the current count rather than
trusting a number in this file, which will always be stale by the time you read it. Categories: unit (pure
logic — lifecycle rules, concurrency helpers, JWT, idempotency hashing), integration (real PostgreSQL — DB
constraints, outbox atomicity, audit append-only enforcement, genuine two-connection concurrency races),
and API (real HTTP contract through FastAPI's ASGI transport — auth, RBAC, concurrency, security).

**`PYTHONPATH` must include the repository root**, not just `backend/`.
`tests/unit/test_monitoring_policy_contract.py` imports the sibling `edge_collector/` package directly
(deliberately not installed into the backend's own distribution — it is developed and validated as an
independent package, per its isolation boundary in
`docs/superpowers/specs/2026-09-18-dcim-mvp-v0.1-design.md`). Omitting `PYTHONPATH` fails that one file's
collection with `ModuleNotFoundError: No module named 'edge_collector'` — not a code defect, just an
invocation gap; `.github/workflows/ci.yml` sets `PYTHONPATH: ${{ github.workspace }}` for exactly this
reason on every CI run.

**ICMP driver tests require `CAP_NET_RAW`.** `app/application/drivers/icmp.py` opens a
genuine `SOCK_RAW`/`IPPROTO_ICMP` socket (not a shell-out to `ping`), which the kernel
only permits to `root` or a process holding `CAP_NET_RAW`. Running `pytest` as an
unprivileged, non-root user without that capability fails `tests/unit/test_drivers.py`'s
ICMP tests and two of `test_collectors.py`'s poll-now tests with `PermissionError`. Grant
it once before running tests, rather than running the whole test process as root:

```bash
sudo setcap cap_net_raw+ep "$(readlink -f "$(command -v python3)")"
```

CI (`.github/workflows/ci.yml`) does this on every run, since GitHub-hosted runners are
non-root by default. This is a real operational requirement for wherever this driver
actually runs (the central collector process today, or a future Edge Collector), not
something to silently work around.

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

**Start with [`docs/2026-09-23-current-state-and-roadmap.md`](docs/2026-09-23-current-state-and-roadmap.md)**
— the dated, maintained answer to "what actually works, on which branch, right now," including the Phase
10A draft-PR chain and its one known open design issue. Everything below it is historical record, correct
for its own point in time but not maintained as a current-status reference:

- `ARCHITECTURE_REVIEW.md` (canonical architecture, v1.3) — the full platform design, historical Phase
  0–14 roadmap (§47), including phases not yet built.
- `PHASE1_BASELINE.md`, `PHASE1_IMPLEMENTATION.md`, `PHASE1_TRACEABILITY_MATRIX.md`, `PHASE1_DEVIATIONS.md`,
  `PHASE1_IMPLEMENTATION_REPORT.md` — Phase 1's own completion record.
- The root-level `PHASE2_*.md` through `PHASE8_*.md`, `PRE_MVP_CONSOLIDATION_REPORT.md`,
  `BRANCH_RECONCILIATION_REPORT.md`, `MVP_*.md` — each later phase's and consolidation gate's own dated
  record, in the same historical-not-current spirit.
- `docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-design.md` and
  `docs/superpowers/plans/2026-09-23-phase-10a-asset-catalog-designer-plan.md` — Phase 10A's authoritative
  specification and implementation plan.
