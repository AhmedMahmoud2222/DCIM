#!/bin/bash

# DCIM Docker Compose Production Deployment Script
#
# This script deploys the DCIM application using Docker Compose to a production server.
#
# SECURITY NOTES:
# - Requires DCIM_DEPLOY_USER identity with restricted sudo privileges
# - All environment variables must be provided by deployment workflow (no default production secrets)
# - Previous deployment state is backed up for rollback
# - Deployment lock prevents concurrent deployments
# - Health checks validate deployment before completion

set -euo pipefail

# ============================================================================
# Configuration and defaults
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Deployment paths (must be set by caller)
DEPLOY_DIR="${DEPLOY_DIR:-/opt/dcim}"
DEPLOYMENT_LOCK_FILE="${DEPLOY_DIR}/.deployment.lock"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dcim}"
LOG_DIR="${LOG_DIR:-/var/log/dcim}"

# Lock tracking
DEPLOYMENT_LOCK_FD=""
DEPLOYMENT_LOCK_TYPE=""  # "flock" or "fallback"

# Release information
RELEASE_SHA="${1:-}"
RELEASE_ENVIRONMENT="${2:-production}"

# Health check configuration
HEALTH_CHECK_TIMEOUT=60
HEALTH_CHECK_INTERVAL=3
BACKEND_HEALTH_URL="http://127.0.0.1:8000/api/v1/health/ready"

# ============================================================================
# Utility functions
# ============================================================================

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "${LOG_DIR}/deployment.log"
}

log_error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S UTC')] ERROR: $*" | tee -a "${LOG_DIR}/deployment.log" >&2
}

log_section() {
    echo "" | tee -a "${LOG_DIR}/deployment.log"
    log "=== $* ==="
}

die() {
    log_error "$*"
    exit 1
}

acquire_deployment_lock() {
    local lock_dir="$(dirname "$DEPLOYMENT_LOCK_FILE")"
    mkdir -p "$lock_dir"

    # Use flock for atomic lock acquisition (requires flock available)
    if ! command -v flock &> /dev/null; then
        log_error "flock command not found; using fallback file lock (not atomic)"
        # Fallback: wait for manual lock file removal
        local timeout=300
        local start_time=$(date +%s)
        while [ -f "$DEPLOYMENT_LOCK_FILE" ]; do
            local elapsed=$(($(date +%s) - start_time))
            if [ $elapsed -ge $timeout ]; then
                die "Deployment lock held for more than ${timeout}s (remove manually: rm $DEPLOYMENT_LOCK_FILE)"
            fi
            log "Waiting for previous deployment... ($elapsed/${timeout}s)"
            sleep 5
        done
        echo "$$" > "$DEPLOYMENT_LOCK_FILE"
        DEPLOYMENT_LOCK_TYPE="fallback"
        log "Acquired fallback file lock (PID: $$)"
        return
    fi

    # Atomic lock using flock (300s timeout)
    exec 200>"$DEPLOYMENT_LOCK_FILE"
    DEPLOYMENT_LOCK_FD=200
    if ! flock -n 200; then
        log "Deployment lock in use; waiting (timeout: 300s)..."
        if ! timeout 300 flock 200; then
            die "Deployment lock timeout; previous deployment may have failed"
        fi
    fi

    DEPLOYMENT_LOCK_TYPE="flock"
    log "Acquired atomic deployment lock (PID: $$, FD: $DEPLOYMENT_LOCK_FD)"
}

release_deployment_lock() {
    # Only release if we actually acquired a lock
    if [ -z "$DEPLOYMENT_LOCK_TYPE" ]; then
        return  # Lock was never acquired
    fi

    case "$DEPLOYMENT_LOCK_TYPE" in
        flock)
            if [ -n "$DEPLOYMENT_LOCK_FD" ]; then
                eval "exec $DEPLOYMENT_LOCK_FD>&-" 2>/dev/null || true  # Close the file descriptor
                log "Released flock deployment lock"
            fi
            ;;
        fallback)
            # Only remove fallback lock file if we created it
            if [ -f "$DEPLOYMENT_LOCK_FILE" ] && grep -q "^$$\$" "$DEPLOYMENT_LOCK_FILE" 2>/dev/null; then
                rm -f "$DEPLOYMENT_LOCK_FILE"
                log "Released fallback file lock"
            else
                log "Fallback lock file not owned by this process (PID: $$), not removing"
            fi
            ;;
    esac
}

