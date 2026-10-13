import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)


def _partitioned_parent_names() -> set[str]:
    """Tables in ORM metadata that are PostgreSQL partitioned parents (e.g. audit_log)."""
    return {
        table.name
        for table in target_metadata.tables.values()
        if table.dialect_options["postgresql"].get("partition_by")
    }


def _attached_partitions(connection: Connection) -> set[tuple[str, str]]:
    """Schema-qualified identities of verified PostgreSQL partitions."""
    parents = _partitioned_parent_names()
    if not parents:
        return set()
    rows = connection.execute(
        text(
            "SELECT child_ns.nspname, child.relname FROM pg_inherits "
            "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
            "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
            "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
            "JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
            "WHERE parent.relname = ANY(:parents) "
            "AND parent_ns.nspname = current_schema() "
            "AND parent.relkind = 'p' AND child.relispartition"
        ),
        {"parents": list(parents)},
    )
    return {(row[0], row[1]) for row in rows}


def _local_partition_drift(connection: Connection, attached_partitions: set[tuple[str, str]]) -> list[str]:
    """Find indexes/constraints created locally on a partition rather than inherited.

    Parent indexes are attached through pg_inherits. PostgreSQL 16 can expose inherited
    CHECK constraints as local catalog rows with conparentid=0, so constraints are
    matched to parent definitions rather than relying on that field. The one unmatched
    local CHECK is the partition bound; additional local constraints, or any unattached
    local index, are drift.
    """
    if not attached_partitions:
        return []
    rows = connection.execute(
        text(
            "WITH partitions AS ("
            " SELECT child.oid, child.relname, child_ns.nspname "
            " FROM pg_inherits part_inh "
            " JOIN pg_class parent ON parent.oid = part_inh.inhparent "
            " JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
            " JOIN pg_class child ON child.oid = part_inh.inhrelid "
            " JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
            " WHERE parent.relname = ANY(:parents) "
            " AND parent_ns.nspname = current_schema() "
            " AND parent.relkind = 'p' AND child.relispartition"
            "), local_indexes AS ("
            " SELECT p.nspname, p.relname, idx.relname AS object_name "
            " FROM partitions p JOIN pg_index i ON i.indrelid = p.oid "
            " JOIN pg_class idx ON idx.oid = i.indexrelid "
            " WHERE NOT EXISTS (SELECT 1 FROM pg_inherits ih WHERE ih.inhrelid = idx.oid)"
            "), local_constraints AS ("
            " SELECT p.nspname, p.relname, c.conname AS object_name, c.contype "
            " FROM partitions p JOIN pg_inherits part_inh ON part_inh.inhrelid = p.oid "
            " JOIN pg_constraint c ON c.conrelid = p.oid "
            " WHERE NOT EXISTS (SELECT 1 FROM pg_constraint parent_con "
            " WHERE parent_con.conrelid = part_inh.inhparent "
            " AND parent_con.contype = c.contype "
            " AND pg_get_constraintdef(parent_con.oid) = pg_get_constraintdef(c.oid))"
            "), invalid_local_constraints AS ("
            " SELECT lc.nspname, lc.relname, lc.object_name "
            " FROM local_constraints lc WHERE lc.contype <> 'c' OR ("
            " SELECT count(*) FROM local_constraints bound "
            " WHERE bound.nspname = lc.nspname AND bound.relname = lc.relname AND bound.contype = 'c'"
            ") <> 1"
            "), missing_parent_indexes AS ("
            " SELECT p.nspname, p.relname, parent_idx.relname AS object_name "
            " FROM partitions p JOIN pg_inherits part_inh ON part_inh.inhrelid = p.oid "
            " JOIN pg_index parent_i ON parent_i.indrelid = part_inh.inhparent "
            " JOIN pg_class parent_idx ON parent_idx.oid = parent_i.indexrelid "
            " WHERE NOT EXISTS (SELECT 1 FROM pg_inherits ii "
            " JOIN pg_index child_i ON child_i.indexrelid = ii.inhrelid AND child_i.indrelid = p.oid "
            " WHERE ii.inhparent = parent_i.indexrelid)"
            "), missing_parent_constraints AS ("
            " SELECT p.nspname, p.relname, parent_con.conname AS object_name "
            " FROM partitions p JOIN pg_inherits part_inh ON part_inh.inhrelid = p.oid "
            " JOIN pg_constraint parent_con ON parent_con.conrelid = part_inh.inhparent "
            " WHERE NOT EXISTS (SELECT 1 FROM pg_constraint child_con "
            " WHERE child_con.conrelid = p.oid AND child_con.contype = parent_con.contype "
            " AND pg_get_constraintdef(child_con.oid) = pg_get_constraintdef(parent_con.oid))"
            ") SELECT nspname, relname, object_name, 'index' AS kind FROM local_indexes"
            " UNION ALL SELECT nspname, relname, object_name, 'constraint' AS kind FROM invalid_local_constraints"
            " UNION ALL SELECT nspname, relname, object_name, 'missing inherited index' AS kind FROM missing_parent_indexes"
            " UNION ALL SELECT nspname, relname, object_name, 'missing inherited constraint' AS kind "
            "FROM missing_parent_constraints"
        ),
        {"parents": list(_partitioned_parent_names())},
    )
    return [f"{row.kind} {row.nspname}.{row.relname}.{row.object_name}" for row in rows]


