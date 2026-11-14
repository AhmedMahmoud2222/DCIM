#!/usr/bin/env bash
# Throughput benchmark of the demo loader CLI in three modes on identical copies of one seeded world:
#   1. one process, one collector at a time        (--sequential)
#   2. one process, one session per collector      (default)
#   3. three processes, one per site               (--site-prefix, run concurrently)
# DISPOSABLE PostgreSQL ONLY: it creates and drops databases named dcim_bench and bench_*. Never point it at real data.
#
# Needs: a PostgreSQL 16 server you control, a superuser (PGUSER/PGPASSWORD/PGHOST), the dcim_app role, and a Python venv with
# the backend installed. Run from backend/:   DCIM_APP_PASSWORD=... PGUSER=postgres PGPASSWORD=... scripts/demo_loader_benchmark/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DCIM_APP_PASSWORD:?set DCIM_APP_PASSWORD (the dcim_app role password)}"
: "${PGUSER:?set PGUSER to a superuser}"; : "${PGPASSWORD:?set PGPASSWORD}"
PGHOST="${PGHOST:-localhost}"; PYTHON="${PYTHON:-python}"
export PYTHONPATH="$PWD:$PWD/.."
export ENVIRONMENT=test REDIS_URL="${REDIS_URL:-redis://localhost:6379/1}"
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export CREDENTIAL_ENCRYPTION_KEY="${CREDENTIAL_ENCRYPTION_KEY:-$($PYTHON -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')}"
url() { echo "postgresql+asyncpg://dcim_app:${DCIM_APP_PASSWORD}@${PGHOST}:5432/$1"; }
sql() { psql -h "$PGHOST" -qAt -d postgres -c "$1"; }

for db in bench_seq bench_par bench_proc dcim_bench; do sql "DROP DATABASE IF EXISTS $db"; done
sql "CREATE DATABASE dcim_bench"
psql -h "$PGHOST" -q -d dcim_bench -c 'GRANT CREATE, USAGE ON SCHEMA public TO dcim_app; CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS btree_gist;'
export DATABASE_URL="$(url dcim_bench)"
alembic upgrade head >/dev/null
psql -h "$PGHOST" -q -d dcim_bench -f scripts/bootstrap_privileged_roles.sql >/dev/null
$PYTHON scripts/demo_loader_benchmark/build_world.py
for c in seq par proc; do sql "CREATE DATABASE bench_$c TEMPLATE dcim_bench"; done

ASOF="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
COMMON=(--confirm-local-demo --steps existing,sensors,devices --device-history-hours 6 --no-verify --as-of "$ASOF")
run() { local db=$1; shift; DATABASE_URL="$(url "$db")" $PYTHON -m app.tools.demo_loader apply "${COMMON[@]}" "$@"; }
timeit() { local s e; s=$(date +%s.%N); "$@" >"$OUT/$LABEL.json" 2>/dev/null; e=$(date +%s.%N); printf '%-45s %.1f s\n' "$LABEL" "$(echo "$e - $s" | bc)"; }
OUT="$(mktemp -d)"
LABEL="one process, sequential";               timeit run bench_seq --sequential
LABEL="one process, session per collector";    timeit run bench_par
s=$(date +%s.%N); pids=()
for n in 01 02 03; do run bench_proc --sequential --site-prefix "DEMO-SITE-$n" >"$OUT/proc_$n.json" 2>/dev/null & pids+=($!); done
rc=0; for p in "${pids[@]}"; do wait "$p" || rc=$?; done
printf '%-45s %.1f s (exit %s)\n' "three processes, one per site" "$(echo "$(date +%s.%N) - $s" | bc)" "$rc"
echo "per-run JSON reports: $OUT"
for db in bench_seq bench_par bench_proc dcim_bench; do sql "DROP DATABASE IF EXISTS $db"; done
