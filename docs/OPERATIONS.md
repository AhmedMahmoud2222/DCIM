# Operations and deployment guide

**Scope:** operational considerations for the current DCIM source on the PR #29 integration branch. This is not a production deployment certification. Test the complete deployment process in a representative environment and supply your own network, identity, backup and incident-response controls.

## Runtime components

| Service | Role | Key checks |
|---|---|---|
| PostgreSQL 16 | Authoritative inventory, topology, catalog, telemetry mapping/cache, audit and outbox | Connections, storage, migrations, table ownership, backups and recovery |
| Redis 7 | Celery broker/cache and a readiness dependency | Connectivity, memory policy and persistence appropriate for the environment |
| FastAPI | Versioned `/api/v1` HTTP API | Liveness, dependency readiness, authenticated endpoints, app errors |
| Celery worker and beat | Background jobs and scheduling | Queue depth, failed tasks, retry/dead-letter behavior and worker availability |
| React/Vite build | Operator and Administrator UI | Auth flow, API proxy/ingress, content security, browser regression tests |
| Central/Edge collectors | Measurements, device discovery/polling and store-and-forward telemetry | Assignment, connectivity/allowlists, deduplication, backoff and telemetry freshness |
| Local catalog graphic storage | Content-addressed catalog graphics if the local storage backend is configured | Persistent volumes, media backup, controlled serving permissions |

Check `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` and your environment configuration for actual service names, volume mappings and ports. The repository's Compose path is a development/reference deployment; do not assume it supplies an external TLS terminator, HA database, centralized secret manager or disaster-recovery solution.

## Deployment checklist

1. **Environment and security:** create strong, unique credentials; set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, allowed CORS origins and permitted device egress. Apply network segmentation, TLS, secret rotation, authentication and monitoring required by your environment. Never use example CI/E2E credentials outside isolated disposable tests.
2. **Database ownership:** create the database under a privileged administrative owner, not `dcim_app`. Grant the application only its documented schema and table privileges; preserve audit/retention separation.
3. **Backups:** take and test a PostgreSQL backup and catalog-media backup before migration. Define RPO/RTO, retention, restore-test frequency and the service account that can perform restore.
4. **Migrations:** confirm one Alembic head; run `alembic upgrade head` as a controlled deployment step. Run `backend/scripts/bootstrap_privileged_roles.sql` with the appropriate privileged identity. Do not run destructive downgrade on production data as a casual rollback mechanism.
5. **Service startup:** start PostgreSQL and Redis, migrate/bootstrap, then API, Celery worker, Celery beat and web frontend. Complete Administrator provisioning through an approved secure procedure. For development, `docker compose up --build` and `docker compose exec backend python scripts/create_admin.py ...` show the reference flow.
6. **Health and smoke tests:** `GET /api/v1/health/live` checks the API process; `GET /api/v1/health/ready` checks both PostgreSQL and Redis and returns 503 when degraded. Exercise login, catalog read, rack elevation and a permission-denied case before admitting operator traffic.
7. **Collector enablement:** verify every target's configured IP/network and port policy, secret handling, mapping ownership, assignment and ingest ACK behavior. Do not assume a laboratory SNMP v2c test establishes enterprise SNMPv3, Modbus or arbitrary vendor support.
8. **Observability:** monitor API health, error rates, outbox dispatcher backlog, Celery retries and queue depth, device poll failures, telemetry age per binding, audit-retention execution, backup failures and disk utilization.
9. **Change evidence:** record the deployed immutable commit, successful CI URL for that SHA, migration revision, image digests, environment changes, approval and rollback plan.

## Telemetry freshness and failure simulation

The Phase 10C latest-status cache has one current row for each `binding_id`. Strict timestamp ordering prevents stale samples from overwriting newer statuses, but it **does not** prove a device is currently reachable. Operators must consider `sampled_at` and `received_at` and establish a documented stale-data policy, alarm escalation and dashboard indication for their installation. Equal timestamps follow first-writer-wins by design.

Modeled power/network impact simulation is an **analysis** feature. A graph response alone must not automatically trigger live switching, customer notifications or physical remediation. Confirm the installed topology, redundancy, out-of-band source of truth and customer-impact procedure before acting on simulation output.

For the collector batch endpoint, known mapping/assignment failures yield individual rejected ACKs while other accepted readings commit at the end of a successful request. Unexpected failures abort the transaction. Document the expected retry behavior for an HTTP failure; a proposed switch to per-record savepoints is a **protocol decision** requiring a testable contract.

## Testing deployment changes

At the reviewed PR #29 HEAD, GitHub Actions includes Python 3.11–3.14 backend/migration gates, frontend/Vitest, Edge Collector and isolated real-backend Playwright, all verified green in [run #80](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/36141826654). Such CI does not simulate customer workload, high availability, external equipment outages, secret rotation or real restore operations.

Before a production cutover, complete at least one staging migration from a representative snapshot, rollback/redeploy rehearsal, restore drill, authentic device polling, alarm and escalation drill, role/permission exercise and a load/fault test appropriate for the data center's scale. Record ownership and sign-off of each check.

## Incident response starting points

- **Readiness is degraded:** check the returned dependency states; inspect PostgreSQL connections and Redis availability before restarting API processes. `/live` may remain healthy by design.
- **New telemetry appears stale:** inspect edge connectivity, collector assignment and retries, clock synchronization, mapping/binding IDs and the cached row's sample/receive timestamps. Do not clear the latest cache to mask an ingest-ordering issue.
- **Audit/retention failures:** check privileged ownership/bootstrap, migration state and partition privileges. Do not grant the normal application database-owner powers as a workaround.
- **Browser tests fail during deployment:** distinguish server startup/readiness, fixture seeding, API-contract failures and actual browser UI regressions. The CI browser job collects backend/frontend logs plus Playwright traces/screenshots when it fails.
- **SNMP response errors:** check the target allowlist, community configuration, OID mapping and BER parser failures. Unexpected trailing data should be rejected, not silently accepted.

## Deployment evidence register

Record, in your operations change record: date/time and timezone, `main` commit SHA, relevant PR and CI run, migration head, deployed image digests, database backup/restore test, health checks, role verification, Edge Collector connectivity checks, browser smoke test, monitoring ownership and formal go/no-go. Historical phase reports document development milestones and are not a replacement for that environment-specific register.
