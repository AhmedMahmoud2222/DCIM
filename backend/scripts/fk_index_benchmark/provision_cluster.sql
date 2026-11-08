-- Run ONCE, as the bootstrap superuser, on a DISPOSABLE PostgreSQL 16 cluster that was started with
--     -c cluster_name=fkbench-disposable-<anything> -c shared_preload_libraries=pg_stat_statements
-- The cluster_name is the marker the benchmark requires: it can only be set in server start-up configuration, never by SQL,
-- so an operator has to put it there on purpose. The benchmark itself then runs WITHOUT superuser, as fkbench_admin.
--
--   psql -U postgres -v admin_password="'change-me'" -f provision_cluster.sql
CREATE ROLE fkbench_admin LOGIN CREATEDB PASSWORD :admin_password;   -- CREATEDB only: not a superuser, not CREATEROLE
GRANT pg_monitor, pg_signal_backend, pg_checkpoint TO fkbench_admin;  -- sampling views, cancel/terminate, CHECKPOINT
GRANT dcim_app TO fkbench_admin;                                      -- load/VACUUM tables that migrations make dcim_app-owned
GRANT SET ON PARAMETER session_replication_role TO fkbench_admin;     -- raw-SQL mode only (FK triggers off); the FK-enforced mode never uses it
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;                    -- exact per-statement (per-index) build times
GRANT EXECUTE ON FUNCTION pg_stat_statements_reset(oid, oid, bigint) TO fkbench_admin;
