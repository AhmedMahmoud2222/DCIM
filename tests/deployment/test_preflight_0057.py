"""Read-only session and SQL boundary contracts, without a database."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
p = importlib.import_module("scripts.preflight_0057.__main__")


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


class Cursor:
    def __init__(self, readonly="on", superuser="off"):
        self.calls = []
        self.readonly = readonly
        self.superuser = superuser

    def execute(self, query, params=None):
        self.calls.append(query)

    def fetchone(self):
        return (self.readonly if self.calls[-1] == p.READ_ONLY else self.superuser,)


def test_session_order():
    cursor = Cursor()
    p.setup(cursor)
    assert cursor.calls == [*p.SETUP, p.READ_ONLY, p.ROLE]


@pytest.mark.parametrize("readonly,superuser,reason", [("off", "off", "unsafe session"), ("on", "on", "superuser")])
def test_cli_refuses_before_catalog_queries(monkeypatch, tmp_path, capsys, readonly, superuser, reason):
    cursor = Cursor(readonly, superuser)
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
