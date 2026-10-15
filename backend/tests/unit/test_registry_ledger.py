"""Registry code/DB agreement and independently frozen migration literals."""

import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.application.registry_ledger import validate_registry_rows, verify_registry_ledger
from app.domain.telemetry.registry import REGISTRIES, snapshot_text


def rows():
    return [(version, snapshot_text(snapshot), snapshot.content_hash) for version, snapshot in REGISTRIES.items()]


def test_exact_code_database_contract_agreement():
    validate_registry_rows(rows())


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "content", "hash", "self_consistent_drift"])
def test_mismatch_is_fail_closed(case):
    altered = rows()
    if case == "missing":
        altered.pop()
    elif case == "extra":
        altered.append(("future", altered[0][1], altered[0][2]))
    elif case == "duplicate":
        altered.append(altered[0])
    elif case == "content":
        altered[0] = ("1", "changed", altered[0][2])
    elif case == "hash":
        altered[0] = ("1", altered[0][1], "0" * 64)
    else:
        altered[0] = ("1", "changed", hashlib.sha256(b"changed").hexdigest())
    with pytest.raises(RuntimeError):
        validate_registry_rows(altered)


def test_migration_literals_are_frozen_independently_and_match_runtime():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0053_registry_version_snapshot.py"
    spec = importlib.util.spec_from_file_location("frozen_registry_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert list(module.FROZEN_SNAPSHOTS) == rows()
    assert "app.domain.telemetry" not in path.read_text()


async def test_startup_query_is_read_only_and_missing_contract_blocks_startup():
    queries = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query):
            queries.append(str(query))
            return []

    class Engine:
        def connect(self):
            return Connection()

    with pytest.raises(RuntimeError, match="disagree"):
        await verify_registry_ledger(Engine())
    assert queries == ["SELECT version, canonical_text, content_hash FROM registry_version_snapshot ORDER BY version"]


@pytest.mark.parametrize("failure", [None, RuntimeError("missing or mismatched ledger")])
async def test_api_lifespan_gate_is_registered_and_failure_prevents_serving(monkeypatch, failure):
    from app import main

    validator = AsyncMock(side_effect=failure)
    monkeypatch.setattr(main, "verify_registry_ledger", validator)
    entered = False
    if failure is None:
        async with main.app.router.lifespan_context(main.app):
            entered = True
    else:
        with pytest.raises(RuntimeError, match="missing or mismatched"):
            async with main.app.router.lifespan_context(main.app):
                entered = True
    assert entered is (failure is None)
    validator.assert_awaited_once_with(main.engine)
