"""Issue #175: the SEC-ARCH-02 preflight report is confidential access-control data."""

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

from app.application.access_preflight import _discard_partial, write_report_file

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


class _PartialWriteHandle:
    """Real handle that flushes half the payload to the file, then fails like a full disk."""

    def __init__(self, handle):
        self._handle = handle

    def write(self, data):
        self._handle.write(data[: max(1, len(data) // 2)])
        raise OSError("no space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False


def test_partial_write_failure_leaves_no_partial_report(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    real_fdopen = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda fd, *args, **kwargs: _PartialWriteHandle(real_fdopen(fd, *args, **kwargs)))
    with pytest.raises(OSError, match="no space left on device"):
        write_report_file(target, "secret" * 512)
    assert not target.exists()


def test_descriptor_wrap_failure_removes_the_created_file(tmp_path, monkeypatch):
    target = tmp_path / "report.json"

    def broken_fdopen(fd, *args, **kwargs):
        raise OSError("cannot wrap descriptor")

    monkeypatch.setattr(os, "fdopen", broken_fdopen)
    with pytest.raises(OSError, match="cannot wrap descriptor"):
        write_report_file(target, "{}\n")
    assert not target.exists()


def test_cleanup_never_unlinks_a_path_swapped_for_another_file(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("partial report", encoding="utf-8")
    stale = os.stat(target)
    stale_identity = (stale.st_dev, stale.st_ino)
    # Build the replacement beside the target so both inodes exist at once and must differ.
    # Unlinking first lets the filesystem hand the freed inode to the new file, which would
    # make the swap indistinguishable from the original and defeat the guard under test.
    decoy = tmp_path / "decoy.json"
    decoy.write_text("unrelated file created meanwhile", encoding="utf-8")
    decoy_stat = os.stat(decoy)
    if (decoy_stat.st_dev, decoy_stat.st_ino) == stale_identity:
        pytest.skip("filesystem does not expose distinct inodes")
    os.replace(decoy, target)
    _discard_partial(target, stale_identity)
    assert target.read_text(encoding="utf-8") == "unrelated file created meanwhile"
    current = os.stat(target)
    _discard_partial(target, (current.st_dev, current.st_ino))
    assert not target.exists()


def test_report_filename_is_gitignored():
    root = Path(__file__).resolve().parents[3]
    patterns = {line.strip() for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "sec-arch-02-preflight*.json" in patterns


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


def test_cli_write_failure_is_sanitized_and_leaves_no_partial_report(monkeypatch, capsys, tmp_path):
    """Filesystem failures during --output print only the class name and clean up."""
    module = _load_script()
    target = tmp_path / "report.json"

    async def fake():
        return {"read_only": True}

    def broken_fdopen(fd, *args, **kwargs):
        raise PermissionError("denied: C:\\Users\\operator\\secret\\path")

    monkeypatch.setattr(module, "report", fake)
    monkeypatch.setattr(os, "fdopen", broken_fdopen)
    monkeypatch.setattr(sys, "argv", ["sec_arch_02_preflight.py", "--output", str(target)])
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "preflight failed: PermissionError"
    assert "secret" not in captured.err
    assert not target.exists()
