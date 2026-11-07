"""Collect evidence without executing migrations or modifying the database."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import psycopg
from alembic.script import ScriptDirectory
from psycopg.conninfo import conninfo_to_dict

VERSION = "1.0.0"
BACKEND = Path(__file__).resolve().parents[2]
SETUP = ("BEGIN READ ONLY", "SET default_transaction_read_only = on")
READ_ONLY = "SHOW transaction_read_only"
ROLE = "SHOW is_superuser"
SYSTEM = "SELECT system_identifier::text FROM pg_control_system()"
REPLICATION = "SELECT write_lag, flush_lag, replay_lag FROM pg_stat_replication"
TABLE = """SELECT pg_total_relation_size(c.oid), pg_relation_size(c.oid), pg_indexes_size(c.oid), c.reltuples
FROM pg_catalog.pg_class c WHERE c.oid = to_regclass(%s)"""
INDEX = """SELECT c.relkind = 'i', i.indisvalid, i.indisready,
CASE WHEN c.relkind = 'i' THEN pg_relation_size(c.oid) END
FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
WHERE c.oid = to_regclass(%s)"""
QUERIES = frozenset(
    [
        READ_ONLY,
        ROLE,
        SYSTEM,
        REPLICATION,
        TABLE,
        INDEX,
        "SELECT version_num FROM alembic_version",
        "SHOW server_version",
        "SELECT pg_is_in_recovery()",
        "SHOW wal_level",
        "SHOW archive_mode",
        "SHOW max_wal_size",
        "SELECT last_archived_time, last_failed_time FROM pg_stat_archiver",
    ]
)
OPERATOR_FIELDS = ("free_disk", "backup_pitr_status", "ingest_peak", "retention_churn")


class Refused(RuntimeError):
    """Unsafe session or unsafe statement."""


def check_statement(statement: str, *, phase: str = "evidence") -> str:
    """Only fixed setup/teardown or reviewed evidence SQL can reach execute."""
    value = statement.strip()
    if value.endswith(";"):
        value = value[:-1].rstrip()
    allowed = SETUP if phase == "setup" else ("ROLLBACK",) if phase == "teardown" else ()
    if value in allowed:
        return value
    if phase != "evidence" or not value.startswith(("SELECT ", "SHOW ")):
        raise Refused("Statement is outside the read-only allowlist")
    if ";" in value or "--" in value or "/*" in value or re.search(r"\bINTO\b", value, re.IGNORECASE):
        raise Refused("Unsafe evidence statement")
    if value not in QUERIES:
        raise Refused("Unreviewed evidence statement")
    return value


def execute(cursor: Any, statement: str, params: Any = None, *, phase: str = "evidence") -> None:
    cursor.execute(check_statement(statement, phase=phase), params)


def setup(cursor: Any) -> None:
    for statement in SETUP:
        execute(cursor, statement, phase="setup")
    execute(cursor, READ_ONLY)
    if cursor.fetchone() != ("on",):
        raise Refused("Refusing unsafe session: transaction_read_only is not on")
    execute(cursor, ROLE)
    if cursor.fetchone() != ("off",):
        raise Refused("Refusing superuser role: evidence collection requires a least-privilege non-superuser")


def ok(value: Any) -> dict[str, Any]:
    return {"status": "ok", "value": value}


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def revision_state(rows: list[str] | None, scripts: ScriptDirectory, target: str) -> str:
    try:
        heads = scripts.get_heads()
        if not rows or len(rows) > 1 or len(heads) != 1:
            return "unknown"
        chain = [r.revision for r in scripts.walk_revisions(base="base", head=heads[0])]
        if target not in chain or rows[0] not in chain:
            return "unknown"
        return "applied" if chain.index(rows[0]) <= chain.index(target) else "pending"
    except Exception:
        return "unknown"


def collect(cursor: Any, label: str) -> tuple[dict[str, Any], str | None]:
    setup(cursor)

    def query(statement: str, params: Any = None) -> dict[str, Any]:
        try:
            execute(cursor, statement, params)
            return ok(cursor.fetchall())
        except psycopg.Error as exc:
            # A failed SELECT aborts the transaction. Restart read-only on the SAME connection.
            execute(cursor, "ROLLBACK", phase="teardown")
            setup(cursor)
            reason = "missing privilege" if exc.sqlstate == "42501" else "SQL read unavailable"
            return unavailable(reason)

    scripts = ScriptDirectory(str(BACKEND / "migrations"))
    migration = scripts.get_revision("0057_fk_indexes")
    if migration is None:
        raise RuntimeError("Migration metadata unavailable")
    module = migration.module
    names = [module._Target(t, c).name for t, c in module.FK_INDEXES]
    fields: dict[str, Any] = {}
    fields["alembic_version"] = query("SELECT version_num FROM alembic_version")
    rows = fields["alembic_version"].get("value")
    state = revision_state(None if rows is None else [r[0] for r in rows], scripts, module.revision)
    fields["migration_0057"] = ok(state) if state != "unknown" else {**unavailable("revision state unknown"), "value": state}
    for key, statement in (
        ("server_version", "SHOW server_version"),
        ("in_recovery", "SELECT pg_is_in_recovery()"),
        ("wal_level", "SHOW wal_level"),
        ("archive_mode", "SHOW archive_mode"),
        ("max_wal_size", "SHOW max_wal_size"),
    ):
        fields[key] = query(statement)
    archive = query("SELECT last_archived_time, last_failed_time FROM pg_stat_archiver")
    for position, key in enumerate(("last_archived_time", "last_failed_time")):
        fields[key] = ok(archive["value"][0][position]) if archive.get("value") else archive.copy()
    replicas = query(REPLICATION)
    fields["replica_count"] = ok(len(replicas["value"])) if replicas["status"] == "ok" else replicas.copy()
    fields["replication_lag"] = replicas
    for table in ("telemetry_reading", "daily_telemetry_aggregate_lineage"):
        result = query(TABLE, (table,))
        for position, metric in enumerate(("total_bytes", "relation_bytes", "index_bytes", "reltuples_estimate")):
            fields[f"{table}.{metric}"] = (
                ok(result["value"][0][position])
                if result.get("value")
                else unavailable(result.get("reason", "relation not found in search_path"))
            )
    for name in names:
        result = query(INDEX, (name,))
        if result["status"] == "ok":
            values = result["value"]
            fields[name] = (
                ok(dict(zip(("exists", "indisvalid", "indisready", "size_bytes"), values[0], strict=True)))
                if values
                else ok({"exists": False, "indisvalid": None, "indisready": None, "size_bytes": None})
            )
        else:
            fields[name] = result
    raw = query(SYSTEM)
    system_id = str(raw["value"][0][0]) if raw.get("value") else None
    fields["system_identifier_sha256"] = ok(hashlib.sha256(system_id.encode()).hexdigest()) if system_id else raw
    for key in OPERATOR_FIELDS:
        fields[key] = {
            "status": "operator_supplied",
            "value": None,
            "reason": "Cannot be established by SQL; operator must supply evidence",
        }
    return {
        "tool_version": VERSION,
        "utc_timestamp": datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"),
        "environment_label": label,
        "statement": "Nothing was modified by this collector; no transaction was committed.",
        "fields": fields,
    }, system_id


def render(report: dict[str, Any]) -> tuple[str, str]:
    """Serialize the report as (json, markdown)."""
    encoded = json.dumps(report, indent=2, default=str, ensure_ascii=False) + "\n"

    def cell(value: Any) -> str:
        return json.dumps(value, default=str, ensure_ascii=False).replace("|", "&#124;").replace("<", "&lt;")

    metadata = "\n".join(f"{key}: {cell(value)}" for key, value in report.items() if key != "fields")
    lines = [
        "# Migration 0057 preflight evidence",
        "",
        metadata,
        "",
        "| Field | Status | Value / reason |",
        "| --- | --- | --- |",
    ]
    for key, field in report["fields"].items():
        lines.append(f"| {key} | {field['status']} | {cell({k: v for k, v in field.items() if k != 'status'})} |")
    return encoded, "\n".join(lines) + "\n"


def _strings(value: Any) -> list[str]:
    """Every string in a report, keys included, before any serializer can escape it."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return _strings(str(value)) if value is not None else []


