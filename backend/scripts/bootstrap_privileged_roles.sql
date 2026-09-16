-- One-time, superuser-run bootstrap for the privileged AuditLog retention role
-- (ARCHITECTURE_REVIEW.md §30a — resolves H6). Run this once per environment, by a DBA
-- connected as a superuser (or a role with CREATEROLE) — it must NEVER be run by the
-- application's own migration role, which is deliberately not granted CREATEROLE
-- (least privilege, §33/§40 of the Phase 1 prompt). Idempotent: safe to re-run.
--
-- Usage: psql -U postgres -d dcim -f scripts/bootstrap_privileged_roles.sql
--        psql -U postgres -d dcim_test -f scripts/bootstrap_privileged_roles.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dcim_retention_admin') THEN
        CREATE ROLE dcim_retention_admin NOINHERIT NOLOGIN;
    END IF;
END
$$;

-- Actual GRANT of table-level DDL rights (ALTER TABLE ... DETACH PARTITION, DROP TABLE
-- for the detached partition) is performed after the schema migration has created
-- audit_log, since the table must exist first. Run this a second time after
-- `alembic upgrade head` if audit_log did not exist yet on first run.
GRANT ALL PRIVILEGES ON audit_log TO dcim_retention_admin;