backup_current_deployment() {
    log_section "Backing up current deployment state"

    mkdir -p "$BACKUP_DIR"

    if [ ! -d "$DEPLOY_DIR" ]; then
        log "No existing deployment to backup"
        return 0
    fi

    local backup_timestamp=$(date +%Y%m%d-%H%M%S)
    local backup_path="$BACKUP_DIR/deployment-${backup_timestamp}"

    # Backup environment file
    if [ -f "$DEPLOY_DIR/.env" ]; then
        mkdir -p "$backup_path"
        cp "$DEPLOY_DIR/.env" "$backup_path/.env.backup"
        chmod 600 "$backup_path/.env.backup"
        log "Backed up environment to $backup_path/.env.backup"
    fi

    # Save current deployment info
    if [ -d "$DEPLOY_DIR" ]; then
        echo "Timestamp: $backup_timestamp" > "$backup_path/deployment-info.txt"
        if [ -f "$DEPLOY_DIR/.deployment-sha" ]; then
            cat "$DEPLOY_DIR/.deployment-sha" >> "$backup_path/deployment-info.txt"
        fi
        log "Backup prepared at $backup_path"
    fi

    echo "$backup_path"
}

# ============================================================================
# Validation functions
# ============================================================================

validate_arguments() {
    if [ -z "$RELEASE_SHA" ]; then
        die "Usage: $0 <release-sha> [environment]"
    fi

    # Validate SHA format (at least 12 hex characters)
    if ! [[ "$RELEASE_SHA" =~ ^[0-9a-f]{12,}$ ]]; then
        die "Invalid SHA format: $RELEASE_SHA (must be at least 12 hex characters)"
    fi

    log "Deploying release: $RELEASE_SHA"
    log "Environment: $RELEASE_ENVIRONMENT"
}

validate_permissions() {
    log_section "Validating deployment permissions"

    # Check that deploy directory exists and is writable
    if [ ! -d "$DEPLOY_DIR" ]; then
        die "Deploy directory does not exist: $DEPLOY_DIR"
    fi

    if [ ! -w "$DEPLOY_DIR" ]; then
        die "Deploy directory is not writable: $DEPLOY_DIR"
    fi

    # Check that backup directory is writable
    mkdir -p "$BACKUP_DIR"
    if [ ! -w "$BACKUP_DIR" ]; then
        die "Backup directory is not writable: $BACKUP_DIR"
    fi

    # Check that log directory is writable
    mkdir -p "$LOG_DIR"
    if [ ! -w "$LOG_DIR" ]; then
        die "Log directory is not writable: $LOG_DIR"
    fi

    log "Deployment permissions validated"
}

validate_docker() {
    log_section "Validating Docker environment"

    # Check Docker daemon
    if ! docker info > /dev/null 2>&1; then
        die "Docker daemon is not running or not accessible"
    fi

    # Check Docker Compose
    if ! docker compose version > /dev/null 2>&1; then
        die "Docker Compose is not installed or not accessible"
    fi

    log "Docker and Docker Compose validated"
}

