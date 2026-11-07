"""Evidence classification, privacy and optional CI database collection."""

import json
from unittest.mock import MagicMock

import psycopg
import pytest
from alembic.script import ScriptDirectory

from scripts.preflight_0057 import __main__ as p


@pytest.fixture(autouse=True)
def _clean_tables():
    # This suite collects catalog evidence only. Override the application's destructive cleanup fixture.
    yield


def test_revision_states(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    for revision, parent in (("before", None), ("target", "before"), ("after", "target")):
        (versions / f"{revision}.py").write_text(f"revision = {revision!r}\ndown_revision = {parent!r}\n")
    scripts = ScriptDirectory(str(tmp_path))
    for rows, expected in (
        ([], "unknown"),
        (["before"], "pending"),
        (["target"], "applied"),
        (["after"], "applied"),
        (None, "unknown"),
        (["bad"], "unknown"),
        (["before", "target"], "unknown"),
    ):
        assert p.revision_state(rows, scripts, "target") == expected
    (versions / "fork.py").write_text("revision = 'fork'\ndown_revision = 'before'\n")
    assert p.revision_state([], ScriptDirectory(str(tmp_path)), "target") == "unknown"


class EvidenceCursor:
    def __init__(self, *, fail_version=False):
        self.calls = []
        self.fail_version = fail_version

    def execute(self, query, params=None):
        self.calls.append(query)
        if self.fail_version and query == "SELECT version_num FROM alembic_version":
            raise psycopg.errors.InsufficientPrivilege("sensitive server error")

    def fetchone(self):
        return ("off" if self.calls[-1] == p.ROLE else "on",)

    def fetchall(self):
        query = self.calls[-1]
        if query == p.SYSTEM:
            return [("9876543210987654321",)]
        if query == "SELECT version_num FROM alembic_version":
            return [("0056_pdu_vertical_placement",)]
        if query == p.TABLE:
            return [(100, 50, 50, 123.0)]
        if query == p.INDEX:
            return [(True, True, True, 20)]
        if "last_archived_time" in query:
            return [(None, None)]
        if query == p.REPLICATION:
            return []
        return [("evidence",)]


def test_collection_privacy_and_placeholders(tmp_path):
    cursor = EvidenceCursor()
    report, raw = p.collect(cursor, "staging / west")
    assert report["fields"]["migration_0057"] == p.ok("pending")
    for name in p.OPERATOR_FIELDS:
        assert report["fields"][name]["status"] == "operator_supplied"
        assert report["fields"][name]["value"] is None
    dsn = "postgresql://sentinel-user:sentinel-password@sentinel-host:6543/sentinel-db"
    p.write_reports(report, dsn, raw, tmp_path)
    assert len(list(tmp_path.iterdir())) == 2
    for path in tmp_path.iterdir():
        content = path.read_text()
        for secret in (dsn, "sentinel-host", "sentinel-user", "sentinel-db", raw):
            assert secret not in content
    parsed = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert len(parsed["fields"]["system_identifier_sha256"]["value"]) == 64
    assert cursor.calls[:2] == list(p.SETUP)


def test_failed_read_is_unavailable_and_restarts_read_only():
    cursor = EvidenceCursor(fail_version=True)
    report, _ = p.collect(cursor, "staging")
    assert report["fields"]["alembic_version"] == p.unavailable("missing privilege")
    assert report["fields"]["migration_0057"]["value"] == "unknown"
    rollback = cursor.calls.index("ROLLBACK")
    assert cursor.calls[rollback + 1 : rollback + 3] == list(p.SETUP)
    assert report["fields"]["server_version"]["status"] == "ok"


@pytest.mark.parametrize(
    "leak", ["postgresql://sentinel-user:sentinel-password@sentinel-host/sentinel-db", "sentinel-password", "9876543210987654321"]
)
def test_output_self_check_rejects_leaks(tmp_path, leak):
    report, raw = p.collect(EvidenceCursor(), leak)
    with pytest.raises(p.Refused, match="privacy"):
        p.write_reports(report, "postgresql://sentinel-user:sentinel-password@sentinel-host/sentinel-db", raw, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("label", ["sentinel-db", "sentinel-host", "sentinel-user", "sentinel-db staging"])
def test_label_may_equal_connection_name_but_other_leaks_still_refuse(tmp_path, label):
    dsn = "postgresql://sentinel-user:sentinel-password@sentinel-host/sentinel-db"
    report, raw = p.collect(EvidenceCursor(), label)
    p.write_reports(report, dsn, raw, tmp_path)
    assert len(list(tmp_path.iterdir())) == 2
    for path in tmp_path.iterdir():
        path.unlink()
    report["fields"]["server_version"] = p.ok("served by sentinel-host")
    with pytest.raises(p.Refused, match="connection identifier"):
        p.write_reports(report, dsn, raw, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("label", ["sentinel%22password", "sentinel%20pass+word", "sentinel+pass%2Fword"])
def test_percent_encoded_password_in_label_is_refused(tmp_path, label):
    from urllib.parse import unquote, unquote_plus

    dsn = f"postgresql://sentinel-user:{label}@sentinel-host/sentinel-db"
    assert unquote(label) != label or unquote_plus(label) != label
    report, raw = p.collect(EvidenceCursor(), label)
    with pytest.raises(p.Refused, match="credential"):
        p.write_reports(report, dsn, raw, tmp_path)
    assert not list(tmp_path.iterdir())


def test_markdown_write_failure_leaves_no_orphan_json(tmp_path, monkeypatch):
    report, raw = p.collect(EvidenceCursor(), "staging")
    real_open = p.Path.open
    calls = []

    def flaky(self, *a, **kw):
        calls.append(self.suffix)
        if self.suffix == ".md":
            raise OSError("disk full")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(p.Path, "open", flaky)
    with pytest.raises(OSError):
        p.write_reports(report, "postgresql://sentinel-user:longpassword@sentinel-host/sentinel-db", raw, tmp_path)
    assert calls == [".json", ".md"]
    assert not list(tmp_path.iterdir())


def test_dead_connection_at_teardown_keeps_refused_exit_code(monkeypatch, tmp_path):
    cursor = SetupCursor("on", "on")
    real_execute = cursor.execute

    def execute(query, params=None):
        if query == "ROLLBACK":
            raise p.psycopg.OperationalError("connection lost")
        real_execute(query, params)

    cursor.execute = execute
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setenv("DCIM_PREFLIGHT_0057_DSN", "postgresql://sentinel-user:longpassword@sentinel-host/sentinel-db")
    monkeypatch.setattr(p.psycopg, "connect", lambda *a, **kw: connection)
    assert p.main(["--environment-label", "staging", "--output-dir", str(tmp_path)]) == 2


@pytest.mark.parametrize("password", ['sentinel"password', "sentinel\\password", "sentinel<pass|word", "sentinel%22password"])
def test_escaped_password_in_label_is_refused(tmp_path, password):
    from urllib.parse import quote

    dsn = f"postgresql://sentinel-user:{quote(password, safe='')}@sentinel-host/sentinel-db"
    report, raw = p.collect(EvidenceCursor(), password)
    with pytest.raises(p.Refused, match="credential"):
        p.write_reports(report, dsn, raw, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO x VALUES (1)",
        "UPDATE x SET y = 1",
        "DELETE FROM x",
        "DROP TABLE x",
        "CREATE TABLE x(y int)",
        "SELECT 1 INTO x",
        "SELECT 1; SELECT 2",
        "SELECT 1 -- ; DROP TABLE x",
        "SELECT 1 /* ; */",
        "BEGIN",
        "BEGIN READ WRITE",
        "SET default_transaction_read_only = off",
        "SET ROLE x",
        "COMMIT",
        "ROLLBACK; DROP TABLE x",
    ],
)
def test_rejects_unsafe_sql(statement):
    for phase in ("setup", "evidence", "teardown"):
        with pytest.raises(p.Refused):
            p.check_statement(statement, phase=phase)


def test_own_queries_and_exact_setup_only():
    for query in p.QUERIES:
        assert p.check_statement(query) == query
    for query, phase in [(s, "setup") for s in p.SETUP] + [("ROLLBACK", "teardown")]:
        assert p.check_statement(f"  {query}; \n", phase=phase) == query
        for bad in (query.lower(), query + " ; SELECT 1", query + " -- comment", query + ";;"):
            with pytest.raises(p.Refused):
                p.check_statement(bad, phase=phase)
        with pytest.raises(p.Refused):
            p.check_statement(query)
    with pytest.raises(p.Refused):
        p.check_statement("ROLLBACK", phase="setup")


class SetupCursor:
    def __init__(self, readonly="on", superuser="off"):
        self.calls = []
        self.readonly = readonly
        self.superuser = superuser

    def execute(self, query, params=None):
        self.calls.append(query)

    def fetchone(self):
        return (self.readonly if self.calls[-1] == p.READ_ONLY else self.superuser,)


def test_session_order():
    cursor = SetupCursor()
    p.setup(cursor)
    assert cursor.calls == [*p.SETUP, p.READ_ONLY, p.ROLE]


@pytest.mark.parametrize("readonly,superuser,reason", [("off", "off", "unsafe session"), ("on", "on", "superuser")])
def test_cli_refuses_before_catalog_queries(monkeypatch, tmp_path, capsys, readonly, superuser, reason):
    cursor = SetupCursor(readonly, superuser)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setenv("DCIM_PREFLIGHT_0057_DSN", "postgresql://sentinel:secret@private-host/private-db")
    monkeypatch.setattr(p.psycopg, "connect", lambda *a, **kw: connection)
    assert p.main(["--environment-label", "staging", "--output-dir", str(tmp_path)]) == 2
    assert reason in capsys.readouterr().err
    assert not any(q.startswith("SELECT") for q in cursor.calls)
    assert cursor.calls[-1] == "ROLLBACK"
    assert not list(tmp_path.iterdir())
    connection.close.assert_called_once()


def test_connection_error_is_private(monkeypatch, capsys):
    dsn = "postgresql://sentinel:secret@private-host/private-db"
    monkeypatch.setenv("DCIM_PREFLIGHT_0057_DSN", dsn)

    def fail(*a, **kw):
        raise p.psycopg.OperationalError(dsn)

    monkeypatch.setattr(p.psycopg, "connect", fail)
    assert p.main(["--environment-label", "staging"]) == 3
    assert "private-host" not in capsys.readouterr().err


async def test_ci_postgres_end_to_end(db_engine, monkeypatch, tmp_path):
    # Reuse the application's engine fixture/CI service URL, without invoking its write cleanup.
    dsn = db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("No CI PostgreSQL service available")
    connection.close()
    monkeypatch.setenv("DCIM_PREFLIGHT_0057_DSN", dsn)
    assert p.main(["--environment-label", "ci", "--output-dir", str(tmp_path)]) == 0
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert report["fields"]["alembic_version"]["status"] == "ok"
    assert report["fields"]["server_version"]["status"] == "ok"
    assert report["fields"]["migration_0057"]["value"] in ("applied", "pending")
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_cli_collects_once_and_never_commits(monkeypatch, tmp_path):
    cursor = EvidenceCursor()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(p.psycopg, "connect", connect)
    monkeypatch.setenv("DCIM_PREFLIGHT_0057_DSN", "postgresql://sentinel-user:sentinel-password@sentinel-host/sentinel-db")
    assert p.main(["--environment-label", "staging", "--output-dir", str(tmp_path)]) == 0
    connect.assert_called_once()
    assert connect.call_args.kwargs["autocommit"] is True
    connection.commit.assert_not_called()
    assert cursor.calls[:2] == list(p.SETUP)
    assert cursor.calls[-1] == "ROLLBACK"
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    scripts = ScriptDirectory(str(p.BACKEND / "migrations"))
    module = scripts.get_revision("0057_fk_indexes").module
    for table, column in module.FK_INDEXES:
        assert report["fields"][module._Target(table, column).name]["value"]["indisvalid"] is True
