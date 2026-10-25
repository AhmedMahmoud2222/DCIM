# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""Database plumbing shared by the scenarios and the tests: clones, alembic runs, integrity checks.

The migration is exercised exactly as an operator would run it: `python -m alembic upgrade head` in a subprocess against a
scratch database. Nothing here imports or patches migration 0057.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine.url import make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
PREVIOUS = "0056_pdu_vertical_placement"
HEAD = "0057_fk_indexes"

_spec = importlib.util.spec_from_file_location("m0057_bench", BACKEND_DIR / "migrations" / "versions" / "0057_fk_indexes.py")
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)
FK_INDEXES: list[tuple[str, str]] = _migration.FK_INDEXES  # read-only: the benchmark never alters the migration
INDEX_NAMES = [f"ix_{t}_{c}" for t, c in FK_INDEXES]
from .safety import OWN_PREFIX, OWNER_TAG, UnsafeTarget, require_owned  # noqa: E402

TARGET_TABLES = sorted({t for t, _ in FK_INDEXES})


@dataclass(frozen=True)
class Urls:
    admin: str  # sqlalchemy URL of a superuser (clones, seeding, sampling)
    app: str  # sqlalchemy URL of the migration role (what alembic connects as)

    @classmethod
    def from_env(cls) -> Urls:
        return cls(admin=os.environ["TEST_ADMIN_DATABASE_URL"], app=os.environ["DATABASE_URL"])

    def conninfo(self, db: str, *, admin: bool = True) -> str:
        url = make_url(self.admin if admin else self.app).set(database=db, drivername="postgresql")
        return url.render_as_string(hide_password=False)

    def sa_url(self, db: str) -> str:
        return make_url(self.app).set(database=db).render_as_string(hide_password=False)


def connect(urls: Urls, db: str, *, autocommit: bool = True, app_role: bool = False) -> psycopg.Connection:
    return psycopg.connect(urls.conninfo(db, admin=not app_role), autocommit=autocommit)


def create_db(urls: Urls, name: str, *, owner: str, template: str | None = None) -> None:
    """Create a fkbench_* database tagged as owned by `owner`; only that owner may later drop it (see safety.require_owned)."""
    if not name.startswith(OWN_PREFIX):
        raise UnsafeTarget(f"refusing to create database {name!r}: not a {OWN_PREFIX}* name.")
    with connect(urls, "postgres") as conn:
        if template is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        else:
            # FILE_COPY is a file-level copy; the default WAL_LOG strategy would write the whole template to WAL again.
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {} STRATEGY FILE_COPY").format(sql.Identifier(name), sql.Identifier(template))
            )
        conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(f"{OWNER_TAG}{owner}")))
    if template is None:
        with connect(urls, name) as conn:
            conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO dcim_app")
            for ext in ("uuid-ossp", "pgcrypto", "btree_gist"):
                conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))


def drop_db(urls: Urls, name: str, *, owner: str) -> None:
    """Drop a database only if it is a fkbench_* database that `owner` created. Anything else raises UnsafeTarget."""
    with connect(urls, "postgres") as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            return
        require_owned(conn, name, owner)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def signal_backend(urls: Urls, pid: int, kind: str) -> None:
    """Cancel or terminate `pid`, but only a backend connected to a fkbench_* database (never an unrelated session)."""
    if kind not in ("cancel", "terminate"):
        raise ValueError(kind)
    with connect(urls, "postgres") as conn:
        row = conn.execute("SELECT datname FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
        if row is None or not (row[0] or "").startswith(OWN_PREFIX):
            raise UnsafeTarget(f"refusing to signal pid {pid}: it is not connected to a {OWN_PREFIX}* database.")
        conn.execute(f"SELECT pg_{kind}_backend(%s)", (pid,))


def db_exists(urls: Urls, name: str) -> bool:
    with connect(urls, "postgres") as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


class AlembicRun:
    """One `alembic <args>` subprocess against `db`, started non-blocking so scenarios can act while it runs."""

    def __init__(self, urls: Urls, db: str, *args: str):
        self._log = tempfile.NamedTemporaryFile("w+", suffix=".alembic.log", delete=False)  # noqa: SIM115
        self.args = args
        self.started = time.time()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "alembic", *args],
            cwd=BACKEND_DIR,
            env={**os.environ, "DATABASE_URL": urls.sa_url(db)},
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.ended: float | None = None

    def poll(self) -> int | None:
        rc = self.proc.poll()
        if rc is not None and self.ended is None:
            self.ended = time.time()
        return rc

    def wait(self, timeout: float) -> int:
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise
        if self.ended is None:
            self.ended = time.time()
        return rc

    @property
    def seconds(self) -> float:
        return round((self.ended or time.time()) - self.started, 2)

    def output(self, tail: int = 2000) -> str:
        self._log.flush()
        return Path(self._log.name).read_text(errors="replace")[-tail:]

    def cleanup(self) -> None:
        try:
            self._log.close()
            Path(self._log.name).unlink(missing_ok=True)
        except OSError:
            pass


def alembic_ok(urls: Urls, db: str, *args: str, timeout: float = 7200) -> AlembicRun:
    run = AlembicRun(urls, db, *args)
    rc = run.wait(timeout)
    if rc != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed (rc={rc}):\n{run.output(4000)}")
    return run


