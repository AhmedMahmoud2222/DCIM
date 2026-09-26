#!/usr/bin/env bash
# Validation-only staging deployment runner. Production execution is disabled.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="${DEPLOY_DIR:-$REPO_ROOT}"
BACKUP_DIR="${BACKUP_DIR:-$DEPLOY_DIR/.deployment-backups}"
LOG_DIR="${LOG_DIR:-$DEPLOY_DIR/.deployment-logs}"
LOCK_FILE="$DEPLOY_DIR/.deployment.lock"
STATE_FILE="$DEPLOY_DIR/.deployment-sha"
PREVIOUS_FILE="$DEPLOY_DIR/.previous-deployment-sha"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.production.yml)
HEALTH_CHECK_TIMEOUT="${HEALTH_CHECK_TIMEOUT:-90}"
DEPLOY_ACTIVE=0
ROLLBACK_ACTIVE=0
BACKUP_ENV=""
PRIOR_SHA=""
RELEASE_SHA="${1:-}"
TARGET="${2:-staging}"

fail() { echo "Deployment failure: $*" >&2; return 1; }
acquire_lock() {
    command -v flock >/dev/null || { fail 'flock is required'; return 1; }
    mkdir -p "$DEPLOY_DIR" "$BACKUP_DIR" "$LOG_DIR"
    # Never unlink the lock file: a second inode would permit simultaneous holders.
    exec 200>"$LOCK_FILE"
    flock -w "${LOCK_TIMEOUT:-300}" 200 || { fail 'deployment lock unavailable'; return 1; }
}

