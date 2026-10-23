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

Why a plan is never trusted. A catalog read is stale the instant it is made, and the advisory lock below only orders
0057 runners against each other, not against arbitrary DDL. So nothing acts on a stale plan:

  * REUSE does no DDL, therefore it is proven at the end. Immediately before `upgrade()` returns, every table is locked in
    SHARE UPDATE EXCLUSIVE mode and ALL 22 indexes are re-inspected (right table, definition, valid, ready). That lock
    blocks DROP/CREATE INDEX (plain or CONCURRENTLY) on those tables, and it is held until Alembic has written
    `alembic_version` and committed (the lock is transaction-scoped and `upgrade()` returns inside that transaction). So a
    replacement made at any earlier point is detected and aborts the run before the stamp, and no replacement can slip in
    between the check and the stamp.
  * An interrupted artifact is dropped only under an ACCESS EXCLUSIVE lock on its table, with the object re-inspected and
    its OID compared with the planned one AFTER the lock is held, using a plain (transactional) DROP INDEX. A replacement
    that appeared after planning therefore survives and aborts the run. CONCURRENTLY is deliberately not used here: it
    cannot run in the transaction that carries the lock, and an interrupted artifact is cheap to drop. The lock wait is
    bounded by `lock_timeout` (5s if the session has none), so a busy table fails the run safely instead of queueing.
  * CREATE INDEX CONCURRENTLY has no IF NOT EXISTS: if the name appeared in the meantime it errors and nothing is stamped.

Locking and transactions. CREATE INDEX CONCURRENTLY cannot run inside a transaction, so the builds run in
`autocommit_block()` (as in migrations 0025 / 0048). That block COMMITS the migration transaction first, which also commits
anything an earlier revision applied in the same `alembic upgrade` run. A concurrent build does not block normal
INSERT/UPDATE/DELETE on the table, but it is not free: it waits for transactions that hold older snapshots or conflicting
locks (so a long-running transaction delays it), takes a SHARE UPDATE EXCLUSIVE lock that conflicts with schema changes,
VACUUM and other index builds on the same table, scans the table twice, and adds CPU, I/O and WAL load while it runs. The
final verification holds SHARE UPDATE EXCLUSIVE on the 21 target tables only until the version update commits (DML is not
blocked; DDL, VACUUM and ANALYZE on them wait). If other revisions run in the same `alembic upgrade`, those locks are held
until that whole run commits.

Serialization. A session-level advisory lock (migrations/migration_lock.py) is acquired here and RELEASED BY env.py after
`context.run_migrations()` commits. Alembic writes `alembic_version` after this function returns, so releasing inside the
migration would let a second runner still see 0056 and repeat the work. A second concurrent runner fails fast.

Failure and retry. `alembic_version` is only advanced after `upgrade()` returns, so an interrupted or failed run leaves the
database at 0056 with whichever indexes were already built (each committed on its own). Re-running re-plans: valid
indexes are reused, an INVALID artifact of ours is rebuilt, and an unrelated collision still aborts.

Downgrade is all-or-nothing. It takes ACCESS EXCLUSIVE on every table that carries one of the indexes, re-inspects under
those locks, and removes all of them with ONE plain `DROP INDEX a, b, c, ...` statement in the migration's own transaction:
the drops and the version downgrade commit together or not at all, so a failure (permission, `lock_timeout`, cancellation,
lost connection) leaves 0057 stamped with all 22 indexes present. The trade-off, accepted for a rollback path, is that the
tables are unavailable to reads and writes from lock acquisition to commit (a metadata-only operation; the wait is bounded
by `lock_timeout`, 5s if the session has none), instead of the non-blocking but non-atomic DROP INDEX CONCURRENTLY. Only
indexes matching the definition above are dropped; any same-name object that is not ours is left untouched.

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

# Applied only when the session has no lock_timeout of its own (an operator-set value is always respected).
_DROP_LOCK_TIMEOUT = "5s"
_VERIFY_LOCK_TIMEOUT = "60s"


@dataclass(frozen=True)
class _Target:
    table: str
    column: str

    @property
    def name(self) -> str:
        return f"ix_{self.table}_{self.column}"


def _acquire_migration_lock(bind) -> None:
    # Imported lazily: tools that merely load the revision files (scripts/check_alembic_single_head.py) do not have
    # backend/ on sys.path, whereas env.py (which actually runs migrations) always does.
    from migrations.migration_lock import acquire_migration_lock

    acquire_migration_lock(bind)  # held until env.py releases it after the version update has committed


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


def _inspect(bind, target: _Target):
    return bind.execute(_INSPECT, {"table": target.table, "column": target.column, "name": target.name}).one()


def _bound_lock_wait(bind, fallback: str) -> None:
    """Bound lock waits in the current transaction unless the operator already configured lock_timeout."""
    if bind.scalar(sa.text("SELECT current_setting('lock_timeout')")) in ("0", "0ms"):
        bind.execute(sa.text(f"SET LOCAL lock_timeout = '{fallback}'"))


def _lock_tables(bind, schema: str, tables: list[str], mode: str) -> None:
    """Lock distinct tables in sorted order (one statement, so the order is deterministic and deadlock-resistant)."""
    preparer = bind.dialect.identifier_preparer
    names = ", ".join(f"{preparer.quote_schema(schema)}.{preparer.quote(t)}" for t in sorted(set(tables)))
    bind.execute(sa.text(f"LOCK TABLE {names} IN {mode} MODE"))


