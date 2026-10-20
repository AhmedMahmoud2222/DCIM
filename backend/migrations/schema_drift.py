"""PostgreSQL partition- and parent-constraint-aware schema-drift helpers for Alembic.

Extracted from env.py (which Alembic loads as a standalone script via exec, not as an
importable module) so these functions can be unit-tested directly against a real
PostgreSQL connection instead of only indirectly through `alembic check` subprocess
fixtures.
"""

import uuid

from sqlalchemy import CheckConstraint, MetaData, text
from sqlalchemy.engine import Connection


class SchemaDriftNormalizationError(RuntimeError):
    """A CHECK-constraint expression could not be normalized/verified against PostgreSQL.

    Always treated as drift by callers (fail-closed): an expression whose match to the
    live catalog definition cannot be verified must never be silently accepted as
    matching just because normalization itself failed.
    """


def partitioned_parent_names(target_metadata: MetaData) -> set[str]:
    """Tables in ORM metadata that are PostgreSQL partitioned parents (e.g. audit_log)."""
    return {
        table.name
        for table in target_metadata.tables.values()
        if table.dialect_options["postgresql"].get("partition_by")
    }


def attached_partitions(connection: Connection, parents: set[str]) -> set[tuple[str, str]]:
    """Schema-qualified identities of verified PostgreSQL partitions."""
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


def local_partition_drift(
    connection: Connection, attached_partitions_: set[tuple[str, str]], parents: set[str]
) -> list[str]:
    """Find indexes/constraints created locally on a partition rather than inherited.

    Parent indexes are attached through pg_inherits. PostgreSQL 16 can expose inherited
    CHECK constraints as local catalog rows with conparentid=0, so constraints are
    matched to parent definitions rather than relying on that field. The one unmatched
    local CHECK is the partition bound; additional local constraints, or any unattached
    local index, are drift.
    """
    if not attached_partitions_:
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
            " FROM local_constraints lc"
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
        {"parents": list(parents)},
    )
    return [f"{row.kind} {row.nspname}.{row.relname}.{row.object_name}" for row in rows]


def make_include_object(
    attached_partitions_: set[tuple[str, str]],
    inherited_partition_indexes_: set[tuple[str, str, str]],
    default_schema: str | None,
):
    def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN202
        if not reflected:
            # Never suppress metadata-side objects: an index/constraint declared on a
            # model but missing from the database is genuine drift and must be reported.
            return True
        if type_ == "table":
            schema = obj.schema or default_schema
            if (schema, name) in attached_partitions_:
                # Verified attached partition child — PostgreSQL owns its lifecycle (created
                # by raw partition DDL / maintenance job, dropped only via DETACH/DROP by an
                # operator). Local partition-only indexes/constraints are separately checked
                # by local_partition_drift before Alembic's table-level comparison.
                return False
        if type_ == "index" and obj.table is not None:
            index_identity = (obj.table.schema or default_schema, obj.table.name, name)
            if index_identity in inherited_partition_indexes_:
                # Only indexes whose PostgreSQL catalog inheritance is verified are
                # excluded; locally-created indexes are not hidden by this callback.
                return False
        return True

    return include_object


def inherited_partition_indexes(
    connection: Connection,
    attached_partitions_: set[tuple[str, str]],
    parents: set[str],
) -> set[tuple[str, str, str]]:
    """Return schema-qualified child index identities attached to parent indexes."""
    if not attached_partitions_:
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
        {"parents": list(parents)},
    )
    return {(row[0], row[1], row[2]) for row in rows}


def existing_parent_tables(connection: Connection, parents: set[str]) -> set[str]:
    """Parent table names in ORM metadata that currently exist in PostgreSQL catalog as partitioned tables."""
    if not parents:
        return set()
    rows = connection.execute(
        text(
            "SELECT parent.relname FROM pg_class parent "
            "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
            "WHERE parent.relname = ANY(:parents) "
            "AND parent_ns.nspname = current_schema() "
            "AND parent.relkind = 'p'"
        ),
        {"parents": list(parents)},
    )
    return {row[0] for row in rows}