validate() {
    [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { fail 'full canonical SHA required'; return 1; }
    [[ "$TARGET" == staging ]] || { fail 'production deployment is disabled'; return 1; }
    [[ "$(cd "$DEPLOY_DIR" && pwd -P)" == "$(cd "$REPO_ROOT" && pwd -P)" ]] || { fail 'DEPLOY_DIR must equal script repository root'; return 1; }
    [[ "$(git -C "$REPO_ROOT" rev-parse --verify "$RELEASE_SHA^{commit}")" == "$RELEASE_SHA" ]] || return 1
    git -C "$REPO_ROOT" fetch --quiet origin main
    git -C "$REPO_ROOT" merge-base --is-ancestor "$RELEASE_SHA" origin/main || { fail 'SHA is not on origin/main'; return 1; }
    GITHUB_TOKEN="${GITHUB_TOKEN:-}" python3 "$REPO_ROOT/.github/scripts/verify_release_sha.py" "$RELEASE_SHA" >/dev/null || { fail 'GitHub release evidence not verified'; return 1; }
    if ! docker info >/dev/null || ! docker compose version >/dev/null; then
        fail 'Docker unavailable'; return 1
    fi
    for name in POSTGRES_PASSWORD DCIM_APP_PASSWORD JWT_SECRET_KEY CREDENTIAL_ENCRYPTION_KEY; do
        [[ -n "${!name:-}" ]] || { fail "missing $name"; return 1; }
    done
    # Do not put secret values in logs, workflow artifacts, or command arguments.
    [[ ${#CREDENTIAL_ENCRYPTION_KEY} == 44 ]] || { fail 'invalid Fernet key length'; return 1; }
    cd "$DEPLOY_DIR"
    "${COMPOSE[@]}" config --quiet >/dev/null || { fail 'effective Compose configuration invalid'; return 1; }
}

service_state() {
    local id
    id="$("${COMPOSE[@]}" ps -a -q "$1")" || return 1
    [[ -n "$id" ]] || return 1
    docker inspect --format "{{.State.$2}}" "$id"
}

healthy() {
    local deadline=$((SECONDS + HEALTH_CHECK_TIMEOUT)) service status
    while (( SECONDS < deadline )); do
        if curl -fsS --max-time 2 http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
            for service in postgres redis backend; do
                status="$(service_state "$service" Health.Status 2>/dev/null)" || break
                [[ "$status" == healthy ]] || break
            done
            [[ "$service" == backend && "$status" == healthy ]] || { sleep 2; continue; }
            for service in celery-worker celery-beat frontend; do
                status="$(service_state "$service" Status 2>/dev/null)" || break
                [[ "$status" == running ]] || break
            done
            [[ "$service" == frontend && "$status" == running ]] || { sleep 2; continue; }
            for service in migrate bootstrap-privileges; do
                [[ "$(service_state "$service" Status 2>/dev/null)" == exited ]] || return 1
                [[ "$(service_state "$service" ExitCode 2>/dev/null)" == 0 ]] || return 1
            done
            return 0
        fi
        sleep 2
    done
    fail 'service health deadline exceeded'
}

start_stack() {
    "${COMPOSE[@]}" up -d --build || return 1
    healthy
}

rollback() {
    ROLLBACK_ACTIVE=1
    [[ "$PRIOR_SHA" =~ ^[0-9a-f]{40}$ ]] || { fail 'no prior successful release for rollback'; return 1; }
    [[ -n "$BACKUP_ENV" && -f "$BACKUP_ENV" ]] || { fail 'previous configuration backup missing'; return 1; }
    [[ "$(git -C "$REPO_ROOT" rev-parse --verify "$PRIOR_SHA^{commit}")" == "$PRIOR_SHA" ]] || return 1
    # Never use down -v: persistent PostgreSQL storage must remain intact.
    "${COMPOSE[@]}" down --remove-orphans || return 1
    git -C "$REPO_ROOT" checkout --quiet --detach "$PRIOR_SHA" || return 1
    cp "$BACKUP_ENV" "$DEPLOY_DIR/.env" || return 1
    chmod 600 "$DEPLOY_DIR/.env"
    cd "$DEPLOY_DIR"
    "${COMPOSE[@]}" config --quiet >/dev/null || return 1
    start_stack || return 1
    printf '%s\n' "$PRIOR_SHA" > "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
    echo "Rollback recovered $PRIOR_SHA" >&2
}

on_exit() {
    local result=$?
    trap - EXIT
    if (( result != 0 && DEPLOY_ACTIVE && ! ROLLBACK_ACTIVE )); then
        echo 'Deployment failed; attempting controlled rollback' >&2
        if [[ -n "$PRIOR_SHA" ]]; then
            rollback || echo 'ROLLBACK FAILED: manual intervention required' >&2
        else
            "${COMPOSE[@]}" down --remove-orphans || echo 'FAILED to stop unsuccessful first deployment' >&2
        fi
    fi
    exit "$result"
}

main() {
    acquire_lock
    trap on_exit EXIT
    validate
    PRIOR_SHA="$(cat "$STATE_FILE" 2>/dev/null || :)"
    if [[ -n "$PRIOR_SHA" ]]; then
        [[ "$PRIOR_SHA" =~ ^[0-9a-f]{40}$ ]] || { fail 'corrupt deployment state'; return 1; }
        [[ -f "$DEPLOY_DIR/.env" ]] || { fail 'previous configuration missing'; return 1; }
        BACKUP_ENV="$(mktemp "$BACKUP_DIR/env.XXXXXXXX")"
        chmod 600 "$BACKUP_ENV"
        cp "$DEPLOY_DIR/.env" "$BACKUP_ENV"
    fi
    DEPLOY_ACTIVE=1
    git -C "$REPO_ROOT" checkout --quiet --detach "$RELEASE_SHA"
    cd "$DEPLOY_DIR"
    "${COMPOSE[@]}" config --quiet >/dev/null
    start_stack
    # Only a healthy release becomes current. Keep previous successful SHA distinct.
    if [[ -n "$PRIOR_SHA" ]]; then
        printf '%s\n' "$PRIOR_SHA" > "$PREVIOUS_FILE"
    fi
    printf '%s\n' "$RELEASE_SHA" > "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
    DEPLOY_ACTIVE=0
    echo "Validated staging release $RELEASE_SHA is running"
}

main "$@"
