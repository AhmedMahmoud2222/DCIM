# DCIM Deployment Implementation Guide

**Version 1.0** | **Date**: 2026-09-26 | **Status**: Implementation PR (Not yet deployed)

This document describes the secure, auditable Docker Compose deployment infrastructure for DCIM production and staging environments.

---

## Executive Summary

This implementation provides:
- **Exact-SHA verification** via GitHub API (not user-supplied input)
- **Approval-gated deployment** in GitHub Actions with environment protection
- **Production-hardened Compose configuration** with persistent storage, health checks, and restricted networking
- **Comprehensive Linux deployment script** with backup, rollback, and audit trail
- **Staging-first validation** with negative test cases (invalid SHAs, missing checks, etc.)
- **Zero production credentials in Git** — all secrets supplied at runtime only
- **Fail-closed design** — deployment stops if any verification step fails

## Architecture Overview

### Components

1. **GitHub API Verification** (`.github/scripts/verify_release_sha.py`)
   - Queries GitHub API to confirm release SHA meets all deployment gates
   - Validates SHA exists, is on main branch, has all required CI checks passing
   - Fails immediately if any check is missing, cancelled, or failed
   - Returns exit code 1 if verification fails (workflow stops)

2. **Production Docker Compose** (`docker-compose.production.yml`)
   - 8 services: PostgreSQL, Redis, migrations, bootstrap-privileges, backend (FastAPI), celery-worker, celery-beat, frontend (React)
   - Persistent host-mounted PostgreSQL storage (no Docker volume)
   - All ports restricted to localhost (127.0.0.1)
   - Internal bridge network (172.20.0.0/16) for service communication
   - JSON logging with rotation (100m-200m max-size, 1-5 max-file)
   - Restart policies: "unless-stopped" for persistent services, "no" for one-shot jobs

3. **Linux Deployment Script** (`scripts/deploy-docker-compose.sh`, ~450 lines)
   - Validates deployment prerequisites (directories, Docker, environment variables)
   - Acquires deployment lock (prevents concurrent deployments)
   - Backs up current deployment state
   - Checks out exact release SHA with validation (exists, is on main)
   - Prepares .env file with production secrets (mode 600)
   - Pulls/builds Docker images
   - Starts services with dependency ordering
   - Waits for health checks (60-second timeout)
   - Records deployment evidence (.deployment-sha, deployments.log)
   - Provides rollback via `--rollback` flag

4. **GitHub Actions Workflow** (`.github/workflows/deploy.yml`, ~500 lines)
   - Manual dispatch trigger: requires release_sha and environment (staging/production)
   - **verify-release** job: Runs GitHub API verification script
   - **request-approval** job: For production only, requires manual approval via GitHub environment
   - **validate-environment** job: Validates Compose syntax, deployment script syntax
   - **staging-deployment** job: Simulates Docker deployment, verifies services, smoke tests (no actual Docker on GitHub runner)
   - **record-deployment** job: Creates deployment-record.md artifact with audit trail
   - **deployment-summary** job: Outputs summary, fails if any prior job failed

---

## Security Model

### Threat Model

