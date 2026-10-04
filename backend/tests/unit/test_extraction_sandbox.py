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

    # Its own session: if the sandbox ever regressed, killpg would hit this process only, not the test runner.
    process = subprocess.Popen(["/bin/sleep", "60"], env={"DEMO_SECRET": "s3cret"}, start_new_session=True)  # noqa: S603
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
    assert (
        child("write_outside", env={"TEST_WORKDIR": str(scratch), "TEST_PROBE": str(outside)}).stdout.decode().strip() == "denied"
    )


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


# ------------------------------------------------------------------------------ signals and file metadata (review round 2)


def test_control_without_the_sandbox_a_child_can_signal_and_mutate_other_files_and_processes(victim, tmp_path):
    target = tmp_path / "stored.pdf"
    target.write_text("original bytes")
    result = child("control_signal_and_mutate", env={**victim, "TEST_PROBE": str(target)})
    assert result.stdout.decode().strip() == "control ok", "the premise of the finding does not hold here"
    assert target.read_text() == ""  # truncated: the damage the sandbox must prevent


def test_a_sandboxed_child_cannot_signal_any_other_process(victim):
    import os as _os

    lines = child("kill_victim", env=victim).stdout.decode().splitlines()
    assert lines == [f"kill blocked {errno.EPERM}", f"kill_all blocked {errno.EPERM}", f"killpg blocked {errno.EPERM}"], lines
    _os.kill(int(victim["TEST_VICTIM_PID"]), 0)  # still alive: nothing reached it


def test_raw_signal_pidfd_and_prlimit_syscalls_are_denied_too(victim):
    lines = child("raw_syscalls", env=victim).stdout.decode().splitlines()
    assert len(lines) == 6 and all(line.endswith(f"blocked {errno.EPERM}") for line in lines), lines


def test_file_metadata_and_truncation_are_refused_even_on_files_the_user_owns(tmp_path):
    target = tmp_path / "media" / "stored.pdf"
    target.parent.mkdir()
    target.write_text("original bytes")
    target.chmod(0o640)
    before = (target.stat().st_mode, target.stat().st_mtime)
    lines = child("mutate_metadata", env={"TEST_PROBE": str(target)}).stdout.decode().splitlines()
    verdicts = {line.split()[0]: line for line in lines}
    for operation in ("chmod", "chown", "utime", "setxattr", "removexattr", "truncate"):
        assert verdicts[operation] == f"{operation} blocked {errno.EPERM}", verdicts
    assert verdicts["unchanged"] == "unchanged True"
    assert target.read_text() == "original bytes" and (target.stat().st_mode, target.stat().st_mtime) == before


def test_truncation_is_refused_without_landlock_abi_3_because_seccomp_stands_alone(tmp_path):
    target = tmp_path / "stored.pdf"
    target.write_text("original bytes")
    result = child("truncate_older_abi", env={"TEST_PROBE": str(target)})
    assert result.stdout.decode().strip() == f"blocked {errno.EPERM}" and target.read_text() == "original bytes"


# ------------------------------------------------------------------------------ truncating opens, groups, scheduling (round 3)


def test_control_a_read_only_open_with_o_trunc_truncates_under_landlock_abi_2_alone(tmp_path):
    target = tmp_path / "stored.pdf"
    target.write_text("original bytes")
    result = child("control_open_trunc", env={"TEST_PROBE": str(target)})
    assert result.stdout.decode().strip() == "control truncated" and target.read_text() == ""


def test_every_truncating_open_form_is_denied_and_the_file_survives(tmp_path):
    target = tmp_path / "stored.pdf"
    target.write_text("original bytes")
    lines = child("open_trunc", env={"TEST_PROBE": str(target)}).stdout.decode().splitlines()
    verdicts = {line.split()[0]: line for line in lines}
    for label in ("openat", "openat_rw", "creat", "open_raw", "openat_raw", "openat2_raw"):
        assert verdicts[label].startswith(f"{label} blocked"), verdicts
    assert verdicts["plain_read"] == "plain_read ok", "an ordinary read-only open must keep working"
    assert target.read_text() == "original bytes"