def _plan_one(bind, schema: str, target: _Target) -> tuple[str, int | None]:
    """Return (action, oid of the existing relation if any). Raises on anything that is not ours."""
    name, table, column = target.name, target.table, target.column
    row = _inspect(bind, target)
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
        return CREATE, None
    if _matches_definition(row):
        if row.is_valid and row.is_ready:
            return REUSE, row.rel_oid
        if row.table_busy:
            raise RuntimeError(
                f"0057: {schema}.{name} is invalid and another session holds a SHARE UPDATE EXCLUSIVE or stronger "
                f"lock on {schema}.{table}: a CREATE INDEX CONCURRENTLY on {where} may still be running. Refusing to drop "
                "and rebuild a possibly active build; wait for it to finish (or cancel it), then re-run."
            )
        return REBUILD, row.rel_oid
    found = row.indexdef or f"a non-index relation (relkind={row.rel_kind!r})"
    raise RuntimeError(
        f"0057: {schema}.{name} already exists but is not the expected index on {where} "
        f"(expected: non-unique btree on the single column {column}, no predicate, default opclass). Found: {found}. "
        "Refusing to reuse or drop an object this migration did not create; rename or drop it deliberately, then re-run."
    )


def _drop_interrupted_artifacts(bind, schema: str, rebuilds: list[tuple[_Target, int | None]]) -> None:
    """Drop planned INVALID artifacts, but only the very objects that were planned, and only while locked out."""
    preparer = bind.dialect.identifier_preparer
    _bound_lock_wait(bind, _DROP_LOCK_TIMEOUT)
    _lock_tables(bind, schema, [t.table for t, _ in rebuilds], "ACCESS EXCLUSIVE")
    for target, planned_oid in rebuilds:
        try:
            action, oid = _plan_one(bind, schema, target)  # re-inspect now that no one can change it under us
        except RuntimeError as exc:  # replaced by something that is not ours
            raise RuntimeError(
                f"0057: {schema}.{target.name} changed while the migration was running (planned an interrupted "
                f"artifact with oid {planned_oid}); refusing to drop it. Re-run. Cause: {exc}"
            ) from exc
        if (action, oid) != (REBUILD, planned_oid):
            raise RuntimeError(
                f"0057: {schema}.{target.name} changed while the migration was running (planned an interrupted "
                f"artifact with oid {planned_oid}, found action={action!r} oid={oid}); refusing to drop it. Re-run."
            )
        bind.execute(sa.text(f"DROP INDEX {preparer.quote_schema(schema)}.{preparer.quote(target.name)}"))


def _verify_all_and_pin(bind, schema: str, targets: list[_Target]) -> None:
    """Prove all 22 indexes are ours and usable, and keep DDL out until Alembic has stamped and committed.

    Runs in the transaction that Alembic commits together with the `alembic_version` update; SHARE UPDATE EXCLUSIVE
    is released only at that commit.
    """
    _bound_lock_wait(bind, _VERIFY_LOCK_TIMEOUT)
    _lock_tables(bind, schema, [t.table for t in targets], "SHARE UPDATE EXCLUSIVE")
    for target in targets:
        row = _inspect(bind, target)
        if not (_matches_definition(row) and row.is_valid and row.is_ready):
            found = row.indexdef or "nothing"
            raise RuntimeError(
                f"0057: {schema}.{target.name} is not the expected valid index at the end of the migration "
                f"(found: {found}). Refusing to stamp 0057; fix or drop it deliberately, then re-run."
            )


def upgrade() -> None:
    _require_online()
    bind = op.get_bind()
    _acquire_migration_lock(bind)
    preparer = bind.dialect.identifier_preparer
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    targets = [_Target(t, c) for t, c in FK_INDEXES]
    # Plan everything before the first build: a collision on any index aborts with nothing built.
    plan = [(target, *_plan_one(bind, schema, target)) for target in targets]
    rebuilds = [(target, oid) for target, action, oid in plan if action == REBUILD]
    if rebuilds:
        _drop_interrupted_artifacts(bind, schema, rebuilds)
    to_build = [target for target, action, _ in plan if action != REUSE]
    for target, action, _ in plan:
        if action == REUSE:
            log.info("0057: reusing valid index %s.%s", schema, target.name)
        elif action == REBUILD:
            log.warning("0057: rebuilding interrupted index artifact %s.%s", schema, target.name)
    if to_build:
        # Commit first: CREATE INDEX CONCURRENTLY cannot run inside the migration transaction. The session advisory
        # lock stays held across that commit.
        with op.get_context().autocommit_block():
            for target in to_build:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY {preparer.quote(target.name)} "
                    f"ON {preparer.quote_schema(schema)}.{preparer.quote(target.table)} ({preparer.quote(target.column)})"
                )
    _verify_all_and_pin(bind, schema, targets)


def downgrade() -> None:
    _require_online()
    bind = op.get_bind()
    _acquire_migration_lock(bind)
    preparer = bind.dialect.identifier_preparer
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    targets = [_Target(t, c) for t, c in reversed(FK_INDEXES)]
    present_tables = [t.table for t in targets if _inspect(bind, t).table_oid is not None]
    if not present_tables:
        return
    # Everything below is ONE transaction (Alembic's), so the drops and the version downgrade commit together or not at all.
    _bound_lock_wait(bind, _DROP_LOCK_TIMEOUT)
    _lock_tables(bind, schema, present_tables, "ACCESS EXCLUSIVE")
    drops = []
    for target in targets:
        row = _inspect(bind, target)  # under the locks above: cannot change before the drop
        if row.rel_oid is None:
            continue
        if _matches_definition(row):
            drops.append(f"{preparer.quote_schema(schema)}.{preparer.quote(target.name)}")
        else:
            log.warning(
                "0057 downgrade: leaving %s.%s in place; it is not the index this migration created", schema, target.name
            )
    if drops:
        bind.execute(sa.text("DROP INDEX " + ", ".join(drops)))