| Threat | Mitigation | Status |
|--------|-----------|--------|
| Deploying non-existent SHA | GitHub API verification confirms SHA exists | Implemented |
| Deploying unapproved code | API verifies SHA is on main branch | Implemented |
| Deploying code without passing CI | API verifies all required checks passed | Implemented |
| Deploying cancelled/skipped CI runs | Script explicitly rejects cancelled/skipped status | Implemented |
| Concurrent deployments corrupting state | Deployment lock file (300s timeout) prevents overlap | Implemented |
| Loss of current deployment state | Backup functionality before deployment | Implemented |
| Cannot restore previous deployment | .deployment-sha recorded for rollback | Implemented |
| Production secrets leaked in Git | All secrets supplied at runtime only, never committed | Implemented |
| Production secrets leaked in workflow logs | verify_release_sha.py does not print secrets | Implemented |
| Unauthorized deployment trigger | GitHub Actions requires workflow dispatch authorization | Implemented |
| Unauthorized production approval | GitHub environment approval gate (requires owner setup) | Implemented - pending owner configuration |
| Deployment script executed with wrong identity | Run as unprivileged deploy user (requires setup) | Documented below |
| Docker daemon accessible to unauthorized users | Restricted to deploy user (requires firewall/file permissions) | Documented below |
| PostgreSQL accessible from internet | All ports restricted to 127.0.0.1 (requires firewall) | Documented below |
| Service restarts cause data loss | Persistent host storage for PostgreSQL (not ephemeral volumes) | Implemented |
| Encryption keys lost during deployment | Script backs up .env with keys (restores on rollback) | Implemented |
| No audit trail of deployments | deployments.log records timestamp, SHA, deployer, health status | Implemented |
| Rollback executed without verification | Script validates SHA exists before rolling back | Implemented |

### Cryptographic Requirements

- **Fernet key** (CREDENTIAL_ENCRYPTION_KEY): 44+ base64-encoded characters (32-byte symmetric key)
  - Used for encrypting credentials in PostgreSQL
  - Must be backed up and restored during rollback (script handles via .env backup)
  - **Do not rotate** during deployment (keys would be inconsistent)
  - **Do not auto-generate** — must be pre-provisioned on deployment server
  - Loss of key = permanent loss of encrypted credentials

- **JWT secret** (JWT_SECRET_KEY): Used for session signing
  - Rotation supported (old tokens remain valid if key is retained)
  - Script does not rotate; manage separately if needed

---

## GitHub Configuration Required

### Permissions

**GITHUB_TOKEN scope** (in GitHub Actions):
- `public_repo` or `repo` (minimum: can read commit, check runs, workflow runs)
- Provided automatically by GitHub Actions
- **Never** store personal access tokens in repository secrets

**Workflow permissions** (repository settings):
- `actions: read` — to query workflow runs
- `checks: read` — to query check runs  
- `contents: read` — to access repository commits
- Already configured in workflow file

### Environment Approval Gate (For Production)

1. Go to Repository → Settings → Environments → New environment
2. Create environment: **`production-deployment`**
3. Set deployment branch restriction: **`main`** (only main can trigger)
4. Add required reviewers: *(choose approvers)*
5. Enable "Require approval before deployment": ✓
6. Timeout: **30 days** (GitHub default)

When production deployment is triggered:
- Workflow pauses at `request-approval` job
- GitHub notifies reviewers
- Reviewer must approve in GitHub Actions UI (or reject)
- Approval is recorded in GitHub audit log
- Only then does `staging-deployment` run (or `production-deployment` if extended)

**Note**: This requires Repository Admin access to configure.

---

## Production Prerequisites

### Server Setup

**Operating System**: Linux (tested on Fedora, RHEL, Ubuntu)

**Network Configuration**:
- PostgreSQL (port 5432): Accessible only to DCIM backend (not internet)
- Redis (port 6379): Accessible only to DCIM backend and workers (not internet)
- Backend (port 8000): Accessible only to reverse proxy (not internet)
- Frontend (port 8080): Accessible only to reverse proxy (not internet)
- **Reverse proxy** (e.g., nginx, Caddy): Handles TLS termination, public access

**Firewall Rules** (example for iptables):
```bash
# Allow only localhost access to Docker services
iptables -A INPUT -i lo -p tcp --dport 5432 -j ACCEPT
iptables -A INPUT -p tcp --dport 5432 -j REJECT
iptables -A INPUT -i lo -p tcp --dport 6379 -j ACCEPT
iptables -A INPUT -p tcp --dport 6379 -j REJECT
iptables -A INPUT -i lo -p tcp --dport 8000 -j ACCEPT
iptables -A INPUT -p tcp --dport 8000 -j REJECT
iptables -A INPUT -i lo -p tcp --dport 8080 -j ACCEPT
iptables -A INPUT -p tcp --dport 8080 -j REJECT
```

