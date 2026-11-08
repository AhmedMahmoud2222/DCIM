# ruff: noqa: E501
"""Target verification and ownership rules. The benchmark creates, loads and DROPS large databases, so it must refuse to run
anywhere it was not deliberately pointed at.

A database NAME is not evidence of anything, so nothing here relies on one. The benchmark proceeds only when ALL of these hold:

1. The operator passed `--allow-create-databases` (explicit authorization to create and drop databases).
2. The cluster was started with `cluster_name` beginning `fkbench-disposable`. That setting exists only in server start-up
   configuration (it cannot be set with SQL), so someone had to put it there on purpose.
3. The operator passed `--authorize-cluster <system_identifier>` matching `pg_control_system()` of the cluster actually
   connected to. A stale or mistyped connection string therefore cannot reach a different cluster.
4. The cluster is a primary (not in recovery) running PostgreSQL 16.
5. The admin role is not a superuser (unless `--allow-superuser` is given, which the report records) and may create databases.
6. Every database in the cluster is either a PostgreSQL system database, one of the databases named in the two connection URLs,
   or a `fkbench_*` database. Any other database means this is not a scratch cluster, and the run is refused.

Ownership: every database the tool creates is tagged with a database comment naming the run (or, for a reusable template, the
dataset). The tool will only drop a database whose name starts with `fkbench_` AND whose tag matches the caller, and will only
cancel or terminate a backend that is connected to such a database.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from sqlalchemy.engine.url import make_url

MARKER_PREFIX = "fkbench-disposable"
OWN_PREFIX = "fkbench_"
OWNER_TAG = "fkbench-owner:"
SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})
SUPPORTED_MAJOR = 16


class UnsafeTarget(RuntimeError):
    """Raised instead of doing anything that could touch a database this run does not own."""


@dataclass(frozen=True)
class Authorization:
    cluster_system_identifier: str | None = None
    allow_create_databases: bool = False
    allow_superuser: bool = False


def verify_target(
    admin_conninfo_for_postgres_db: str, admin_url: str, app_url: str, auth: Authorization, *, marker_prefix: str = MARKER_PREFIX
) -> dict:
    """Return the verified facts about the cluster, or raise UnsafeTarget naming the first failed requirement.

    `marker_prefix` exists so the tests can exercise the later checks on a CI cluster that has no marker; the CLI never sets it.
    """
    if not auth.allow_create_databases:
        raise UnsafeTarget("refusing to run: creating and dropping databases was not authorized (pass --allow-create-databases).")
    with psycopg.connect(admin_conninfo_for_postgres_db, autocommit=True) as conn:
        cluster_name = conn.execute("SHOW cluster_name").fetchone()[0]
        if not cluster_name.startswith(marker_prefix):
            raise UnsafeTarget(
                f"refusing to run: cluster_name is {cluster_name!r}, not {marker_prefix!r}*. Start a disposable cluster with "
                f"-c cluster_name={MARKER_PREFIX}-<name> (see provision_cluster.sql). This is the operator's marker that the cluster is scratch."
            )
        system_id = str(conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])
        if auth.cluster_system_identifier != system_id:
            raise UnsafeTarget(
                "refusing to run: --authorize-cluster does not match this cluster's system identifier "
                f"(connected cluster is {system_id}; given {auth.cluster_system_identifier!r}). Confirm the target, then pass the identifier."
            )
        if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            raise UnsafeTarget("refusing to run: the server is a standby (in recovery).")
        major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000
        if major != SUPPORTED_MAJOR:
            raise UnsafeTarget(f"refusing to run: PostgreSQL {major} (this benchmark is validated for {SUPPORTED_MAJOR} only).")
        is_super, can_create_db = conn.execute(
            "SELECT rolsuper, rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if is_super and not auth.allow_superuser:
            raise UnsafeTarget(
                "refusing to run as a superuser (pass --allow-superuser to override, which the report records). Use a CREATEDB "
                "role as in provision_cluster.sql."
            )
        if not can_create_db:
            raise UnsafeTarget("refusing to run: the admin role cannot create databases.")
        allowed = set(SYSTEM_DATABASES) | {make_url(admin_url).database, make_url(app_url).database}
        unknown = sorted(
            d for (d,) in conn.execute("SELECT datname FROM pg_database").fetchall() if d not in allowed and not d.startswith(OWN_PREFIX)
        )
        if unknown:
            raise UnsafeTarget(
                f"refusing to run: the cluster contains databases this benchmark does not recognise: {unknown}. "
                "It is not a scratch cluster."
            )
        return {
            "cluster_name": cluster_name,
            "system_identifier": system_id,
            "admin_is_superuser": bool(is_super),
            "server_major_version": major,
        }


def require_owned(conn: psycopg.Connection, name: str, owner: str) -> None:
    """Raise unless database `name` is a fkbench_* database tagged as created by `owner`."""
    if not name.startswith(OWN_PREFIX):
        raise UnsafeTarget(f"refusing to touch database {name!r}: not a {OWN_PREFIX}* database.")
    row = conn.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None:
        raise UnsafeTarget(f"refusing to touch database {name!r}: it does not exist.")
    if row[0] != f"{OWNER_TAG}{owner}":
        raise UnsafeTarget(f"refusing to touch database {name!r}: its owner tag is {row[0]!r}, not created by {owner!r}.")