def _present(secret: str | None, report: dict[str, Any]) -> bool:
    """True when the secret appears raw in any value or in any serialized form of the report."""
    if not secret:
        return False
    escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
    forms = {
        secret,
        quote(secret, safe=""),
        quote(secret),
        quote_plus(secret),
        escaped,
        escaped.replace("|", "&#124;").replace("<", "&lt;"),
        secret.replace("|", "&#124;").replace("<", "&lt;"),
    }
    texts = [*_strings(report), *render(report)]
    return any(form in text for form in forms for text in texts)


def write_reports(report: dict[str, Any], dsn: str, raw_id: str | None, directory: Path) -> None:
    encoded, markdown = render(report)
    connection = conninfo_to_dict(dsn)
    # Credentials, the DSN and the raw system identifier must never appear anywhere, label included.
    hard = [dsn, raw_id, str(connection["password"]) if connection.get("password") else None]
    try:
        # The password exactly as typed in a URI DSN (still percent-encoded), which libpq decodes above.
        hard.append(urlsplit(dsn).password)
    except ValueError:
        pass
    # Host, database and user names may equal the operator's own label (a database called
    # "staging" labelled "staging"). Scan them in a render with only the label field blanked,
    # so a leak anywhere else is still caught even when it matches the label.
    soft = [str(connection[k]) for k in ("host", "hostaddr", "dbname", "user") if connection.get(k)]
    blanked_report = {**report, "environment_label": ""}
    # Check raw values and every serialized form, so quotes, backslashes, pipes or angle brackets
    # that a serializer escapes cannot hide a secret. Never print rejected content.
    if any(_present(secret, report) for secret in hard):
        raise Refused("Output privacy self-check failed (credential or identifier); no evidence files written")
    if any(_present(secret, blanked_report) for secret in soft):
        raise Refused("Output privacy self-check failed (connection identifier); no evidence files written")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", report["environment_label"])[:80] or "environment"
    stem = f"preflight_0057_{slug}_{report['utc_timestamp']}"
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for suffix, content in (("json", encoded), ("md", markdown)):
            target = directory / f"{stem}.{suffix}"
            with target.open("x", encoding="utf-8") as output:
                written.append(target)
                output.write(content)
    except OSError:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-label", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    dsn = os.environ.get("DCIM_PREFLIGHT_0057_DSN")
    if not dsn:
        print("DSN environment variable DCIM_PREFLIGHT_0057_DSN is required", file=sys.stderr)
        return 3
    try:
        # Autocommit prevents an implicit BEGIN before our explicit BEGIN READ ONLY.
        # Never use the connection context manager, whose successful exit commits.
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        try:
            with conn.cursor() as cursor:
                try:
                    report, raw_id = collect(cursor, args.environment_label)
                finally:
                    # A dead connection must not replace the original Refused (exit 2) with exit 3.
                    with contextlib.suppress(psycopg.Error):
                        execute(cursor, "ROLLBACK", phase="teardown")
        finally:
            conn.close()
        write_reports(report, dsn, raw_id, args.output_dir)
        return 0
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Driver exceptions can contain connection identifiers and credentials.
        print("Could not connect, read evidence, or write reports; no connection details emitted", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