### Directory Structure

```
/opt/dcim/                          # DEPLOY_DIR (must exist, be writable by deploy user)
├── docker-compose.production.yml
├── .env                            # (created at deployment time, mode 600)
├── .deployment-sha                 # (records current SHA for rollback)
├── .deployment-info                # (metadata from last deployment)
├── .deployment.lock                # (acquired during deployment, released after)
└── [repository code]               # (checked out at deployment time)

/var/backups/dcim/                  # BACKUP_DIR (must be writable by deploy user)
└── deployment-{timestamp}/
    ├── .env.backup                 # (previous .env, mode 600)
    └── deployment-info.txt         # (timestamp, previous SHA)

/var/log/dcim/                      # LOG_DIR (must be writable by deploy user)
├── deployment.log                  # (all deployment script output)
└── deployments.log                 # (one line per deployment: timestamp|SHA|deployer|status)
```

### Deployment Identity

**System user** (create if not exists):
```bash
sudo useradd -m -s /bin/bash -c "DCIM deployment" dcim-deploy
```

**Directory ownership**:
```bash
sudo chown -R dcim-deploy:dcim-deploy /opt/dcim /var/backups/dcim /var/log/dcim
sudo chmod 750 /opt/dcim /var/backups/dcim /var/log/dcim
```

**Sudo privileges** (minimal, if needed):
```bash
# In /etc/sudoers.d/dcim-deploy (via visudo):
dcim-deploy ALL=(ALL) NOPASSWD: /usr/bin/docker compose -f /opt/dcim/docker-compose.production.yml *
# This allows running docker compose without password (if Docker requires sudo)
# Alternative: add dcim-deploy to docker group (less isolated, but simpler)
sudo usermod -aG docker dcim-deploy
```

### Docker Installation

```bash
# Install Docker Engine (official documentation)
# https://docs.docker.com/engine/install/

# Verify Docker and Compose are available:
docker --version
docker compose version

# Grant access to dcim-deploy user (if not using sudo):
sudo usermod -aG docker dcim-deploy
# (Requires logout/login for group membership to take effect)
```

### Secrets Management

**Environment variables** (must be provided before deployment script runs):
```bash
export POSTGRES_PASSWORD="<strong-random-64-char-password>"
export DCIM_APP_PASSWORD="<strong-random-64-char-password>"
export JWT_SECRET_KEY="<strong-random-64-char-secret>"
export CREDENTIAL_ENCRYPTION_KEY="<fernet-44-char-base64-key>"
export POSTGRES_DB="dcim"                         # (optional, default: dcim)
export POSTGRES_DATA_PATH="/var/lib/dcim/postgres" # (optional, default shown)
export CORS_ALLOWED_ORIGINS='["https://dcim.example.com"]' # (production domain)
export ENVIRONMENT="production"                   # (set by script)
export DEPLOY_DIR="/opt/dcim"                     # (optional, default shown)
export BACKUP_DIR="/var/backups/dcim"             # (optional, default shown)
export LOG_DIR="/var/log/dcim"                    # (optional, default shown)
```

**Secrets should be supplied via**:
- GitHub Actions Secrets (for workflow execution)
- `.env` file created at deployment time (mode 600, not committed to Git)
- Secure credential vaults (e.g., HashiCorp Vault, AWS Secrets Manager) — reference implementation uses GitHub Actions Secrets + runtime export

**Never**:
- Commit `.env` to Git
- Print secrets in logs
- Store plaintext secrets in configuration files
- Rotate Fernet key automatically (causes decryption failures)

### PostgreSQL Storage

**Persistent storage** (required for production):
```bash
# Create data directory on production server:
sudo mkdir -p /var/lib/dcim/postgres
sudo chown dcim-deploy:dcim-deploy /var/lib/dcim/postgres
sudo chmod 700 /var/lib/dcim/postgres
```

