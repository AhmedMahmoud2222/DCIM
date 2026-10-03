"""The sandbox that every PDF-touching child runs in: network denial (Python level and syscall
level, inherited by exec'd binaries), resource limits, timeouts, crashes and runaway output."""

import errno
import platform
import subprocess
import sys
import time

import pytest

from app.application.catalog_documents.extraction import sandbox
from app.application.catalog_documents.extraction.sandbox import run_sandboxed_child, seccomp_available

CHILD = "tests.unit._sandbox_child"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox")


def child(mode: str, *, wall: float = 20, cap: int = 1_000_000):
    return run_sandboxed_child(CHILD, [mode], b"", wall_seconds=wall, stdout_cap_bytes=cap)


def test_seccomp_is_available_where_the_tests_run():
    assert seccomp_available(), "CI and the worker image must allow the unprivileged seccomp filter"


def test_python_level_socket_creation_is_blocked():
    assert child("socket_python").stdout.decode().strip() == "blocked OSError"


def test_a_raw_socket_syscall_is_denied_with_eperm():
    assert child("socket_raw_syscall").stdout.decode().strip() == f"blocked {errno.EPERM}"


def test_the_filter_is_inherited_by_an_exec_ed_grandchild():
    """This is what covers the OCR engine binary, which Python's socket patch cannot reach."""
    assert child("socket_grandchild").stdout.decode().strip() == f"blocked {errno.EPERM}"


def test_the_child_environment_carries_no_secrets(monkeypatch):
    for name in ("DATABASE_URL", "JWT_SECRET_KEY", "REDIS_URL", "CREDENTIAL_ENCRYPTION_KEY"):
        monkeypatch.setenv(name, "secret-value")
    assert child("env").stdout.decode().strip() == "[]"


def test_a_cpu_spin_is_killed_by_the_rlimit():
    started = time.monotonic()
    result = child("cpu_spin", wall=30)
    assert time.monotonic() - started < 15
    assert not result.timed_out and result.returncode is not None and result.returncode != 0


def test_a_wall_clock_timeout_kills_the_child():
    started = time.monotonic()
    result = child("sleep", wall=1.5)
    assert result.timed_out and time.monotonic() - started < 10


def test_a_memory_bomb_is_stopped_by_the_address_space_limit():
    result = child("memory", wall=30)
    assert not result.timed_out and result.returncode != 0


def test_a_crashing_child_is_reported_not_raised():
    result = child("crash")
    assert result.returncode is not None and result.returncode < 0 and result.stdout == b""


def test_runaway_output_is_cut_off_at_the_cap():
    result = child("flood", wall=20, cap=200_000)
    assert result.output_overflow and len(result.stdout) <= 200_000


def test_a_timeout_kills_the_whole_process_group_including_grandchildren():
    result = run_sandboxed_child(CHILD, ["grandchild_sleep"], b"", wall_seconds=2, stdout_cap_bytes=1000)
    assert result.timed_out
    time.sleep(0.5)
    listing = subprocess.run(["ps", "-eo", "pid,stat,args"], capture_output=True, text=True).stdout.splitlines()
    alive = [line for line in listing if "sleep 60" in line and line.split(None, 2)[1][0] != "Z" and "ps -eo" not in line]
    assert not alive, "grandchild survived the timeout"


def test_a_normal_child_returns_its_output():
    result = child("ok")
    assert result.returncode == 0 and result.stdout.strip() == b'{"ok": true}'


def test_the_filter_program_has_the_expected_shape_and_fails_closed_on_unknown_machines():
    program = sandbox.build_seccomp_program(platform.machine())
    assert len(program) % 8 == 0 and len(program) >= 8 * 8
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.build_seccomp_program("sparc64")