def _make_include_object(
    attached_partitions: set[tuple[str, str]],
    inherited_partition_indexes: set[tuple[str, str, str]],
    default_schema: str | None,
):
    def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN202
        if not reflected:
            # Never suppress metadata-side objects: an index/constraint declared on a
            # model but missing from the database is genuine drift and must be reported.
            return True
        schema = obj.schema or default_schema
        table_identity = (schema, name)
        if type_ == "table" and table_identity in attached_partitions:
            # Verified attached partition child — PostgreSQL owns its lifecycle (created
            # by raw partition DDL / maintenance job, dropped only via DETACH/DROP by an
            # operator). Local partition-only indexes/constraints are separately checked
            # by _local_partition_drift before Alembic's table-level comparison.
            return False
        if type_ == "index" and obj.table is not None:
            index_identity = (obj.table.schema or default_schema, obj.table.name, name)
            if index_identity in inherited_partition_indexes:
                # Only indexes whose PostgreSQL catalog inheritance is verified are
                # excluded; locally-created indexes are not hidden by this callback.
                return False
        return True

    return include_object


def _inherited_partition_indexes(
    connection: Connection,
    attached_partitions: set[tuple[str, str]],
) -> set[tuple[str, str, str]]:
    """Return schema-qualified child index identities attached to parent indexes."""
    if not attached_partitions:
        return set()
    rows = connection.execute(
        text(
            "SELECT child_ns.nspname, child.relname, child_idx.relname "
            "FROM pg_inherits part_inh "
            "JOIN pg_class parent ON parent.oid = part_inh.inhparent "
            "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
            "JOIN pg_class child ON child.oid = part_inh.inhrelid "
            "JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
            "JOIN pg_index parent_idx ON parent_idx.indrelid = parent.oid "
            "JOIN pg_inherits index_inh ON index_inh.inhparent = parent_idx.indexrelid "
            "JOIN pg_class child_idx ON child_idx.oid = index_inh.inhrelid "
            "JOIN pg_index child_index ON child_index.indexrelid = child_idx.oid "
            "AND child_index.indrelid = child.oid "
            "WHERE parent.relname = ANY(:parents) "
            "AND parent_ns.nspname = current_schema() "
            "AND parent.relkind = 'p' AND child.relispartition"
        ),
        {"parents": list(_partitioned_parent_names())},
    )
    return {(row[0], row[1], row[2]) for row in rows}


def run_migrations_offline() -> None:
    # Offline mode has no connection, so partition membership cannot be verified via
    # pg_inherits and no partition filtering is applied; `alembic check` and all
    # autogenerate validation run online.
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    attached_partitions = _attached_partitions(connection)
    local_drift = _local_partition_drift(connection, attached_partitions)
    if local_drift:
        raise RuntimeError(
            "Unexpected local indexes/constraints on attached audit partitions: "
            + ", ".join(local_drift)
        )
    inherited_indexes = _inherited_partition_indexes(connection, attached_partitions)
    # The lookup above autobegins a SQLAlchemy transaction on the connection; close it
    # so Alembic's own transaction handling (and migrations that use
    # `autocommit_block`, e.g. 0025's CONCURRENTLY indexes) behaves exactly as before.
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_make_include_object(
            attached_partitions,
            inherited_indexes,
            connection.dialect.default_schema_name,
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