**Backup strategy** (outside scope of this script):
- Backup PostgreSQL data directory weekly (e.g., via rsync, tar, or pg_dump)
- Store backups on separate storage (not on deployment server)
- Test restore procedure quarterly
- Example: `pg_dump dcim | gzip > /backup/dcim-$(date +%Y%m%d).sql.gz`

**Volume mounts** (in docker-compose.production.yml):
```yaml
volumes:
  - /var/lib/dcim/postgres:/var/lib/postgresql/data
```
This mounts host storage, not Docker named volume, so data persists across container restarts.

---

## Known Limitations

1. **No automatic production deployment**
   - Workflow simulates deployment (no Docker daemon on GitHub runner)
   - Actual deployment requires manual trigger on production server
   - Example: `bash scripts/deploy-docker-compose.sh <sha> production`

2. **No GitHub runner access to private network**
   - Staging validation is in GitHub Actions (public runner environment)
   - Cannot actually pull Docker images from private registry in workflow
   - Cannot test actual database connection from runner
   - Staging deployment is simulated (logs commands that would run)

3. **Fernet key not rotated**
   - Automatic rotation would break credential decryption
   - Rotation is out of scope for this deployment script
   - Manual key rotation requires credential re-encryption (separate process)

4. **No automatic monitoring/alerting**
   - Script records deployment evidence but doesn't integrate with monitoring
   - Operator must check deployments.log and health status manually
   - Recommend integrating with monitoring system (e.g., Datadog, Prometheus)

5. **No automatic rollback on health check failure**
   - Script exits with error if health checks fail
   - Operator must manually review logs and decide rollback
   - Rollback is manual: `bash scripts/deploy-docker-compose.sh --rollback`

6. **No zero-downtime deployment**
   - Script stops services, deploys code, starts services
   - Requests during deployment window will fail
   - Recommend scheduling deployments during low-traffic windows

7. **Lock file cleanup on hard crash**
   - Lock file is released on normal exit (trap mechanism)
   - If process killed with -9 or server crashes, lock persists for 300 seconds
   - Manual cleanup: `rm /opt/dcim/.deployment.lock` (if older than 300 seconds)

---

## Operational Runbook

### Normal Deployment (Staging)

1. **Prepare release candidate**:
   ```bash
   git log --oneline main | head -1  # Get latest main SHA
   ```

2. **Trigger GitHub Actions workflow**:
   - Go to Actions → Deploy DCIM
   - Click "Run workflow"
   - Enter: `release_sha` (12+ hex chars), `environment: staging`
   - Click "Run workflow"

3. **Monitor verification**:
   - Watch GitHub Actions → Deploy DCIM → [run]
   - `verify-release` job should pass
   - `validate-environment` job should pass

4. **Review deployment summary**:
   - Check `deployment-summary` job output
   - Verify all checks passed
   - Note deployment record artifact

5. **Manual staging test** (if infrastructure supports):
   - SSH to staging server: `ssh deploy@staging.dcim.internal`
   - Test application: `curl http://localhost:8000/api/v1/health/ready`
   - Review logs: `docker compose -f docker-compose.production.yml logs -f backend`

### Normal Deployment (Production)

1. **Complete staging validation** (see above)

2. **Prepare production secrets**:
   ```bash
   export POSTGRES_PASSWORD="..."
   export DCIM_APP_PASSWORD="..."
   export JWT_SECRET_KEY="..."
   export CREDENTIAL_ENCRYPTION_KEY="..."  # Must be existing key, not new
   ```

3. **Trigger GitHub Actions workflow**:
   - Go to Actions → Deploy DCIM
   - Click "Run workflow"
   - Enter: `release_sha` (same as staging), `environment: production`
   - Click "Run workflow"

4. **Approve deployment** (if reviewer configured):
   - GitHub sends notification to approvers
   - Reviewer goes to Actions → Deploy DCIM → [run]
   - Clicks "Review deployments" and "Approve"
   - Deployment proceeds

