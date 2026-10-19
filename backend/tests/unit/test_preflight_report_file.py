"""Issue #175: the SEC-ARCH-02 preflight report is confidential access-control data."""

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

from app.application.access_preflight import write_report_file

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sec_arch_02_preflight.py"
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not enforced on Windows")


def _load_script():
    spec = importlib.util.spec_from_file_location("sec_arch_02_preflight", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@posix_only
def test_report_is_created_owner_only_even_under_a_permissive_umask(tmp_path):
    target = tmp_path / "report.json"
    previous = os.umask(0)
    try:
        write_report_file(target, "{}\n")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_existing_report_is_never_overwritten(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_report_file(target, "new")
    assert target.read_text(encoding="utf-8") == "keep"


def test_symlink_target_is_refused_and_not_written_through(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link = tmp_path / "report.json"
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(FileExistsError):
        write_report_file(link, "secret")
    assert victim.read_text(encoding="utf-8") == "keep"


def test_cli_failure_prints_only_the_exception_class(monkeypatch, capsys, tmp_path):
    module = _load_script()

    async def boom():
        raise RuntimeError("postgresql://user:hunter2@db/prod")

    monkeypatch.setattr(module, "report", boom)
    monkeypatch.setattr(sys, "argv", ["sec_arch_02_preflight.py", "--output", str(tmp_path / "r.json")])
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "preflight failed: RuntimeError"
    assert "hunter2" not in captured.out + captured.err
    assert not (tmp_path / "r.json").exists()


def test_stdout_mode_warns_on_stderr_and_keeps_report_on_stdout(monkeypatch, capsys):
    module = _load_script()

    async def fake():
        return {"read_only": True}

    monkeypatch.setattr(module, "report", fake)
    monkeypatch.setattr(sys, "argv", ["sec_arch_02_preflight.py"])
    assert module.main() == 0
    captured = capsys.readouterr()
    assert "confidential" in captured.err
    assert '"read_only": true' in captured.out
