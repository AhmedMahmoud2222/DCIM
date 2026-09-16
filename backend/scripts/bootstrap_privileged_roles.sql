-- One-time, superuser-run bootstrap for the privileged AuditLog retention role
-- (ARCHITECTURE_REVIEW.md §30a — resolves H6). Run this once per environment, by a DBA
-- connected as a superuser (or a role with CREATEROLE) — it must NEVER be run by the
-- application's own migration role, which is deliberately not granted CREATEROLE
-- (least privilege, §33/§40 of the Phase 1 prompt). Idempotent: safe to re-run.
--
-- Usage: psql -U postgres -d dcim -f scripts/bootstrap_privileged_roles.sql
--        psql -U postgres -d dcim_test -f scripts/bootstrap_privileged_roles.sql
--
-- PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md Finding C1 (CRITICAL): revoking UPDATE/DELETE
-- from dcim_app on audit_log (done in migration 0002) is not sufficient on its own,
-- because dcim_app is the table's OWNER (it created audit_log while running Alembic),
-- and PostgreSQL treats DROP/ALTER as inherent to ownership rather than a grantable,
-- revocable privilege — REVOKE cannot take that away while dcim_app remains owner.
-- TRUNCATE, by contrast, *is* an ordinary grantable ACL privilege and empirical testing
-- confirmed `REVOKE TRUNCATE` does bind even the owner — but that alone still leaves
-- ALTER TABLE / DROP TABLE open. The correct fix is to move ownership of audit_log (and
-- every partition) to this dedicated, NOLOGIN retention role, and grant the application
-- role only the exact operations it legitimately needs (INSERT, SELECT, and — via a
-- narrowly scoped SECURITY DEFINER function below — the ability to trigger creation of
-- a new monthly partition, without ever holding ALTER/DROP/TRUNCATE itself).
--
-- A second, deeper layer of the same finding, found while validating this fix: even with
-- audit_log fully owned by dcim_retention_admin, dcim_app could STILL `DROP TABLE` its
-- partitions — because PostgreSQL grants the *database owner* an implicit right to drop
-- any table in that database, regardless of the table's own ownership or grants. If
-- dcim_app is the database owner (e.g. `CREATE DATABASE dcim OWNER dcim_app`, the pattern
-- this repository used before this correction), table-level ownership transfer alone
-- cannot close DROP TABLE. The block below defensively reassigns the database itself
-- away from dcim_app if it is currently the owner, so this holds regardless of how the
-- database was originally created; README.md/docker-compose.yml/ci.yml also no longer
-- create it that way in the first place.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dcim_retention_admin') THEN
        CREATE ROLE dcim_retention_admin NOINHERIT NOLOGIN;
    END IF;
END
$$;

DO $$
BEGIN
    IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) = 'dcim_app' THEN
        EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), current_user);
    END IF;
END
$$;

-- Needed so the SECURITY DEFINER function below (which runs as dcim_retention_admin) can
-- create new partition tables in the public schema. As of PostgreSQL 15, CREATE on the
-- public schema is no longer granted to PUBLIC by default, so this must be explicit.
GRANT CREATE, USAGE ON SCHEMA public TO dcim_retention_admin;
GRANT CREATE, USAGE ON SCHEMA public TO dcim_app;

-- --- Ownership transfer (Finding C1) -----------------------------------------------
-- Must run after the schema migration has created audit_log and its partitions, since
-- the objects must exist first. Re-running this block is safe: ALTER TABLE ... OWNER TO
-- is idempotent, and the pg_inherits loop picks up whatever partitions exist at the time
-- (including ones the audit_partition_maintenance Celery task creates later via the
-- SECURITY DEFINER function below, which itself creates them already owned by
-- dcim_retention_admin — see CREATE FUNCTION below).
DO $$
DECLARE
    part record;
BEGIN
    IF EXISTS (SELECT FROM pg_class WHERE relname = 'audit_log' AND relkind = 'p') THEN
        ALTER TABLE audit_log OWNER TO dcim_retention_admin;
        FOR part IN
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'audit_log'
        LOOP
            EXECUTE format('ALTER TABLE %I OWNER TO dcim_retention_admin', part.relname);
        END LOOP;
    END IF;
END
$$;

-- dcim_app (the application's own runtime/migration role) may only append and read audit
-- history — never rewrite, delete, truncate, or structurally alter it. Explicit REVOKE of
-- TRUNCATE/UPDATE/DELETE is defense-in-depth here (the ownership transfer above already
-- makes dcim_app a non-owner with no privileges unless granted); migration 0002 already
-- revokes UPDATE/DELETE, this adds TRUNCATE and re-grants exactly INSERT/SELECT under the
-- new ownership.
GRANT INSERT, SELECT ON audit_log TO dcim_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM dcim_app;
REVOKE ALL ON audit_log FROM PUBLIC;

-- --- Scoped partition-creation capability (Finding C1) ------------------------------
-- Creating a new partition (`CREATE TABLE ... PARTITION OF audit_log`) is, under the
-- hood, an ALTER of the parent table — which now only dcim_retention_admin can do. The
-- application still needs to trigger that (the audit_partition_maintenance Celery task,
-- running as dcim_app, keeps future months' partitions created ahead of time). Rather
-- than granting dcim_app any ALTER/ownership right, it is granted EXECUTE on this single,
-- narrowly-scoped SECURITY DEFINER function, which runs with dcim_retention_admin's
-- privileges for exactly this one operation and nothing else. `SET search_path` guards
-- against the standard SECURITY DEFINER search-path-hijack risk.
CREATE OR REPLACE FUNCTION dcim_create_audit_log_partition(
    partition_name text,
    range_start date,
    range_end date
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF partition_name !~ '^audit_log_[0-9]{4}_[0-9]{2}$' THEN
        RAISE EXCEPTION 'invalid audit_log partition name: %', partition_name;
    END IF;
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
        partition_name, range_start, range_end
    );
END;
$$;

ALTER FUNCTION dcim_create_audit_log_partition(text, date, date) OWNER TO dcim_retention_admin;
REVOKE ALL ON FUNCTION dcim_create_audit_log_partition(text, date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dcim_create_audit_log_partition(text, date, date) TO dcim_app;

-- NOTE on migration downgrade: once this script has run, `alembic downgrade` past
-- migration 0002 will fail on the steps that GRANT/DROP audit_log as dcim_app, because
-- dcim_app is no longer its owner. This mirrors the pre-existing, documented limitation
-- that dcim_retention_admin's own creation is likewise outside Alembic's downgrade path
-- (PHASE1_IMPLEMENTATION.md §14). To downgrade in an environment where this script has
-- run, a superuser must first run: ALTER TABLE audit_log OWNER TO dcim_app; (and each
-- partition), reversing the transfer above, before `alembic downgrade` is attempted.
