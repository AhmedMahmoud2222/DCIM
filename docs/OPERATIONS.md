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

### Reproduced Compose configuration blocker — 2026-09-26

At source `5235f6c2866b4831b5ef30db11d5d74028072359`, `migrate`, `backend`, `celery-worker` and `celery-beat` omit `CREDENTIAL_ENCRYPTION_KEY` from their container environments. `backend/app/core/config.py::Settings` requires it; `backend/migrations/env.py`, the API and Celery load those settings. The runtime Docker image does not copy a `.env` file. A root Compose `.env` provides interpolation values only, so adding a key there alone does not fix the omission.

Reproduction without Docker: load the committed `Settings` class with `_env_file=None`, provide valid `database_url`, `redis_url` and a 32-character-or-longer `jwt_secret_key`, and omit `credential_encryption_key`. Pydantic raises `ValidationError` with `credential_encryption_key: Field required`, matching each of those four service configurations. This was executed during final integration review; a complete Compose startup was not available in that environment.

**Disposition (subsequent correction on PR #29):** the original high-severity omission is fixed by forwarding `${CREDENTIAL_ENCRYPTION_KEY:?set CREDENTIAL_ENCRYPTION_KEY to one shared Fernet key in .env}` to all four services. There is no default or generated per-service key. Actual Compose configuration checks now succeed with all required variables and fail with a named error for a missing or empty key. The regression check also fails against the old configuration. This preserves the historical finding above; it does not claim that the defect was absent at that baseline.

Fresh Docker startup of migrate/API/worker/beat and HTTP 200 readiness remain **unverified in the review workspace**, which has no Docker daemon. Before deployment, validate the complete stack in a fresh, isolated environment and inspect migration/bootstrap completion, backend readiness, worker and beat logs. Also configure writable, persistent catalog graphic storage: the current Compose file declares only the PostgreSQL data volume, while the backend runs as non-root under `/app`. Configuration validation is not production deployment approval.

### Credential-encryption key generation, sharing and recovery

1. In the installed backend Python environment, run `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` **once for the environment**. Fernet requires URL-safe base64 encoding of 32 random bytes (normally a 44-character string). Settings' minimum-length check alone does not validate an arbitrary password as a Fernet key.
2. Store that value as `CREDENTIAL_ENCRYPTION_KEY` in the private root `.env` used by Compose, or your deployment secret injection mechanism. Restrict file access (`chmod 600 .env` on Linux). All four Python services must receive the identical value; retain it across rebuilds/restarts. Do not commit it, put it in command arguments, publish `docker compose config` output, or reuse the JWT/database password as the encryption key. Use `docker compose config --quiet` for a non-printing configuration check.
3. Keep an access-controlled recovery copy separately from the database backup, with a record of which key protects which backup. Recover ciphertext together with its matching key. Losing the key makes existing encrypted credentials unrecoverable from the database alone; those credentials must then be re-provisioned from their authoritative sources.
4. **Rotation is not a simple environment-variable replacement.** The application uses one Fernet key and has no automatic keyring/rotation migration. Use a reviewed maintenance procedure: back up database and old key, stop credential readers/writers and polling, decrypt every affected stored credential with the old key and re-encrypt it with the new key, validate the migrated ciphertext, update all four services consistently, then resume and verify credential-dependent operations. Define transactional rollback and preserve the old key for retained old backups. Do not introduce mixed-key ciphertext while the application still supports only one key. Production secret-manager integration and an automated rotation tool remain separate work.

## Deployment checklist

1. **Environment and security:** create strong, unique credentials; set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, allowed CORS origins and permitted device egress. Apply network segmentation, TLS, secret rotation, authentication and monitoring required by your environment. Never use example CI/E2E credentials outside isolated disposable tests.
2. **Database ownership:** create the database under a privileged administrative owner, not `dcim_app`. Grant the application only its documented schema and table privileges; preserve audit/retention separation.
3. **Backups:** take and test a PostgreSQL backup and catalog-media backup before migration. Define RPO/RTO, retention, restore-test frequency and the service account that can perform restore.
4. **Migrations:** confirm one Alembic head; run `alembic upgrade head` as a controlled deployment step. Run `backend/scripts/bootstrap_privileged_roles.sql` with the appropriate privileged identity. Do not run destructive downgrade on production data as a casual rollback mechanism.
5. **Service startup:** start PostgreSQL and Redis, migrate/bootstrap, then API, Celery worker, Celery beat and web frontend. Complete Administrator provisioning through an approved secure procedure. Supply the shared key and validate `docker compose config --quiet` before `docker compose up --build`, then confirm a clean isolated startup and HTTP 200 readiness; the configuration-only regression check does not establish that runtime result.
6. **Health and smoke tests:** `GET /api/v1/health/live` checks the API process; `GET /api/v1/health/ready` checks both PostgreSQL and Redis and returns 503 when degraded. Exercise login, catalog read, rack elevation and a permission-denied case before admitting operator traffic.
7. **Collector enablement:** verify every target's configured IP/network and port policy, secret handling, mapping ownership, assignment and ingest ACK behavior. Do not assume a laboratory SNMP v2c test establishes enterprise SNMPv3, Modbus or arbitrary vendor support.
8. **Observability:** monitor API health, error rates, outbox dispatcher backlog, Celery retries and queue depth, device poll failures, telemetry age per binding, audit-retention execution, backup failures and disk utilization.
9. **Change evidence:** record the deployed immutable commit, successful CI URL for that SHA, migration revision, image digests, environment changes, approval and rollback plan.

## Telemetry freshness and failure simulation

The Phase 10C latest-status cache has one current row for each `binding_id`. Strict timestamp ordering prevents stale samples from overwriting newer statuses, but it **does not** prove a device is currently reachable. Operators must consider `sampled_at` and `received_at` and establish a documented stale-data policy, alarm escalation and dashboard indication for their installation. Equal timestamps follow first-writer-wins by design.

Modeled power/network impact simulation is an **analysis** feature. A graph response alone must not automatically trigger live switching, customer notifications or physical remediation. Confirm the installed topology, redundancy, out-of-band source of truth and customer-impact procedure before acting on simulation output.

For `POST /api/v1/collectors/{collector_id}/telemetry`, known mapping/assignment failures yield individual rejected ACKs while other accepted readings commit at the end of a successful request. Unexpected failures abort the transaction and prevent a successful ACK response; clients must retain unacknowledged records for retry. This differs from the discovery `/collectors/{collector_id}/ingest` endpoint, which already uses per-record savepoints and is the endpoint used by `edge_collector/client.py`. A proposed switch to savepoints in telemetry is a **protocol decision** requiring a testable contract; the discovery contract does not by itself establish that requirement for telemetry.

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
