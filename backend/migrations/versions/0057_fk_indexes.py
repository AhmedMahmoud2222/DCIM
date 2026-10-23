"""Index the referencing column of hot-path foreign keys that had none.

A foreign key does not create an index on the referencing side. Without one, every DELETE of a referenced row (and any
UPDATE that changes its referenced key) must scan the whole child table to apply `ON DELETE` / `ON UPDATE` actions, and
joins from the referenced row to its children cannot use an index. The largest exposure is `telemetry_reading`
(`mapping_id` is `ON DELETE RESTRICT`), but the cable / port-connection / notification / placement / power-topology FKs
below had the same gap.

Not indexed here: the `*_by_user_id` / `created_by` audit columns (23 FKs to `app_user` currently have no index). They
are consulted only when `DELETE /users/{id}` runs, an infrequent administrator action: each such delete does one
sequential scan of every referencing table to apply the `RESTRICT` / `SET NULL` / `CASCADE` action. The referencing tables
(catalog revisions, import jobs, floor plans, cables, notification config, ...) are low-volume relative to telemetry, so
that cost is small today. This is a judgment call, not a proof: if those tables grow or user deletion becomes routine,
index them in a follow-up. (`power_report_job.requested_by_user_id` is already indexed and is filtered on by a list endpoint.)

Existing objects are never trusted by name. Before any DDL, every target is resolved in the current schema and checked:

  * the target table must exist as an ordinary (non-partitioned) table with the column;
  * if a relation with the index name already exists it must be an index on THAT table, btree, single non-expression key
    column = the FK column, default opclass / collation / sort order, non-unique, no predicate, not owned by a constraint
    (PRIMARY KEY / UNIQUE / EXCLUDE). Only then is it ours;
  * a matching index that is valid and ready is reused; a matching index that is invalid or not ready is an interrupted
    CREATE INDEX CONCURRENTLY artifact and is dropped and rebuilt, unless another session holds a SHARE UPDATE
    EXCLUSIVE (or stronger) lock on the table, which is how a running CREATE INDEX CONCURRENTLY shows up in `pg_locks`
    (readable by any role, unlike the progress views). The migration then aborts rather than race that build; this also
    trips on a concurrent VACUUM or migration, and a re-run once the table is quiet resolves it;
  * ANY other collision (different table, key, method, predicate, uniqueness, or a non-index relation) aborts the
    migration with a clear error. Nothing unrelated is ever dropped, and the plan is computed for all indexes before the
    first build, so a collision aborts with nothing built.

Locking and transactions. CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction, so the builds run in
`autocommit_block()` (as in migrations 0025 / 0048). That block COMMITS the migration transaction first, which also commits
anything an earlier revision applied in the same `alembic upgrade` run. A concurrent build does not block normal
INSERT/UPDATE/DELETE on the table, but it is not free: it waits for transactions that hold older snapshots or conflicting
locks (so a long-running transaction delays it), takes a SHARE UPDATE EXCLUSIVE lock that conflicts with schema changes,
VACUUM and other index builds on the same table, scans the table twice, and adds CPU, I/O and WAL load while it runs.

Failure and retry. `alembic_version` is only advanced after `upgrade()` returns, so an interrupted or failed run leaves the
database at 0056 with whichever indexes were already built (each committed on its own). Re-running re-plans: valid
indexes are reused, an INVALID artifact of ours is rebuilt, and an unrelated collision still aborts. Downgrade drops only
indexes matching the definition above and leaves any same-name object that is not ours untouched.

Offline mode (`alembic ... --sql`) is not supported: the plan needs the live catalog, so it raises a clear error. Run the
upgrade online.

Performance validation at representative scale is OUTSTANDING (no populated telemetry benchmark has been run); see
docs/FK_INDEX_ROLLOUT_0057.md for what must be measured and the operator runbook.

Revision ID: 0057_fk_indexes
Revises: 0056_pdu_vertical_placement
Create Date: 2026-10-23
"""
import logging
from dataclasses import dataclass

import sqlalchemy as sa
from alembic import op