VERIFY_SQL = """
SELECT c.relname, c.relkind::text, c.oid::bigint, t.relname AS tbl, tn.nspname AS tbl_schema, cn.nspname AS idx_schema,
       am.amname, i.indisvalid, i.indisready, i.indislive, i.indisunique, i.indisprimary, i.indisexclusion,
       i.indnatts, i.indnkeyatts, i.indpred IS NOT NULL AS has_pred, i.indexprs IS NOT NULL AS has_exprs,
       c.reloptions IS NOT NULL AS has_options, pg_get_indexdef(c.oid) AS def,
       EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = c.oid) AS constraint_owned,
       pg_get_userbyid(c.relowner) = pg_get_userbyid(t.relowner) AS owner_matches_table
FROM pg_class c
LEFT JOIN pg_namespace cn ON cn.oid = c.relnamespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_class t ON t.oid = i.indrelid
LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace
LEFT JOIN pg_am am ON am.oid = c.relam
WHERE c.relname = ANY(%s) AND cn.nspname = 'public'
"""


def expected_definition(table: str, column: str, name: str) -> str:
    return f"CREATE INDEX {name} ON public.{table} USING btree ({column})"


def verify_indexes(conn: psycopg.Connection, previous_oids: dict[str, int] | None = None) -> dict:
    """Check each of the 22 indexes against its COMPLETE expected definition, not just its name and validity.

    Defects are reported per index with a kind: missing, not_an_index, wrong_definition (this also catches an index of the
    right name on the wrong table or column, which is how a substituted index shows up), invalid, not_ready, not_live,
    constraint_owned, owner_mismatch, unexpected_shape, replaced (same name but a different OID than `previous_oids`).
    """
    rows = {r[0]: r for r in conn.execute(VERIFY_SQL, (INDEX_NAMES,)).fetchall()}
    defects: list[dict] = []
    for table, column in FK_INDEXES:
        name = f"ix_{table}_{column}"
        r = rows.get(name)
        if r is None:
            defects.append({"index": name, "kind": "missing"})
            continue
        (_, relkind, oid, tbl, _ts, _is, am, valid, ready, live, uniq, primary, excl, natts, nkey, pred, exprs, opts, definition, owned, owner_ok) = r
        if relkind != "i":
            defects.append({"index": name, "kind": "not_an_index", "relkind": relkind})
            continue
        if definition != expected_definition(table, column, name):
            defects.append({"index": name, "kind": "wrong_definition", "found": definition})
        if not valid:
            defects.append({"index": name, "kind": "invalid"})
        if not ready:
            defects.append({"index": name, "kind": "not_ready"})
        if not live:
            defects.append({"index": name, "kind": "not_live"})
        if owned:
            defects.append({"index": name, "kind": "constraint_owned"})
        if not owner_ok:
            defects.append({"index": name, "kind": "owner_mismatch"})
        if uniq or primary or excl or pred or exprs or opts or am != "btree" or natts != 1 or nkey != 1 or tbl != table:
            defects.append({"index": name, "kind": "unexpected_shape"})
        if previous_oids and previous_oids.get(name) not in (None, oid):
            defects.append({"index": name, "kind": "replaced", "was": previous_oids[name], "now": oid})
    return {
        "checked": len(FK_INDEXES),
        "defects": defects,
        "oids": {n: int(r[2]) for n, r in rows.items() if r[1] == "i"},
    }


def integrity(urls: Urls, db: str, previous_oids: dict[str, int] | None = None) -> dict:
    """Catalog facts about the 22 indexes (full-definition check) and the version stamp, read from a fresh connection."""
    with connect(urls, db) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        verdict = verify_indexes(conn, previous_oids)
        invalid_any = conn.execute("SELECT count(*) FROM pg_index WHERE NOT indisvalid").fetchone()[0]
        advisory = conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'").fetchone()[0]
    bad = {d["index"] for d in verdict["defects"]}
    return {
        "version": version,
        "present": len(verdict["oids"]),
        "valid_and_ready": len(INDEX_NAMES) - len(bad),  # exactly the indexes with NO defect of any kind
        "defects": verdict["defects"],
        "invalid_named": sorted(d["index"] for d in verdict["defects"] if d["kind"] in ("invalid", "not_ready", "not_live")),
        "missing": sorted(d["index"] for d in verdict["defects"] if d["kind"] == "missing"),
        "invalid_anywhere": invalid_any,
        "advisory_locks_held": advisory,
        "oids": verdict["oids"],
    }


def index_sizes(urls: Urls, db: str) -> dict:
    with connect(urls, db) as conn:
        sizes = dict(
            conn.execute("SELECT relname, pg_relation_size(oid) FROM pg_class WHERE relname = ANY(%s)", (INDEX_NAMES,)).fetchall()
        )
        tables = dict(
            conn.execute(
                "SELECT relname, pg_table_size(oid) FROM pg_class WHERE relname = ANY(%s)",
                (["telemetry_reading", "daily_telemetry_aggregate_lineage"],),
            ).fetchall()
        )
        total_idx = dict(
            conn.execute(
                "SELECT relname, pg_indexes_size(oid) FROM pg_class WHERE relname = ANY(%s)",
                (["telemetry_reading", "daily_telemetry_aggregate_lineage"],),
            ).fetchall()
        )
    return {"new_indexes_bytes": sizes, "table_bytes": tables, "all_indexes_bytes_per_table": total_idx}
