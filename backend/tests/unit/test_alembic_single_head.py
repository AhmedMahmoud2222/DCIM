"""The single-head gate passes on the real migration graph and fails on a forked one."""

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "check_alembic_single_head.py"


def _write_revision(directory: Path, revision: str, down_revision: str | None) -> None:
    (directory / f"{revision}.py").write_text(
        f'revision = "{revision}"\ndown_revision = {down_revision!r}\nbranch_labels = None\ndepends_on = None\n'
        "def upgrade() -> None:\n    pass\n\ndef downgrade() -> None:\n    pass\n"
    )


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *extra], capture_output=True, text=True, timeout=60, check=False)


@pytest.fixture
def versions(tmp_path: Path) -> Path:
    (tmp_path / "versions").mkdir()
    return tmp_path


def test_the_repository_migration_graph_has_one_head():
    result = _run()
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK: single Alembic head")


def test_a_linear_chain_passes(versions: Path):
    _write_revision(versions / "versions", "0001_a", None)
    _write_revision(versions / "versions", "0002_b", "0001_a")
    result = _run("--script-location", str(versions))
    assert result.returncode == 0, result.stderr


def test_two_revisions_on_one_parent_fail_and_are_named(versions: Path):
    _write_revision(versions / "versions", "0001_a", None)
    _write_revision(versions / "versions", "0002_user_groups", "0001_a")
    _write_revision(versions / "versions", "0002_catalog_documents", "0001_a")
    result = _run("--script-location", str(versions))
    assert result.returncode == 1
    assert "found 2" in result.stderr
    assert "0002_user_groups" in result.stderr and "0002_catalog_documents" in result.stderr


def test_an_empty_graph_fails(versions: Path):
    result = _run("--script-location", str(versions))
    assert result.returncode == 1