revision = "0057_fk_indexes"
down_revision = "0056_pdu_vertical_placement"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# (table, column) -- index name is `ix_<table>_<column>`, matching the declarations at the bottom of each model module.
FK_INDEXES = [
    ("telemetry_reading", "mapping_id"),
    ("telemetry_reading", "mapping_revision_id"),
    ("daily_telemetry_aggregate_lineage", "mapping_revision_id"),
    ("alarm", "telemetry_reading_id"),
    ("notification_delivery", "policy_id"),
    ("notification_delivery", "channel_id"),
    ("cable", "port_connection_id"),
    ("cable", "source_neighbor_id"),
    ("port_connection", "target_port_id"),
    ("port_connection", "target_power_node_id"),
    ("rack", "model_revision_id"),
    ("equipment", "model_revision_id"),
    ("power_node", "owning_asset_id"),
    ("ups", "room_id"),
    ("power_panel", "room_id"),
    ("generator", "site_id"),
    ("rack_placement", "room_id"),
    ("bulk_import_row", "target_managed_asset_id"),
    ("bulk_import_row", "target_catalog_model_id"),
    ("bulk_import_row", "target_catalog_revision_id"),
    ("role_permission", "permission_id"),
    ("user_group_permission", "permission_id"),
]

# One row per (table, column): the resolved target plus whatever relation already carries the index name in that schema.
_INSPECT = sa.text(
    """
    SELECT t.oid AS table_oid, CAST(t.relkind AS text) AS table_kind, a.attnum AS col_attnum,
           x.oid AS rel_oid, CAST(x.relkind AS text) AS rel_kind,
           i.indrelid = t.oid AS on_target,
           am.amname AS method,
           i.indnatts AS natts, i.indnkeyatts AS nkeyatts, i.indkey[0] AS key_attnum,
           (i.indexprs IS NOT NULL) AS has_exprs, (i.indpred IS NOT NULL) AS has_pred,
           i.indisunique AS is_unique, i.indisexclusion AS is_exclusion,
           EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = x.oid) AS constraint_owned,
           EXISTS (SELECT 1 FROM pg_locks l
                    WHERE l.locktype = 'relation' AND l.relation = t.oid AND l.granted AND l.pid <> pg_backend_pid()
                      AND l.mode IN ('ShareUpdateExclusiveLock', 'ShareLock', 'ShareRowExclusiveLock',
                                     'ExclusiveLock', 'AccessExclusiveLock')) AS table_busy,
           i.indoption[0] AS key_option,
           (i.indclass[0] = (SELECT oc.oid FROM pg_opclass oc
                              WHERE oc.opcmethod = x.relam AND oc.opcdefault AND oc.opcintype = a.atttypid)) AS default_opclass,
           (i.indcollation[0] = a.attcollation) AS default_collation,
           i.indisvalid AS is_valid, i.indisready AS is_ready,
           CASE WHEN CAST(x.relkind AS text) = 'i' THEN pg_get_indexdef(x.oid) END AS indexdef
    FROM (SELECT to_regclass(format('%I.%I', current_schema(), CAST(:table AS text)))) AS tt(oid)
    LEFT JOIN pg_class t ON t.oid = tt.oid
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = :column AND NOT a.attisdropped AND a.attnum > 0
    LEFT JOIN pg_class x ON x.relname = :name AND x.relnamespace = t.relnamespace
    LEFT JOIN pg_index i ON i.indexrelid = x.oid
    LEFT JOIN pg_am am ON am.oid = x.relam
    """
)

CREATE, REUSE, REBUILD = "create", "reuse", "rebuild"


@dataclass(frozen=True)
class _Target:
    schema: str
    table: str
    column: str
    name: str

    def qualified(self, preparer) -> tuple[str, str, str]:
        return preparer.quote_schema(self.schema), preparer.quote(self.table), preparer.quote(self.name)


def _name(table: str, column: str) -> str:
    return f"ix_{table}_{column}"


def _require_online() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Migration 0057_fk_indexes cannot generate offline SQL (--sql): it inspects the live catalog to verify "
            "pre-existing indexes and uses CREATE/DROP INDEX CONCURRENTLY. Run it against the database."
        )