def _normalize_check_constraint_def(
    connection: Connection, schema: str, table_name: str, sqltext_str: str
) -> str:
    """Ask PostgreSQL to parse and normalize a CHECK expression via a temporary probe.

    `sqltext_str` must come from trusted ORM metadata, never from user input. The probe
    clones `schema.table_name` into a TEMP table with a per-call unique name, inside its
    own SAVEPOINT, so a malformed expression never poisons the caller's transaction and
    concurrent probes never collide. The result is read back only from this session's
    temporary schema.

    Always returns a non-blank normalized definition or raises SchemaDriftNormalizationError
    (exception, missing row, blank result, blank input) so a caller can never mistake a
    failed normalization for "no drift".
    """
    if not isinstance(sqltext_str, str) or not sqltext_str.strip():
        raise SchemaDriftNormalizationError(
            f"blank CHECK expression for {schema}.{table_name} cannot be normalized"
        )
    preparer = connection.dialect.identifier_preparer
    probe_id = uuid.uuid4().hex
    probe_table = f"_norm_probe_{probe_id}"
    probe_constraint = f"_norm_probe_c_{probe_id}"
    parent_ref = f"{preparer.quote_schema(schema)}.{preparer.quote(table_name)}"
    probe_ref = f"pg_temp.{preparer.quote(probe_table)}"
    try:
        with connection.begin_nested():
            connection.execute(
                text(f"CREATE TEMP TABLE {preparer.quote(probe_table)} (LIKE {parent_ref} INCLUDING DEFAULTS)")
            )
            connection.execute(
                text(
                    f"ALTER TABLE {probe_ref} ADD CONSTRAINT {preparer.quote(probe_constraint)} "
                    f"CHECK ({sqltext_str})"
                )
            )
            norm_def = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relnamespace = pg_my_temp_schema() AND t.relname = :probe_table "
                    "AND c.conname = :probe_constraint AND c.contype = 'c'"
                ),
                {"probe_table": probe_table, "probe_constraint": probe_constraint},
            ).scalar()
            connection.execute(text(f"DROP TABLE {probe_ref}"))
    except Exception as exc:
        raise SchemaDriftNormalizationError(
            f"could not normalize CHECK expression against {schema}.{table_name}: {exc}"
        ) from exc
    if norm_def is None or not str(norm_def).strip():
        raise SchemaDriftNormalizationError(
            f"normalizing CHECK expression against {schema}.{table_name} produced no catalog definition "
            f"(probe {probe_table}.{probe_constraint})"
        )
    return str(norm_def)


def missing_or_modified_parent_metadata_constraints(
    connection: Connection,
    existing_parents: set[str],
    target_metadata: MetaData,
) -> list[str]:
    """Find check constraints declared in ORM metadata on parent tables that are missing or modified in PostgreSQL catalog."""
    if not existing_parents:
        return []
    drift = []
    schema = connection.dialect.default_schema_name or "public"
    for parent_name in existing_parents:
        table = target_metadata.tables.get(parent_name)
        if table is None:
            continue
        db_constraints = {
            row[0]: row[1]
            for row in connection.execute(
                text(
                    "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class parent ON parent.oid = c.conrelid "
                    "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
                    "WHERE parent.relname = :parent_name AND parent_ns.nspname = current_schema() "
                    "AND c.contype = 'c'"
                ),
                {"parent_name": parent_name},
            )
        }
        declared = {c.name for c in table.constraints if isinstance(c, CheckConstraint) and c.name}
        drift.extend(
            f"unexpected parent constraint {schema}.{parent_name}.{name}"
            for name in sorted(set(db_constraints) - declared)
        )
        for c in table.constraints:
            if not (isinstance(c, CheckConstraint) and c.name):
                continue
            if c.name not in db_constraints:
                drift.append(f"missing parent constraint {schema}.{parent_name}.{c.name}")
                continue
            actual = db_constraints[c.name]
            try:
                norm = _normalize_check_constraint_def(connection, schema, parent_name, str(c.sqltext))
            except SchemaDriftNormalizationError as exc:
                # Fail closed: an expression that cannot be verified is reported as
                # drift rather than silently accepted as matching.
                drift.append(
                    f"could not verify parent constraint {schema}.{parent_name}.{c.name} against "
                    f"PostgreSQL, treated as drift: {exc}"
                )
                continue
            if actual != norm:
                drift.append(f"modified parent constraint {schema}.{parent_name}.{c.name}")
    return drift