5. **Monitor verification**:
   - `verify-release` job runs
   - `request-approval` job waits for manual approval
   - After approval, deployment proceeds

6. **Verify production deployment** (post-deployment):
   - SSH to production: `ssh deploy@dcim.prod.internal`
   - Check services: `docker compose -f docker-compose.production.yml ps`
   - Test API: `curl http://localhost:8000/api/v1/health/ready`
   - Monitor logs: `tail -f /var/log/dcim/deployments.log`

### Rollback (Emergency)

1. **Identify issue**:
   - Health checks failed OR
   - Services not responding OR
   - Errors in `docker compose logs`

2. **Execute rollback** (on production server):
   ```bash
   cd /opt/dcim
   bash scripts/deploy-docker-compose.sh --rollback
   ```

3. **Verify rollback**:
   ```bash
   cat .deployment-sha              # Check current SHA
   docker compose ps                # Verify services running
   curl http://localhost:8000/api/v1/health/ready  # Test health
   tail -f /var/log/dcim/deployments.log           # Check rollback recorded
   ```

4. **Post-incident review**:
   - Review what failed
   - Review deployment logs: `cat /var/log/dcim/deployment.log`
   - Determine root cause (bug, infrastructure, configuration)
   - Fix and re-deploy

### Monitoring & Observability

**Log locations**:
- Deployment script: `/var/log/dcim/deployment.log` (all deployment activity)
- Deployment history: `/var/log/dcim/deployments.log` (one line per deployment)
- Docker container logs: `docker compose -f docker-compose.production.yml logs <service>`
- Backup records: `/var/backups/dcim/deployment-{timestamp}/deployment-info.txt`

**Health check endpoints**:
```bash
# Liveness (server is running)
curl http://localhost:8000/api/v1/health/live

# Readiness (server can handle requests)
curl http://localhost:8000/api/v1/health/ready

# Full status
curl http://localhost:8000/api/v1/health/ready -v
```

**Common failure modes**:
| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `health check timeout` | Services not starting | Check `docker compose logs`, verify secrets |
| `connection refused on 5432` | PostgreSQL not healthy | Check `docker compose ps`, `docker compose logs postgres` |
| `migration failed` | Database schema incompatible | Manual review required, possible rollback needed |
| `deployment lock held` | Previous deployment crashed | Manual cleanup: `rm /opt/dcim/.deployment.lock` (verify age first) |
| `.env: permission denied` | File mode issue | Check: `ls -l /opt/dcim/.env` (should be 600) |

---

## Testing & Validation

### Test Cases (Covered by GitHub Actions Workflow)

1. **Valid deployment** ✓
   - Existing SHA on main branch
   - All required checks passed
   - Expected: deployment-result=success

2. **Nonexistent SHA** ✓
   - SHA does not exist in repository
   - Expected: verify-release fails, workflow stops

3. **SHA not on main** ✓
   - SHA on different branch, not reachable from main
   - Expected: verify-release fails, workflow stops

4. **Missing CI check** ✓
   - Required check (e.g., "backend suite (Python 3.12)") not found
   - Expected: verify-release fails, workflow stops

5. **Failed CI check** ✓
   - Required check has conclusion="failure"
   - Expected: verify-release fails, workflow stops

6. **Cancelled CI run** ✓
   - CI check status="completed" but conclusion="cancelled"
   - Expected: verify-release fails, workflow stops

7. **Skipped CI run** ✓
   - CI check conclusion="skipped"
   - Expected: verify-release fails, workflow stops

8. **Stale/superseded run** ✓
   - Old workflow run from same SHA (superseded by newer)
   - Expected: verify-release checks latest run

### Staging Validation (GitHub Actions)

These tests run in `validate-environment` job:
- Docker Compose configuration syntax validation (no Docker daemon needed)
- Deployment script syntax validation (bash -n)
- Directory permission checks (simulated)

