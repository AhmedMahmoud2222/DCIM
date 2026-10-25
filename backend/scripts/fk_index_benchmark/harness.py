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
from sqlalchemy.engine.url import make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
PREVIOUS = "0056_pdu_vertical_placement"
HEAD = "0057_fk_indexes"

_spec = importlib.util.spec_from_file_location("m0057_bench", BACKEND_DIR / "migrations" / "versions" / "0057_fk_indexes.py")
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)
FK_INDEXES: list[tuple[str, str]] = _migration.FK_INDEXES  # read-only: the benchmark never alters the migration
INDEX_NAMES = [f"ix_{t}_{c}" for t, c in FK_INDEXES]
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


def connect(urls: Urls, db: str, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(urls.conninfo(db), autocommit=autocommit)


def create_db(urls: Urls, name: str, template: str | None = None) -> None:
    with connect(urls, "postgres") as conn:
        if template is None:
            conn.execute(f'CREATE DATABASE "{name}"')
        else:
            # FILE_COPY is a file-level copy; the default WAL_LOG strategy would write the whole template to WAL again.
            conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}" STRATEGY FILE_COPY')
    if template is None:
        with connect(urls, name) as conn:
            conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO dcim_app")
            for ext in ('"uuid-ossp"', "pgcrypto", "btree_gist"):
                conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")


def drop_db(urls: Urls, name: str) -> None:
    with connect(urls, "postgres") as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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


def integrity(urls: Urls, db: str) -> dict:
    """Catalog facts about the 22 indexes and the version stamp, read from a fresh connection."""
    with connect(urls, db) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        rows = conn.execute(
            "SELECT c.relname, i.indisvalid, i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(%s)",
            (INDEX_NAMES,),
        ).fetchall()
        invalid_any = conn.execute("SELECT count(*) FROM pg_index WHERE NOT indisvalid").fetchone()[0]
        advisory = conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'").fetchone()[0]
    present = {name: (valid, ready) for name, valid, ready in rows}
    return {
        "version": version,
        "present": len(present),
        "valid_and_ready": sum(1 for v, r in present.values() if v and r),
        "invalid_named": sorted(n for n, (v, r) in present.items() if not (v and r)),
        "missing": sorted(set(INDEX_NAMES) - set(present)),
        "invalid_anywhere": invalid_any,
        "advisory_locks_held": advisory,
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
