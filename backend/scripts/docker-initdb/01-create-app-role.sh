#!/bin/bash
# Runs automatically, once, by the official postgres image's docker-entrypoint-initdb.d
# mechanism on first container start (only when the data directory is empty).
#
# Creates `dcim_app` as an ordinary, non-superuser, non-CREATEROLE login role — distinct
# from the Postgres bootstrap superuser (`postgres`, since POSTGRES_USER is intentionally
# left unset in docker-compose.yml). This mirrors README.md's non-Docker local setup
# exactly, including NOT making dcim_app the database owner (see below).
#
# Before this script existed, docker-compose.yml set POSTGRES_USER=dcim_app, which the
# official postgres image grants full superuser rights to — silently defeating the
# least-privilege audit_log protection in scripts/bootstrap_privileged_roles.sql, since a
# superuser bypasses every GRANT/REVOKE/ownership check. See
# PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md Finding C1 and PHASE1_CORRECTION_REPORT.md.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<-EOSQL
    CREATE ROLE dcim_app LOGIN PASSWORD '${DCIM_APP_PASSWORD}';
EOSQL

# dcim_app is deliberately NOT made the database owner (do not `ALTER DATABASE ... OWNER
# TO dcim_app`): PostgreSQL grants the database owner an implicit right to DROP TABLE any
# table in that database regardless of that table's own ownership or grants — empirically
# confirmed during the Finding C1 correction to bypass the audit_log ownership transfer
# below entirely. The database stays owned by the `postgres` superuser; dcim_app gets
# exactly CREATE, USAGE on the public schema, which is enough to run migrations and own
# the tables it creates, but not enough to bypass dcim_retention_admin's ownership of
# audit_log.
psql -v ON_ERROR_STOP=1 --username postgres --dbname "${POSTGRES_DB}" <<-EOSQL
    GRANT CREATE, USAGE ON SCHEMA public TO dcim_app;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    CREATE EXTENSION IF NOT EXISTS btree_gist;
EOSQL
