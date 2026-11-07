# Migration 0057 read-only preflight evidence

Classification: **NOT YET VALIDATED**. This kit collects environment facts. It does not
approve a migration, change the production gate, or close issue #214. The owner and a
DBA must approve in writing after reviewing the complete decision package.

## Provision the collector role

A DBA provisions a dedicated login in the target database, outside the collector.
Set its password through the organization's secret-management process, not in a
committed script. Replace `dcim` with the target database and `public` with the
application schema if needed. Do not grant table ownership, application table
SELECT, write privileges, CREATE, role membership with write access, or superuser.
Catalog views/functions used by this kit retain their standard catalog read grants.
`pg_monitor` supplies monitoring visibility; it is not a write grant.

```sql
CREATE ROLE dcim_preflight_0057 LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE dcim TO dcim_preflight_0057;
GRANT USAGE ON SCHEMA public TO dcim_preflight_0057;
GRANT pg_monitor TO dcim_preflight_0057;
GRANT SELECT ON TABLE public.alembic_version TO dcim_preflight_0057;
ALTER ROLE dcim_preflight_0057 SET default_transaction_read_only = on;
```

Audit inherited and PUBLIC privileges under local policy. If catalog function
access is restricted locally, retain the unavailable result and ask the DBA to
supply the missing evidence rather than granting broad application data access.
System identifier collection can be unavailable when `pg_control_system()` is not
executable; the collector never exports its raw value.

## Run

Use the backend's existing Python environment and dependencies. Supply a libpq DSN
only via `DCIM_PREFLIGHT_0057_DSN`, injected by the secret manager or entered silently.
Do not put it on the command line or enable shell tracing.

```sh
cd backend
read -r -s -p 'Preflight DSN: ' DCIM_PREFLIGHT_0057_DSN
export DCIM_PREFLIGHT_0057_DSN
python -m scripts.preflight_0057 --environment-label staging --output-dir /tmp/0057-evidence
unset DCIM_PREFLIGHT_0057_DSN
```

Dev, staging and production are supported with the dedicated non-superuser role.
Unlike the destructive benchmark harness, this collector never creates or seeds
objects, runs migrations, or requires a disposable target. Run the benchmark only
on its authorized non-production scratch cluster, following its separate runbook.

The label is free text; omit connection details and secrets. Filenames replace
unsafe label characters with underscores. Reports contain a UTC timestamp, tool
version, the original label and a SHA-256 hash of the PostgreSQL system identifier
when readable. Neither output includes connection identifiers. A privacy self-check
rejects serialized output containing the DSN, password, raw system identifier, or a host,
database or user name outside the label you chose.
Use a protected output directory and review artifacts before sharing.

Every transaction starts with `BEGIN READ ONLY`, then
`SET default_transaction_read_only = on`. The tool verifies
`SHOW transaction_read_only` and refuses superusers before catalog collection.
Only fixed setup/rollback statements and reviewed SELECT/SHOW queries are allowed.
No transaction is committed. Failed reads are marked unavailable with a generic
reason, then a fresh read-only transaction starts on the same connection. There is
only one database connection. The tool makes no other network calls.

Exit codes: `0` collected (individual fields may be unavailable), `2` refused
(superuser, unsafe session or privacy check), `3` connection, collection or output
failure. Missing privileges are evidence gaps, not permission to proceed. Table
sizes are bytes; `reltuples` is an estimate, never a row count; `-1` means the table has never been analyzed. Replica lag rows
are ordered as write, flush and replay lag; a null lag is not proof of zero lag.
Archive times are last archived and last failed times. Catalog facts reflect the
collection interval and can change immediately afterward. Index existence and
validity do not prove the full index definition matches migration 0057.

## Attach to issue #214

Attach both timestamped JSON and Markdown artifacts. Fill the `operator_supplied`
fields for free disk, backup/PITR status, ingest peak and retention churn with
measurement time, source and responsible operator. Keep original machine artifacts
and attach these additions separately. Include representative staging benchmark
artifacts, monitoring evidence and the completed decision package below. Resolve
all unavailable fields explicitly. Approval must be written by both owner and DBA;
collection success alone does not authorize production execution.

## Decision-package template

All thresholds below are intentionally blank. The owner and DBA set values, units,
measurement windows and stop actions before approving a run.

| Field | Value / evidence reference |
| --- | --- |
| Environment label and collection timestamp | |
| System identifier hash | |
| Current revision and 0057 state | |
| Unavailable evidence and resolution | |
| Representative staging benchmark | |
| Free disk evidence | |
| Backup/PITR evidence and restore verification | |
| Ingest peak evidence | |
| Retention churn evidence | |
| Latency p95 threshold, units and window | |
| Latency p99 threshold, units and window | |
| Write failures threshold and window | |
| Lock waits threshold and window | |
| Disk headroom threshold and units | |
| WAL headroom threshold and units | |
| Replication lag threshold and units | |
| Maximum run duration and units | |
| Monitoring responsibility and stop actions | |
| Rollback/recovery plan | |
| Change reference and approved window | |
| Owner written approval, identity and date | |
| DBA written approval, identity and date | |
| Go/no-go decision and rationale | |

Follow [the rollout runbook](FK_INDEX_ROLLOUT_0057.md) for the production gate and
approved execution process. Issue #214 remains open until owner and DBA written approval.