And in `staging-deployment` job:
- Simulated Docker image pull/build
- Simulated service startup
- Simulated health check polling
- Simulated smoke tests (GET /api/v1/health/live, /api/v1/health/ready)

**Note**: Staging deployment in GitHub Actions is simulated (echoes commands, no actual Docker). Real staging validation requires manual trigger on staging infrastructure or self-hosted runner with Docker.

---

## Threat Model Summary

| Layer | Component | Threat | Mitigation |
|-------|-----------|--------|-----------|
| **GitHub** | API Verification | Deploy unapproved code | API queries main branch + all required checks |
| **GitHub** | Environment Approval | Unauthorized production deployment | GitHub environment approval gate (owner-configured) |
| **GitHub** | Secrets | Leaking production credentials | Never printed in logs, supplied at runtime |
| **Workflow** | Dispatch trigger | Unauthorized workflow execution | GitHub Actions authorization (user must have push permission) |
| **Linux** | Deployment script | Wrong SHA deployed | Script validates SHA exists + is on main |
| **Linux** | Concurrency | Data corruption from concurrent deploys | Deployment lock file (300s timeout) |
| **Linux** | State loss | Cannot recover from failed deployment | Backup functionality + rollback capability |
| **Linux** | Identity | Script running as wrong user | Run as unprivileged dcim-deploy (requires setup) |
| **Docker** | Networking | Unauthorized access to services | All ports localhost-only (requires firewall) |
| **Docker** | Storage | PostgreSQL data loss | Persistent host-mounted storage (not ephemeral volume) |
| **Docker** | Encryption | Encrypted credentials lost | .env backup + restore on rollback |
| **Docker** | Restart | Service not restarting after crash | "unless-stopped" restart policy for persistent services |

---

## Appendix: Configuration Files

### `.github/scripts/verify_release_sha.py`
- **Purpose**: Query GitHub API to verify release SHA
- **Entry**: `python .github/scripts/verify_release_sha.py <sha>`
- **Environment**: Requires `GITHUB_TOKEN` (GitHub Actions provides automatically)
- **Returns**: Exit code 0 (success) or 1 (failure), JSON output to stdout

### `docker-compose.production.yml`
- **Purpose**: Production Docker Compose configuration
- **Services**: postgres, redis, migrate, bootstrap-privileges, backend, celery-worker, celery-beat, frontend
- **Overrides**: Development docker-compose.yml with hardening (localhost ports, persistent storage, logging, restart policies)

### `scripts/deploy-docker-compose.sh`
- **Purpose**: Linux deployment script
- **Entry**: `bash scripts/deploy-docker-compose.sh <sha> [environment]`
- **Special modes**: `bash scripts/deploy-docker-compose.sh --rollback`
- **Environment**: Requires POSTGRES_PASSWORD, DCIM_APP_PASSWORD, JWT_SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY

### `.github/workflows/deploy.yml`
- **Purpose**: GitHub Actions deployment workflow
- **Trigger**: Manual dispatch (workflow_dispatch)
- **Inputs**: release_sha (required), environment (required: staging or production)
- **Jobs**: verify-release, request-approval (production only), validate-environment, staging-deployment, record-deployment, deployment-summary

---

## References

- Docker Compose: https://docs.docker.com/compose/
- GitHub Actions: https://docs.github.com/en/actions
- GitHub API (Check Runs): https://docs.github.com/en/rest/checks
- PostgreSQL Docker: https://hub.docker.com/_/postgres
- Fernet (cryptography): https://cryptography.io/en/latest/fernet/

---

**End of Document**

This implementation is **not yet deployed**. It requires:
1. Owner review and approval
2. Production server setup (directories, user, Docker, firewall)
3. GitHub environment approval gate configuration (Settings → Environments → production-deployment)
4. Secrets provisioning (POSTGRES_PASSWORD, etc.)
5. Manual trigger to begin actual deployments

**Do NOT** provision production resources or execute production deployment without explicit authorization.
