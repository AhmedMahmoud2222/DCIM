"""Contract tests for the persisted alarm subject-key format migration."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


def migration():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0058_alarm_subject_key_node_metric.py"
    spec = importlib.util.spec_from_file_location("alarm_subject_key_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_strips_only_the_row_integration_prefix():
    module = migration()
    module.op = MagicMock()

    module.upgrade()

    statement = str(module.op.execute.call_args.args[0])
    assert "substring(subject_key FROM char_length(integration_id::text) + 2)" in statement
    assert "subject_key LIKE integration_id::text || ':%'" in statement


def test_downgrade_restores_the_integration_prefix():
    module = migration()
    module.op = MagicMock()

    module.downgrade()

    statement = str(module.op.execute.call_args.args[0])
    assert "integration_id::text || ':' || subject_key" in statement
    assert "WHERE" not in statement.upper()
