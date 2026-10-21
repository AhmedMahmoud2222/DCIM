"""DDL contract tests; populated PostgreSQL rehearsal remains a release gate."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


def migration():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0055_alarm_rule_resolution.py"
    assert path.exists(), "G8 additive migration is missing"
    spec = importlib.util.spec_from_file_location("g8_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolution_migration_follows_aggregate_lineage_as_single_head():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    directory = ScriptDirectory.from_config(config)
    # Issue #150 B7: 0055_pdu_catalog_outlet_template follows 0055_alarm_rule_resolution;
    # the vertical-placement uniqueness migration (0056) follows the PDU migration, so
    # 0056_pdu_vertical_placement is the single head.
    assert directory.get_heads() == ["0056_pdu_vertical_placement"]
    assert directory.get_revision("0055_alarm_rule_resolution").down_revision == "0051_aggregate_lineage"
    assert directory.get_revision("0055_pdu_catalog_outlet_template").down_revision == "0055_alarm_rule_resolution"
    assert directory.get_revision("0056_pdu_vertical_placement").down_revision == "0055_pdu_catalog_outlet_template"


def test_upgrade_is_nullable_without_backfill():
    module = migration()
    module.op = MagicMock()
    module.upgrade()
    columns = [call.args[1] for call in module.op.add_column.call_args_list]
    assert {column.name for column in columns} == {"unit_resolved_by", "unit_resolved_at", "unit_resolution_reason", "blocked_notified_at"}
    assert all(column.nullable and column.default is None and column.server_default is None for column in columns)
    module.op.execute.assert_not_called()


def test_populated_downgrade_refuses_before_dropping_evidence():
    module = migration()
    module.op = MagicMock()
    module.op.get_bind.return_value.scalar.return_value = True
    with pytest.raises(RuntimeError, match="resolution evidence"):
        module.downgrade()
    module.op.drop_column.assert_not_called()
    module.op.drop_constraint.assert_not_called()
    assert "ACCESS EXCLUSIVE" in str(module.op.execute.call_args.args[0])


def test_empty_downgrade_drops_columns_after_guard():
    module = migration()
    module.op = MagicMock()
    module.op.get_bind.return_value.scalar.return_value = False
    module.downgrade()
    assert len(module.op.drop_column.call_args_list) == 4
