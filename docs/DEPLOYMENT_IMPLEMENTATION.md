# DCIM release validation and staging runner

**Status: validation only. Production execution is disabled.** This PR does not authorize a production deployment, provision infrastructure, or establish a protected deployment environment. The workflow dispatch validates a release and, for staging, checks the effective Compose configuration without starting containers. A separate operator invocation of the staging runner is not protected by a GitHub environment approval; access to that host and Docker socket must be controlled independently.

## Exact release evidence

`.github/scripts/verify_release_sha.py <40-character-lowercase-sha>` requires `GITHUB_TOKEN`. It verifies the canonical commit, exact ancestry from current `main`, and successful GitHub Actions **push to main** runs for both the seven-job CI and two-job deployment validation workflow. It binds each required job to the selected workflow attempt, its check-run ID and check-suite ID, and rejects wrong app, missing, duplicate, incomplete, skipped or failed evidence. It fails closed if any required API page is unavailable. A release on a PR branch cannot pass this check until merged into main and validated there.

`deploy.yml` is manually dispatched. The production input explicitly fails. The staging input performs the verifier check and `docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet` with disposable placeholder values; it never starts containers. Its final summary marks a pass only when both verification and staging configuration succeed. No passing production record is emitted.

## Staging runner

`scripts/deploy-docker-compose.sh <full-sha> staging` is for a controlled **disposable staging** environment only. It requires `flock`, the authorized SHA verifier, Docker Engine and Compose, and runtime secrets. It rejects production. The deployment checkout and `DEPLOY_DIR` must be the same repository root. Use a token with read access to repository contents, Actions and checks. Restrict host access and Docker socket access to trusted operators; this script does not confer GitHub approval enforcement.

With secrets supplied through the environment, run:

```bash
export GITHUB_TOKEN=... POSTGRES_PASSWORD=... DCIM_APP_PASSWORD=...
export JWT_SECRET_KEY=... CREDENTIAL_ENCRYPTION_KEY=...
export POSTGRES_DATA_PATH=/path/to/disposable/staging/postgres
bash scripts/deploy-docker-compose.sh <full-main-sha> staging
```

Use a valid URL-safe 32-byte Fernet key. Set `POSTGRES_DATA_PATH` to dedicated staging storage. The script combines both Compose files. The production override uses `!override` for ports and the PostgreSQL mount, requiring Docker Compose 2.24.4 or newer. Only loopback ports are published; the bridge network has outbound access and is **not** an `internal: true` network. No `down --volumes` command is issued. PostgreSQL data must be backed up independently before any migration; database migrations are not automatically reversed by application rollback.

The script takes an atomic `flock` on a stable file, validates release evidence, backs up the prior `.env`, checks out the candidate, starts Compose, and verifies database/Redis/backend health, worker/beat/frontend running states, and successful exit of the migration/bootstrap jobs. Only after success does it update `.deployment-sha` and `.previous-deployment-sha`. On failure it returns nonzero and attempts to restore the prior Git commit and `.env`, restart Compose and verify recovery. Failed recovery also returns nonzero and requires operator intervention. A first deployment with no prior successful version cannot roll back and stops the failed stack without removing volumes.

## Executable validation and limitations

`tests/deployment/test_verify_release.py` mocks GitHub API adversarial responses; `test_deploy_script.py` uses disposable Git repositories, two real competing processes, and mocked Docker and curl commands. The blocking `Deployment Tools Test` workflow runs these tests, ShellCheck, actionlint, security assertions and the combined Compose configuration assertions. The existing `Deployment validation` workflow additionally starts the **base** Compose stack on Docker; it does not prove the combined override starts or that a real rollback works. The staging script tests are mocks. Production host permissions, backup restoration under a real migration, actual production overrides under load, manual approval enforcement and screen of operational secrets remain unverified. Do not enable production deployment on the basis of this PR alone.
