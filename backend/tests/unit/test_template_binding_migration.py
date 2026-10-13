"""DDL ordering contract; real concurrent PostgreSQL proof remains required."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def migration():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0052_monitoring_template_binding.py"
    spec = importlib.util.spec_from_file_location("g9b_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("evidence", [False, True])
def test_downgrade_locks_before_guard_and_never_discards_populated_evidence(evidence):
    module = migration()
    module.op = MagicMock()
    order = []
    module.op.execute.side_effect = lambda statement: order.append(("lock", str(statement)))
    module.op.get_bind.return_value.execute.side_effect = lambda statement: (
        order.append(("guard", str(statement))) or MagicMock(scalar=lambda: evidence)
    )
    module.op.drop_column.side_effect = lambda table, column: order.append(("drop", column))
    if evidence:
        with pytest.raises(RuntimeError, match="Cannot downgrade 0052"):
            module.downgrade()
        module.op.drop_column.assert_not_called()
    else:
        module.downgrade()
        assert len(module.op.drop_column.call_args_list) == 3
    assert order[0] == ("lock", "LOCK TABLE monitoring_metric_template IN ACCESS EXCLUSIVE MODE")
    assert order[1][0] == "guard"
