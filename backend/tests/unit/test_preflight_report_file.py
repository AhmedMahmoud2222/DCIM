"""Issue #175: the SEC-ARCH-02 preflight report is confidential access-control data.

Issue #185 restores negative regression coverage for cleanup. Cleanup validates the
directory, rejects a link at the report pathname, verifies that the entry still has the
``(st_dev, st_ino)`` identity captured at creation, and only then unlinks.

Limitation, stated deliberately and not a TODO: every check here (and in the
implementation) is a pathname-based check followed by a later ``os.unlink`` of the same
pathname. The check and the unlink are not one atomic operation, so a mutation made
between the two calls cannot be excluded. These tests cover mutations performed before
cleanup is invoked, which is the window this code chooses to defend, and they do not
establish race-freedom.
"""

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

from app.application.access_preflight import _discard_partial, write_report_file

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sec_arch_02_preflight.py"
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not enforced on Windows")


def _geteuid_or_zero() -> int:
    geteuid = getattr(os, "geteuid", None)
    return 0 if geteuid is None else geteuid()


# Directory write/execute permissions are not enforced for root or on Windows.
requires_posix_non_root = pytest.mark.skipif(
    os.name != "posix" or _geteuid_or_zero() == 0,
    reason="directory permissions are not enforced on Windows or for root",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("sec_arch_02_preflight", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def _make_symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform or account")


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


def test_cleanup_refuses_a_symlink_substituted_at_the_report_path(tmp_path):
    """The link is refused even when it resolves to the exact identity cleanup owns.

    The owned file is moved aside so its inode survives; the planted link then reports
    that same ``(st_dev, st_ino)`` through ``os.stat``. Only the ``lstat`` refusal keeps
    the link in place: nothing may be unlinked through a replaced pathname.
    """
    owned = tmp_path / "owned-partial.json"
    owned.write_text("partial report", encoding="utf-8")
    identity = _identity(owned)
    moved = tmp_path / "moved.json"
    os.replace(owned, moved)
    link = tmp_path / "report.json"
    _make_symlink_or_skip(link, moved)

    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    victim_link = tmp_path / "victim-report.json"
    _make_symlink_or_skip(victim_link, victim)

    assert _identity(link) == identity
    _discard_partial(link, identity)
    assert link.is_symlink(), "cleanup unlinked a symlink instead of refusing it"
    assert moved.read_text(encoding="utf-8") == "partial report"

    _discard_partial(victim_link, identity)
    assert victim_link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"

    # Positive sensitivity: the identical identity on a regular file is still removed.
    _discard_partial(moved, identity)
    assert not moved.exists()


def test_cleanup_refuses_a_regular_file_replaced_with_a_different_identity(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("partial report", encoding="utf-8")
    created_identity = _identity(target)
    # Build the replacement beside the target so both inodes exist at once and must differ.
    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement", encoding="utf-8")
    replacement_identity = _identity(replacement)
    if created_identity == replacement_identity:
        pytest.skip("filesystem does not expose distinct inodes")

    os.replace(replacement, target)
    assert _identity(target) == replacement_identity
    _discard_partial(target, created_identity)
    assert target.exists(), "cleanup unlinked a file whose identity did not match"
    assert target.read_text(encoding="utf-8") == "replacement"

    # Positive sensitivity: the replacement's own identity is accepted for cleanup.
    _discard_partial(target, replacement_identity)
    assert not target.exists()


@posix_only
def test_cleanup_refuses_a_group_or_world_writable_directory(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    target = unsafe / "report.json"
    target.write_text("partial report", encoding="utf-8")
    sibling = unsafe / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    identity = _identity(target)
    try:
        for mode in (0o770, 0o707, 0o777):
            os.chmod(unsafe, mode)
            _discard_partial(target, identity)
            assert target.exists(), f"cleanup deleted a report in a {oct(mode)} directory"
            assert sibling.read_text(encoding="utf-8") == "keep"
    finally:
        os.chmod(unsafe, 0o700)

    # Positive sensitivity: the same cleanup succeeds once the directory is private.
    _discard_partial(target, identity)
    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_cleanup_refuses_a_symlinked_report_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    target = real / "report.json"
    target.write_text("partial report", encoding="utf-8")
    sibling = real / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    identity = _identity(target)
    linked = tmp_path / "linked"
    _make_symlink_or_skip(linked, real, directory=True)

    _discard_partial(linked / "report.json", identity)
    assert target.exists(), "cleanup followed a symlinked directory"
    assert sibling.read_text(encoding="utf-8") == "keep"

    # Positive sensitivity: the real directory is trusted and cleanup succeeds.
    _discard_partial(target, identity)
    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_writer_refuses_a_symlinked_report_directory_and_creates_nothing(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    sibling = real / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    linked = tmp_path / "linked"
    _make_symlink_or_skip(linked, real, directory=True)

    with pytest.raises(OSError):
        write_report_file(linked / "report.json", "{}\n")
    assert not (real / "report.json").exists(), "writer created a report through a symlinked directory"
    assert sorted(entry.name for entry in real.iterdir()) == ["unrelated.txt"]
    assert sibling.read_text(encoding="utf-8") == "keep"

    # Positive sensitivity: the real directory is trusted and the same writer succeeds.
    write_report_file(real / "report.json", "{}\n")
    assert (real / "report.json").read_text(encoding="utf-8") == "{}\n"


def test_cleanup_and_write_fail_closed_when_parent_is_missing_or_not_a_directory(tmp_path):
    missing = tmp_path / "missing" / "report.json"
    _discard_partial(missing, (0, 0))
    assert not missing.exists()

    not_a_directory = tmp_path / "parent-file"
    not_a_directory.write_text("keep", encoding="utf-8")
    target = not_a_directory / "report.json"
    _discard_partial(target, (0, 0))
    with pytest.raises(OSError):
        write_report_file(target, "{}\n")
    assert not_a_directory.read_text(encoding="utf-8") == "keep"

    # Positive sensitivity: a real, trusted directory still supports cleanup.
    good = tmp_path / "good"
    good.mkdir()
    written = good / "report.json"
    write_report_file(written, "{}\n")
    _discard_partial(written, _identity(written))
    assert not written.exists()


@posix_only
def test_cleanup_and_write_refuse_a_directory_owned_by_another_user(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    target.write_text("partial report", encoding="utf-8")
    identity = _identity(target)
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    real_euid = os.geteuid()
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "geteuid", lambda: real_euid + 1)
        _discard_partial(target, identity)
        with pytest.raises(OSError):
            write_report_file(tmp_path / "new.json", "{}\n")
    assert target.exists(), "cleanup deleted a report in a directory owned by another user"
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "new.json").exists()

    # Positive sensitivity: cleanup succeeds under the real effective user.
    _discard_partial(target, identity)
    assert not target.exists()


@requires_posix_non_root
def test_cleanup_and_write_fail_closed_in_a_read_only_directory(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "report.json"
    target.write_text("partial report", encoding="utf-8")
    identity = _identity(target)
    sibling = locked / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    try:
        os.chmod(locked, 0o500)
        _discard_partial(target, identity)
        with pytest.raises(OSError):
            write_report_file(locked / "new.json", "{}\n")
        assert target.exists(), "cleanup deleted a report it was not permitted to unlink"
        assert sibling.read_text(encoding="utf-8") == "keep"
        assert not (locked / "new.json").exists()
    finally:
        os.chmod(locked, 0o700)

    # Positive sensitivity: cleanup succeeds once the directory is writable again.
    _discard_partial(target, identity)
    assert not target.exists()


def test_cleanup_and_write_fail_closed_on_an_unsupported_platform(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    target.write_text("partial report", encoding="utf-8")
    identity = _identity(target)
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "name", "java")
        _discard_partial(target, identity)
        with pytest.raises(OSError):
            write_report_file(tmp_path / "new.json", "{}\n")
    assert target.exists(), "cleanup deleted a report on an unsupported platform"
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "new.json").exists()

    # Positive sensitivity: supported-platform cleanup still removes the owned file.
    _discard_partial(target, identity)
    assert not target.exists()


def test_cleanup_after_a_partial_write_removes_the_owned_report_and_keeps_siblings(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    real_fdopen = os.fdopen
    monkeypatch.setattr(
        os,
        "fdopen",
        lambda fd, *args, **kwargs: _PartialWriteHandle(real_fdopen(fd, *args, **kwargs)),
    )
    with pytest.raises(OSError, match="no space left on device"):
        write_report_file(target, "secret" * 512)
    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_cleanup_after_a_descriptor_wrap_failure_removes_the_owned_report(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")

    def broken_fdopen(fd, *args, **kwargs):
        raise OSError("cannot wrap descriptor")

    monkeypatch.setattr(os, "fdopen", broken_fdopen)
    with pytest.raises(OSError, match="cannot wrap descriptor"):
        write_report_file(target, "{}\n")
    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_cleanup_after_a_wrap_failure_refuses_a_swapped_replacement(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement", encoding="utf-8")
    replacement_identity = _identity(replacement)

    def swapping_fdopen(fd, *args, **kwargs):
        # Simulate a mutation between creation and failure-triggered cleanup. The fd is
        # closed first so the replacement is permitted on Windows as well as POSIX.
        os.close(fd)
        os.replace(replacement, target)
        raise OSError("cannot wrap descriptor")

    monkeypatch.setattr(os, "fdopen", swapping_fdopen)
    with pytest.raises(OSError, match="cannot wrap descriptor"):
        write_report_file(target, "{}\n")
    assert target.exists(), "cleanup deleted a replacement created before cleanup"
    assert target.read_text(encoding="utf-8") == "replacement"

    # Positive sensitivity: the replacement's own identity is accepted for cleanup.
    _discard_partial(target, replacement_identity)
    assert not target.exists()


class _SwappingPartialWriteHandle:
    """Fails mid-write, then swaps the pathname on close, before cleanup is called."""

    def __init__(self, handle, target, replacement):
        self._handle = handle
        self._target = target
        self._replacement = replacement

    def write(self, data):
        self._handle.write(data[: max(1, len(data) // 2)])
        raise OSError("no space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        os.replace(self._replacement, self._target)
        return False


def test_cleanup_after_a_partial_write_refuses_a_swapped_replacement(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement", encoding="utf-8")
    replacement_identity = _identity(replacement)
    real_fdopen = os.fdopen
    monkeypatch.setattr(
        os,
        "fdopen",
        lambda fd, *args, **kwargs: _SwappingPartialWriteHandle(real_fdopen(fd, *args, **kwargs), target, replacement),
    )
    with pytest.raises(OSError, match="no space left on device"):
        write_report_file(target, "secret" * 512)
    assert target.exists(), "cleanup deleted a replacement created before cleanup"
    assert target.read_text(encoding="utf-8") == "replacement"

    # Positive sensitivity: the replacement's own identity is accepted for cleanup.
    _discard_partial(target, replacement_identity)
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