def test_a_truncating_open_is_refused_by_seccomp_alone_when_landlock_has_no_truncate_right(tmp_path):
    target = tmp_path / "stored.pdf"
    target.write_text("original bytes")
    result = child("open_trunc_older_abi", env={"TEST_PROBE": str(target)})
    assert result.stdout.decode().strip() == f"blocked {errno.EPERM}" and target.read_text() == "original bytes"


def test_control_a_forked_descendant_can_leave_the_process_group_without_the_sandbox():
    assert child("control_escape_group").stdout.decode().strip() == "control escaped"


def test_a_descendant_cannot_leave_the_process_group_the_supervisor_kills():
    lines = child("escape_group").stdout.decode().splitlines()
    assert lines == [f"setsid blocked {errno.EPERM}", f"setpgid blocked {errno.EPERM}"], lines


def _nice(pid: int) -> int:
    import os as _os

    return _os.getpriority(_os.PRIO_PROCESS, pid)


def _sched_env(victim: dict[str, str]) -> dict[str, str]:
    machine = platform.machine()
    return {
        **victim,
        "TEST_NR_IOPRIO": "251" if machine == "x86_64" else "30",
        "TEST_NR_SCHED_SETATTR": "314" if machine == "x86_64" else "274",
    }


def test_control_without_the_sandbox_a_child_can_renice_another_process(victim):
    pid = int(victim["TEST_VICTIM_PID"])
    before = _nice(pid)
    assert child("control_sched", env=victim).stdout.decode().strip() == "control niceness 15"
    assert _nice(pid) == 15 != before


def test_a_sandboxed_child_cannot_change_the_scheduling_of_another_process(victim):
    pid = int(victim["TEST_VICTIM_PID"])
    before = _nice(pid)
    lines = child("sched_mutations", env=_sched_env(victim)).stdout.decode().splitlines()
    verdicts = {line.split()[0]: line for line in lines}
    for label in ("setpriority", "sched_setaffinity", "sched_setscheduler", "ioprio_set", "sched_setattr"):
        assert verdicts[label] == f"{label} blocked {errno.EPERM}", verdicts
    assert _nice(pid) == before


def test_shared_kernel_state_and_namespace_syscalls_are_denied_raw():
    index = 0 if platform.machine() == "x86_64" else 1
    numbers = [
        str(pair[index])
        for group in ("shared kernel state", "process control")
        for name, pair in sandbox._DENIED[group].items()
        if pair[index] is not None and name not in {"ioprio_set", "sched_setattr"}
    ]
    lines = child("raw_numbers", env={"TEST_NUMBERS": ",".join(numbers)}).stdout.decode().splitlines()
    assert len(lines) == len(numbers) and all(line.endswith("blocked") for line in lines), lines


def test_every_denied_syscall_group_is_present_for_both_architectures():
    for index, machine in enumerate(("x86_64", "aarch64")):
        numbers = set(sandbox._denied_numbers(index))
        for group, calls in sandbox._DENIED.items():
            expected = {pair[index] for pair in calls.values() if pair[index] is not None}
            assert expected and expected <= numbers, (machine, group)
        assert len(sandbox.build_seccomp_program(machine)) // 8 < 256


def test_the_supervisor_kills_helpers_a_normally_exiting_child_leaves_behind():
    import subprocess

    seconds = str(3000 + int(time.time()) % 997)  # a sleep length nothing else on the host uses
    result = child("leave_helper", env={"TEST_SLEEP_SECONDS": seconds})
    assert result.returncode == 0 and result.stdout.decode().strip() == "exiting"
    time.sleep(0.3)
    listing = subprocess.run(["ps", "-eo", "stat,args"], capture_output=True, text=True).stdout.splitlines()
    alive = [line for line in listing if f"sleep {seconds}" in line and not line.startswith("Z")]
    assert not alive, "the helper outlived its child"
