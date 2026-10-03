"""The sandbox that every PDF-touching child runs in: network denial (Python level and syscall
level, inherited by exec'd binaries), resource limits, timeouts, crashes and runaway output."""

import errno
import platform
import subprocess
import sys
import time

import pytest

from app.application.catalog_documents.extraction import sandbox
from app.application.catalog_documents.extraction.sandbox import landlock_available, run_sandboxed_child, seccomp_available

CHILD = "tests.unit._sandbox_child"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox")


def child(mode: str, *, wall: float = 20, cap: int = 1_000_000, env: dict[str, str] | None = None, **kwargs):
    return run_sandboxed_child(CHILD, [mode], b"", wall_seconds=wall, stdout_cap_bytes=cap, extra_env=env, **kwargs)


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


# ------------------------------------------------------------------------------ filesystem policy (Landlock)


def test_landlock_is_available_where_the_tests_run():
    assert landlock_available(), "CI and the worker image must allow the unprivileged Landlock policy"


@pytest.fixture
def victim():
    """A same-UID process holding a secret in its environment, like the worker or its Celery master. A plain
    system binary on purpose: the interpreter running the tests may carry file capabilities (CI grants
    cap_net_raw), which makes its own /proc entries unreadable and would hide the very thing under test."""
    import subprocess

    process = subprocess.Popen(["/bin/sleep", "60"], env={"DEMO_SECRET": "s3cret"})  # noqa: S603
    time.sleep(0.2)
    yield {"TEST_VICTIM_PID": str(process.pid)}
    process.kill()
    process.wait()


def test_control_without_the_policy_a_child_reads_another_same_uid_process_environment(victim):
    """The premise of the finding: same UID, so /proc/<pid>/environ is readable unless the policy stops it."""
    assert child("victim_environ_without_policy", env=victim).stdout.decode().strip() == "READ secret-visible"


def test_the_policy_makes_other_processes_environments_unreachable(victim):
    assert child("read_victim_environ", env=victim).stdout.decode().strip() == "denied PermissionError"
    assert child("read_proc_self").stdout.decode().strip() == "denied"


def test_the_policy_blocks_reading_and_writing_everything_it_does_not_grant(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    stored = media / "datasheet.pdf"
    stored.write_text("original bytes")
    env = {"TEST_PROBE": str(stored)}
    assert child("read_outside", env=env).stdout.decode().strip() == "denied"  # the shared media volume
    assert child("write_outside", env=env).stdout.decode().strip() == "denied"
    assert stored.read_text() == "original bytes"
    assert child("list_media", env={"TEST_PROBE": str(media)}).stdout.decode().strip() == "denied"
    assert child("write_outside", env={"TEST_PROBE": str(tmp_path / "new.txt")}).stdout.decode().strip() == "denied"
    assert not (tmp_path / "new.txt").exists()


def test_the_only_writable_place_is_the_granted_scratch_directory(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = child("write_workdir", env={"TEST_WORKDIR": str(scratch)})
    assert result.stdout.decode().strip() == "wrote fine" and (scratch / "ok.txt").read_text() == "fine"
    outside = tmp_path / "elsewhere.txt"
    assert child("write_outside", env={"TEST_WORKDIR": str(scratch), "TEST_PROBE": str(outside)}).stdout.decode().strip() == "denied"


def test_the_libraries_the_workers_import_after_the_sandbox_still_load():
    assert child("imports_still_work").stdout.decode().strip() == "imported"


def test_a_child_cannot_attach_to_or_read_the_memory_of_its_parent():
    assert child("ptrace_parent").stdout.decode().strip() == f"blocked {errno.EPERM}"
    assert child("process_vm_readv").stdout.decode().strip() == f"blocked {errno.EPERM}"


def test_landlock_failure_is_fail_closed_unless_explicitly_waived(monkeypatch):
    def unavailable(**_kwargs):
        raise sandbox.SandboxUnavailable("no landlock")

    monkeypatch.setattr(sandbox, "install_landlock", unavailable)
    monkeypatch.setattr(sandbox, "apply_rlimits", lambda **_k: None)
    monkeypatch.setattr(sandbox, "block_python_network", lambda: None)
    monkeypatch.setattr(sandbox, "install_seccomp_no_network", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.enter_sandbox(cpu_seconds=1, address_space_bytes=1 << 30, require_seccomp=False)
    sandbox.enter_sandbox(cpu_seconds=1, address_space_bytes=1 << 30, require_seccomp=False, require_landlock=False)


# ------------------------------------------------------------------------------ lease heartbeat while a child runs


def test_the_heartbeat_runs_repeatedly_while_a_long_child_works():
    ticks: list[float] = []
    started = time.monotonic()
    result = child("sleep", wall=2.6, tick=lambda: ticks.append(time.monotonic() - started) or True, tick_every=0.5)
    assert result.timed_out and not result.aborted
    assert len(ticks) >= 3, "the lease must be renewed during a stage, not only between stages"


def test_a_lost_claim_stops_the_child_at_once():
    started = time.monotonic()
    result = child("sleep", wall=30, tick=lambda: False, tick_every=0.3)
    assert result.aborted and not result.timed_out and time.monotonic() - started < 10
