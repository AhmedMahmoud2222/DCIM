import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata
from migrations.schema_drift import (
    attached_partitions as _attached_partitions_for,
)
from migrations.schema_drift import (
    existing_parent_tables as _existing_parent_tables_for,
)
from migrations.schema_drift import (
    inherited_partition_indexes as _inherited_partition_indexes_for,
)
from migrations.schema_drift import (
    local_partition_drift as _local_partition_drift_for,
)
from migrations.schema_drift import (
    make_include_object as _make_include_object,
)
from migrations.schema_drift import (
    missing_or_modified_parent_metadata_constraints as _missing_or_modified_parent_metadata_constraints_for,
)
from migrations.schema_drift import partitioned_parent_names as _partitioned_parent_names

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)


def _attached_partitions(connection: Connection) -> set[tuple[str, str]]:
    return _attached_partitions_for(connection, _partitioned_parent_names(target_metadata))


def _local_partition_drift(connection: Connection, attached_partitions: set[tuple[str, str]]) -> list[str]:
    return _local_partition_drift_for(connection, attached_partitions, _partitioned_parent_names(target_metadata))


def _inherited_partition_indexes(
    connection: Connection,
    attached_partitions: set[tuple[str, str]],
) -> set[tuple[str, str, str]]:
    return _inherited_partition_indexes_for(
        connection, attached_partitions, _partitioned_parent_names(target_metadata)
    )


def _existing_parent_tables(connection: Connection) -> set[str]:
    return _existing_parent_tables_for(connection, _partitioned_parent_names(target_metadata))


def _missing_or_modified_parent_metadata_constraints(
    connection: Connection,
    existing_parents: set[str],
) -> list[str]:
    return _missing_or_modified_parent_metadata_constraints_for(connection, existing_parents, target_metadata)


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

    # Post-migration schema drift check: runs after context.run_migrations() so legitimate
    # migration upgrades can restore schema objects, while fail-closed drift verification
    # still rejects missing/modified/unexpected constraints or indexes for alembic check.
    existing_parents = _existing_parent_tables(connection)
    updated_attached_partitions = _attached_partitions(connection)
    local_drift = _local_partition_drift(
        connection, updated_attached_partitions
    ) + _missing_or_modified_parent_metadata_constraints(connection, existing_parents)
    if local_drift:
        raise RuntimeError(
            "Unexpected local indexes/constraints on attached audit partitions: "
            + ", ".join(local_drift)
        )


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
