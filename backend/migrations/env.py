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
        if table.dialect_options.get("postgresql", {}).get("partition_by")
    }


def _attached_partition_names(connection: Connection) -> set[str]:
    """Names of tables that are VERIFIED attached partitions (per pg_inherits) of a
    partitioned parent declared in ORM metadata. Only these are excluded from
    autogenerate comparison — an arbitrary ``audit_log_*``-looking table that is not
    actually attached is still compared (and would be reported as drift), so genuine
    schema drift remains detectable."""
    parents = _partitioned_parent_names()
    if not parents:
        return set()
    rows = connection.execute(
        text(
            "SELECT child.relname FROM pg_inherits "
            "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
            "JOIN pg_namespace ns ON ns.oid = parent.relnamespace "
            "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
            "WHERE parent.relname = ANY(:parents) AND ns.nspname = current_schema()"
        ),
        {"parents": list(parents)},
    )
    return {row[0] for row in rows}


def _make_include_object(attached_partitions: set[str]):
    def include_object(obj, name, type_, reflected, table_name):  # noqa: ANN001, ANN202
        if not reflected:
            # Never suppress metadata-side objects: an index/constraint declared on a
            # model but missing from the database is genuine drift and must be reported.
            return True
        if type_ == "table" and name in attached_partitions:
            # Verified attached partition child — PostgreSQL owns its lifecycle (created
            # by raw partition DDL / maintenance job, dropped only via DETACH/DROP by an
            # operator), so autogenerate must never propose removing it.
            return False
        if type_ == "index" and table_name in attached_partitions:
            # Partition-child indexes inherited from the parent's partitioned indexes.
            return False
        return True

    return include_object


def run_migrations_offline() -> None:
    # Offline mode has no connection, so partition membership cannot be verified via
    # pg_inherits and no partition filtering is applied; `alembic check` and all
    # autogenerate validation run online.
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    attached_partitions = _attached_partition_names(connection)
    # The lookup above autobegins a SQLAlchemy transaction on the connection; close it
    # so Alembic's own transaction handling (and migrations that use
    # `autocommit_block`, e.g. 0025's CONCURRENTLY indexes) behaves exactly as before.
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_make_include_object(attached_partitions),
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