validate_environment_variables() {
    log_section "Validating required environment variables"

    local required_vars=(
        "POSTGRES_PASSWORD"
        "DCIM_APP_PASSWORD"
        "JWT_SECRET_KEY"
        "CREDENTIAL_ENCRYPTION_KEY"
    )

    for var in "${required_vars[@]}"; do
        if [ -z "${!var:-}" ]; then
            die "Required environment variable not set: $var"
        fi
    done

    # Validate Fernet key format (base64 encoded 32 bytes = 44 characters)
    local fernet_key="${CREDENTIAL_ENCRYPTION_KEY}"
    if [ ${#fernet_key} -lt 44 ]; then
        die "CREDENTIAL_ENCRYPTION_KEY appears invalid (must be 44+ characters for Fernet)"
    fi

    log "Environment variables validated"
}

# ============================================================================
# Deployment functions
# ============================================================================

checkout_release() {
    log_section "Checking out release $RELEASE_SHA"

    cd "$REPO_ROOT"

    # Verify SHA exists in local repository
    if ! git rev-parse --verify "$RELEASE_SHA^{commit}" > /dev/null 2>&1; then
        die "Commit $RELEASE_SHA not found in repository"
    fi

    # Verify SHA is on main branch
    if ! git merge-base --is-ancestor "$RELEASE_SHA" origin/main 2>/dev/null; then
        die "Commit $RELEASE_SHA is not an ancestor of main branch"
    fi

    # Checkout the release
    git fetch --quiet origin "$RELEASE_SHA"
    git checkout --quiet "$RELEASE_SHA"

    log "Checked out release $RELEASE_SHA"
}

prepare_environment_file() {
    log_section "Preparing environment file"

    cd "$DEPLOY_DIR"

    # Create .env file with provided variables
    cat > .env <<EOF
# Generated by deployment script on $(date -Iseconds)
# Release: $RELEASE_SHA
# Environment: $RELEASE_ENVIRONMENT

POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_DB=${POSTGRES_DB:-dcim}
DCIM_APP_PASSWORD=$DCIM_APP_PASSWORD
JWT_SECRET_KEY=$JWT_SECRET_KEY
CREDENTIAL_ENCRYPTION_KEY=$CREDENTIAL_ENCRYPTION_KEY
POSTGRES_DATA_PATH=${POSTGRES_DATA_PATH:-/var/lib/dcim/postgres}
CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS:-["https://dcim.example.com"]}
ENVIRONMENT=$RELEASE_ENVIRONMENT

# Backup: Previous deployment SHA
# $(<.deployment-sha 2>/dev/null || echo "None")
EOF

    # Restrict permissions on environment file
    chmod 600 .env

    log "Environment file prepared at $DEPLOY_DIR/.env"
}

pull_images() {
    log_section "Pulling Docker images"

    cd "$DEPLOY_DIR"

    # Pull latest images from registry (if needed)
    docker compose -f docker-compose.production.yml pull --quiet

    log "Docker images pulled"
}

build_images() {
    log_section "Building Docker images for release $RELEASE_SHA"

    cd "$DEPLOY_DIR"

    # Build images with release SHA tag
    docker compose -f docker-compose.production.yml build --quiet --no-cache

    log "Docker images built"
}

start_services() {
    log_section "Starting services"

    cd "$DEPLOY_DIR"

    # Start services in order (migrations, bootstrap, then main services)
    docker compose -f docker-compose.production.yml up -d postgres redis
    docker compose -f docker-compose.production.yml up -d migrate
    docker compose -f docker-compose.production.yml up -d bootstrap-privileges
    docker compose -f docker-compose.production.yml up -d

    log "Services started"
}

wait_for_health() {
    log_section "Waiting for services to be healthy"

    local elapsed=0
    local deadline=$((SECONDS + HEALTH_CHECK_TIMEOUT))

    while [ $SECONDS -lt $deadline ]; do
        # Check backend readiness
        if curl -sf "$BACKEND_HEALTH_URL" > /dev/null 2>&1; then
            log "Backend is ready"

            # Verify expected long-running services are running
            cd "$DEPLOY_DIR"
            local expected_services=("postgres" "redis" "backend" "celery-worker" "celery-beat" "frontend")
            local all_running=true

            for service in "${expected_services[@]}"; do
                local state=$(docker compose -f docker-compose.production.yml ps --filter "service=$service" --format "{{.State}}" 2>/dev/null)
                if [ "$state" != "running" ]; then
                    log "Service $service is not running (state: $state)"
                    all_running=false
                    break
                fi
            done

            if [ "$all_running" = true ]; then
                log "All long-running services healthy and running"
                return 0
            fi
        fi

        elapsed=$((SECONDS - (deadline - HEALTH_CHECK_TIMEOUT)))
        log "Waiting for services... (${elapsed}/${HEALTH_CHECK_TIMEOUT}s)"
        sleep $HEALTH_CHECK_INTERVAL
    done

    log_error "Services failed to become healthy within ${HEALTH_CHECK_TIMEOUT}s"
    return 1
}

record_deployment() {
    log_section "Recording deployment"

    cd "$DEPLOY_DIR"

    cat > .deployment-info <<EOF
Deployment Time: $(date -Iseconds)
Release SHA: $RELEASE_SHA
Environment: $RELEASE_ENVIRONMENT
Deployed By: $DEPLOY_USER
EOF

    echo "$RELEASE_SHA" > .deployment-sha

    # Record to deployment log
    {
        echo "$(date -Iseconds) | Deployed $RELEASE_SHA to $RELEASE_ENVIRONMENT"
        echo "  Deployed by: ${DEPLOY_USER:-unknown}"
        echo "  Health check passed"
    } >> "$LOG_DIR/deployments.log"

    log "Deployment recorded: $RELEASE_SHA"
}

# ============================================================================
# Rollback function
# ============================================================================

rollback_deployment() {
    log_section "Rolling back to previous deployment"

    cd "$DEPLOY_DIR"

    if [ ! -f .deployment-sha ]; then
        die "No previous deployment recorded; cannot rollback"
    fi

    local previous_sha=$(cat .deployment-sha)
    if [ -z "$previous_sha" ]; then
        die "Previous deployment SHA is empty; cannot rollback safely"
    fi

    log "Rolling back to previous deployment: $previous_sha"

    # Validate previous SHA exists in repository
    if ! git rev-parse --verify "${previous_sha}^{commit}" > /dev/null 2>&1; then
        die "Previous deployment SHA ${previous_sha} not found in repository; cannot rollback"
    fi

    log "Stopping current services..."
    docker compose -f docker-compose.production.yml down --remove-orphans 2>/dev/null || true

    log "Checking out previous code: $previous_sha"
    git fetch --quiet origin "$previous_sha" || die "Could not fetch previous SHA from origin"
    git checkout --quiet "$previous_sha" || die "Could not checkout previous SHA"

    # Restore previous .env from backup if available
    if [ -f "$BACKUP_DIR/deployment-"*"/.env.backup" ]; then
        log "Restoring previous environment configuration..."
        local latest_backup=$(ls -d "$BACKUP_DIR"/deployment-* 2>/dev/null | sort -r | head -1)
        if [ -n "$latest_backup" ] && [ -f "$latest_backup/.env.backup" ]; then
            cp "$latest_backup/.env.backup" .env
            chmod 600 .env
            log "Restored .env from backup"
        fi
    fi

    log "Starting services from previous deployment..."
    docker compose -f docker-compose.production.yml up -d postgres redis 2>/dev/null || true
    sleep 5
    docker compose -f docker-compose.production.yml up -d migrate 2>/dev/null || true
    docker compose -f docker-compose.production.yml up -d bootstrap-privileges 2>/dev/null || true
    docker compose -f docker-compose.production.yml up -d 2>/dev/null || true

    log "Verifying rollback..."
    local verify_timeout=$((SECONDS + HEALTH_CHECK_TIMEOUT))
    local verified=false

    while [ $SECONDS -lt $verify_timeout ]; do
        if curl -sf "$BACKEND_HEALTH_URL" > /dev/null 2>&1; then
            log "✓ Backend health check passed"
            verified=true
            break
        fi
        sleep 3
    done

    if [ "$verified" = true ]; then
        log "✓ Rollback verified: services healthy"
        log "Deployment rolled back to $previous_sha"

        # Record rollback in log
        {
            echo "$(date -Iseconds) | Rolled back to $previous_sha"
            echo "  Initiated by: ${DEPLOY_USER:-unknown}"
            echo "  Rollback verified: health checks passed"
        } >> "$LOG_DIR/deployments.log"
    else
        log_error "Rollback completed but health checks failed; manual verification required"
        log_error "Services may not be healthy; check: docker compose -f docker-compose.production.yml logs"
        exit 1
    fi
}

# ============================================================================
# Main execution
# ============================================================================

main() {
    # Trap cleanup
    trap release_deployment_lock EXIT INT TERM

    log_section "DCIM Deployment Script Started"

    # Validation phase
    validate_arguments
    validate_permissions
    validate_docker
    validate_environment_variables

    # Lock deployment
    acquire_deployment_lock

    # Backup phase
    local backup_path=$(backup_current_deployment)

    # Deployment phase
    checkout_release
    prepare_environment_file
    pull_images
    build_images
    start_services

    # Health check phase
    if ! wait_for_health; then
        log_error "Deployment health check failed"
        rollback_deployment
        die "Deployment aborted and rolled back"
    fi

    # Record phase
    record_deployment

    log_section "DCIM Deployment Completed Successfully"
    log "Release $RELEASE_SHA deployed to $RELEASE_ENVIRONMENT"
    log "Backup available at: $backup_path"
    log ""
    log "Next steps:"
    log "  1. Verify application availability at https://dcim.example.com"
    log "  2. Check logs: docker compose -f docker-compose.production.yml logs -f"
    log "  3. To rollback if needed: $0 --rollback"
}

# Handle special modes
if [ "${1:-}" = "--rollback" ]; then
    trap release_deployment_lock EXIT INT TERM
    acquire_deployment_lock
    rollback_deployment
    exit 0
fi

# Run main deployment
main "$@"