def _matches_definition(row) -> bool:
    """True when the relation already named like our index IS our index definition (any validity/readiness)."""
    return bool(
        row.rel_kind == "i"
        and row.on_target
        and row.method == "btree"
        and row.natts == 1
        and row.nkeyatts == 1
        and row.key_attnum == row.col_attnum
        and not row.has_exprs
        and not row.has_pred
        and not row.is_unique
        and not row.is_exclusion
        and not row.constraint_owned
        and row.key_option == 0
        and row.default_opclass
        and row.default_collation
    )


def _inspect(bind, table: str, column: str, name: str):
    return bind.execute(_INSPECT, {"table": table, "column": column, "name": name}).one()


def _plan_one(bind, schema: str, table: str, column: str) -> str:
    name = _name(table, column)
    row = _inspect(bind, table, column, name)
    where = f"{schema}.{table}({column})"
    if row.table_oid is None:
        raise RuntimeError(f"0057: target table {schema}.{table} does not exist; refusing to continue.")
    if row.table_kind != "r":
        raise RuntimeError(
            f"0057: {schema}.{table} is not an ordinary table (relkind={row.table_kind!r}); "
            "CREATE INDEX CONCURRENTLY is unsupported on partitioned tables. Refusing to continue."
        )
    if row.col_attnum is None:
        raise RuntimeError(f"0057: column {column} does not exist on {schema}.{table}; refusing to continue.")
    if row.rel_oid is None:
        return CREATE
    if _matches_definition(row):
        if row.is_valid and row.is_ready:
            return REUSE
        if row.table_busy:
            raise RuntimeError(
                f"0057: {schema}.{name} is invalid and another session holds a SHARE UPDATE EXCLUSIVE or stronger "
                f"lock on {schema}.{table}: a CREATE INDEX CONCURRENTLY on {where} may still be running. Refusing to drop "
                "and rebuild a possibly active build; wait for it to finish (or cancel it), then re-run."
            )
        return REBUILD
    found = row.indexdef or f"a non-index relation (relkind={row.rel_kind!r})"
    raise RuntimeError(
        f"0057: {schema}.{name} already exists but is not the expected index on {where} "
        f"(expected: non-unique btree on the single column {column}, no predicate, default opclass). Found: {found}. "
        "Refusing to reuse or drop an object this migration did not create; rename or drop it deliberately, then re-run."
    )


def upgrade() -> None:
    _require_online()
    bind = op.get_bind()
    preparer = bind.dialect.identifier_preparer
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    # Plan everything before the first build: a collision on any index aborts with nothing built.
    plan = [(_Target(schema, t, c, _name(t, c)), _plan_one(bind, schema, t, c)) for t, c in FK_INDEXES]
    # Commit first: CREATE/DROP INDEX CONCURRENTLY cannot run inside the migration transaction.
    with op.get_context().autocommit_block():
        for target, action in plan:
            q_schema, q_table, q_name = target.qualified(preparer)
            if action == REUSE:
                log.info("0057: reusing valid index %s.%s", target.schema, target.name)
                continue
            if action == REBUILD:
                log.warning("0057: rebuilding interrupted index artifact %s.%s", target.schema, target.name)
                op.execute(f"DROP INDEX CONCURRENTLY {q_schema}.{q_name}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY {q_name} ON {q_schema}.{q_table} ({preparer.quote(target.column)})"
            )


def downgrade() -> None:
    _require_online()
    bind = op.get_bind()
    preparer = bind.dialect.identifier_preparer
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    drops = []
    for table, column in reversed(FK_INDEXES):
        name = _name(table, column)
        row = _inspect(bind, table, column, name)
        if row.rel_oid is None:
            continue
        if _matches_definition(row):
            drops.append(preparer.quote_schema(schema) + "." + preparer.quote(name))
        else:
            log.warning("0057 downgrade: leaving %s.%s in place; it is not the index this migration created", schema, name)
    with op.get_context().autocommit_block():
        for qualified in drops:
            op.execute(f"DROP INDEX CONCURRENTLY {qualified}")
