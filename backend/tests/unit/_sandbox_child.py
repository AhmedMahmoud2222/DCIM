"""Child process used by the sandbox tests. `argv[1]` selects the behaviour; it enters the sandbox the
same way the production workers do."""

import os
import signal
import subprocess
import sys
import time

from app.application.catalog_documents.extraction.sandbox import enter_sandbox


def main() -> None:
    mode = sys.argv[1]
    workdir = os.environ.get("TEST_WORKDIR")
    if mode == "victim_environ_without_policy":  # control: the same read, with the filesystem policy left out
        return _read_victim_environ()
    if mode == "control_signal_and_mutate":  # control: the same calls, with no sandbox at all
        target = os.environ["TEST_PROBE"]
        os.chmod(target, 0o600)
        os.utime(target, (1, 1))
        os.truncate(target, 0)
        os.kill(int(os.environ["TEST_VICTIM_PID"]), signal.SIGTERM)
        print("control ok")
        return None
    if mode == "control_open_trunc":  # control: Landlock like ABI 2 alone; a read-only O_TRUNC open still truncates
        from app.application.catalog_documents.extraction.sandbox import default_read_paths, install_landlock

        install_landlock(read_paths=[*default_read_paths(), os.path.dirname(os.environ["TEST_PROBE"])], abi_limit=2)
        os.close(os.open(os.environ["TEST_PROBE"], os.O_RDONLY | os.O_TRUNC))
        print("control truncated")
        return None
    if mode == "open_trunc_older_abi":  # Landlock like ABI 2 plus the seccomp filter only: the filter must stand alone
        from app.application.catalog_documents.extraction.sandbox import (
            default_read_paths,
            install_landlock,
            install_seccomp_no_network,
        )

        install_landlock(read_paths=[*default_read_paths(), os.path.dirname(os.environ["TEST_PROBE"])], abi_limit=2)
        install_seccomp_no_network()
        try:
            os.close(os.open(os.environ["TEST_PROBE"], os.O_RDONLY | os.O_TRUNC))
            print("TRUNCATED")
        except OSError as exc:
            print("blocked", exc.errno)
        return None
    if mode == "control_escape_group":  # control: no sandbox, so a forked descendant can leave the process group
        print("control", _forked_verdict(os.setsid))
        return None
    if mode == "control_sched":  # control: no sandbox, so a same-UID child can renice another process
        pid = int(os.environ["TEST_VICTIM_PID"])
        os.setpriority(os.PRIO_PROCESS, pid, 15)
        print("control niceness", os.getpriority(os.PRIO_PROCESS, pid))
        return None
    enter_sandbox(
        cpu_seconds=3,
        address_space_bytes=512 * 1024 * 1024,
        require_seccomp=True,
        write_dirs=(workdir,) if workdir else (),
    )
    if mode == "read_victim_environ":
        _read_victim_environ()
    elif mode == "read_proc_self":
        try:
            open("/proc/self/environ").read()
            print("READ")
        except OSError:
            print("denied")
    elif mode == "read_outside":
        try:
            print("READ", open(os.environ["TEST_PROBE"]).read())
        except OSError:
            print("denied")
    elif mode == "write_outside":
        try:
            open(os.environ["TEST_PROBE"], "w").write("tampered")
            print("WROTE")
        except OSError:
            print("denied")
    elif mode == "write_workdir":
        target = os.path.join(workdir or "", "ok.txt")
        open(target, "x").write("fine")  # "w" would pass O_TRUNC, which the filter denies
        print("wrote", open(target).read())
    elif mode == "list_media":
        try:
            print("LISTED", os.listdir(os.environ["TEST_PROBE"]))
        except OSError:
            print("denied")
    elif mode == "kill_victim":
        pid = int(os.environ["TEST_VICTIM_PID"])
        for label, call in (
            ("kill", lambda: os.kill(pid, signal.SIGTERM)),
            ("kill_all", lambda: os.kill(-1, 0)),
            ("killpg", lambda: os.killpg(os.getpgid(pid), signal.SIGTERM)),
        ):
            try:
                call()
                print(label, "ALLOWED")
            except OSError as exc:
                print(label, "blocked", exc.errno)
    elif mode == "raw_syscalls":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pid = int(os.environ["TEST_VICTIM_PID"])
        arch = os.uname().machine
        numbers = {  # x86_64, aarch64
            "tgkill": (234, 131),
            "tkill": (200, 130),
            "rt_sigqueueinfo": (129, 138),
            "pidfd_send_signal": (424, 424),
            "pidfd_open": (434, 434),
            "prlimit64": (302, 261),
        }
        for name, (x86, arm) in numbers.items():
            number = x86 if arch == "x86_64" else arm
            result = libc.syscall(number, pid, pid, 0, 0, 0, 0)
            print(name, "blocked" if result < 0 else "ALLOWED", ctypes.get_errno())
    elif mode == "mutate_metadata":
        target = os.environ["TEST_PROBE"]
        before = os.stat(target)
        attempts = {
            "chmod": lambda: os.chmod(target, 0o000),
            "chown": lambda: os.chown(target, os.getuid(), os.getgid()),
            "utime": lambda: os.utime(target, (0, 0)),
            "setxattr": lambda: os.setxattr(target, "user.dcim", b"x"),
            "removexattr": lambda: os.removexattr(target, "user.dcim"),
            "truncate": lambda: os.truncate(target, 0),
        }
        for label, call in attempts.items():
            try:
                call()
                print(label, "ALLOWED")
            except OSError as exc:
                print(label, "blocked", exc.errno)
        after = os.stat(target)
        print("unchanged", (before.st_mode, before.st_mtime, before.st_size) == (after.st_mode, after.st_mtime, after.st_size))
    elif mode == "truncate_older_abi":
        # Landlock behaving like ABI 2 (no truncate right): the seccomp layer alone must still refuse.
        from app.application.catalog_documents.extraction.sandbox import default_read_paths, install_landlock

        install_landlock(read_paths=default_read_paths(), abi_limit=2)
        try:
            os.truncate(os.environ["TEST_PROBE"], 0)
            print("TRUNCATED")
        except OSError as exc:
            print("blocked", exc.errno)
    elif mode == "open_trunc":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        target = os.environ["TEST_PROBE"].encode()
        o_trunc = os.O_TRUNC
        for label, call in (
            ("openat", lambda: os.open(target, os.O_RDONLY | o_trunc)),
            ("openat_rw", lambda: os.open(target, os.O_RDWR | o_trunc)),
            ("creat", lambda: libc.creat(target, 0o600)),
            ("open_raw", lambda: libc.syscall(2, target, os.O_RDONLY | o_trunc, 0)),
            ("openat_raw", lambda: libc.syscall(257, -100, target, os.O_RDONLY | o_trunc, 0)),
            ("openat2_raw", lambda: libc.syscall(437, -100, target, 0, 0)),
        ):
            try:
                result = call()
                print(label, "OPENED" if result is None or result >= 0 else f"blocked {ctypes.get_errno()}")
            except OSError as exc:
                print(label, "blocked", exc.errno)
        print("plain_read", "ok" if open(os.__file__, "rb").read() else "empty")
    elif mode == "escape_group":
        print("setsid", _forked_verdict(os.setsid))
        print("setpgid", _forked_verdict(lambda: os.setpgid(0, 0)))
    elif mode == "sched_mutations":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pid = int(os.environ["TEST_VICTIM_PID"])
        attr = ctypes.create_string_buffer(64)
        calls = (
            ("setpriority", lambda: os.setpriority(os.PRIO_PROCESS, pid, 15)),
            ("sched_setaffinity", lambda: os.sched_setaffinity(pid, {0})),
            ("sched_setscheduler", lambda: os.sched_setscheduler(pid, os.SCHED_BATCH, os.sched_param(0))),
            ("ioprio_set", lambda: _raw(libc, int(os.environ["TEST_NR_IOPRIO"]), 1, pid, (3 << 13))),
            ("sched_setattr", lambda: _raw(libc, int(os.environ["TEST_NR_SCHED_SETATTR"]), pid, attr, 0)),
        )
        for label, call in calls:
            try:
                call()
                print(label, "CHANGED")
            except OSError as exc:
                print(label, "blocked", exc.errno)
        print("niceness", os.getpriority(os.PRIO_PROCESS, pid))
    elif mode == "raw_numbers":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        for number in os.environ["TEST_NUMBERS"].split(","):
            result = libc.syscall(int(number), 0, 0, 0, 0, 0, 0)
            print(number, "blocked" if result < 0 and ctypes.get_errno() == 1 else f"ALLOWED {result}")
    elif mode == "ptrace_parent":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.ptrace(16, os.getppid(), 0, 0)  # PTRACE_ATTACH
        print("blocked" if result < 0 else "ATTACHED", ctypes.get_errno())
    elif mode == "process_vm_readv":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(310, os.getppid(), 0, 0, 0, 0, 0)
        print("blocked" if result < 0 else "ALLOWED", ctypes.get_errno())
    elif mode == "imports_still_work":
        from PIL import Image  # noqa: F401
        from pypdf import PdfReader  # noqa: F401

        print("imported")
    elif mode == "socket_python":
        import socket

        try:
            socket.socket()
            print("ALLOWED")
        except OSError as exc:
            print("blocked", type(exc).__name__)
    elif mode == "socket_raw_syscall":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.socket(2, 1, 0)  # AF_INET, SOCK_STREAM: bypasses Python's socket module entirely
        print("blocked" if fd < 0 else "ALLOWED", ctypes.get_errno())
    elif mode == "socket_grandchild":
        code = (
            "import ctypes;l=ctypes.CDLL(None,use_errno=True);fd=l.socket(2,1,0);"
            "print('blocked' if fd<0 else 'ALLOWED',ctypes.get_errno())"
        )
        print(subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True).stdout.strip())
    elif mode == "env":
        print(sorted(k for k in os.environ if k in {"DATABASE_URL", "JWT_SECRET_KEY", "REDIS_URL", "CREDENTIAL_ENCRYPTION_KEY"}))
    elif mode == "cpu_spin":
        while True:
            pass
    elif mode == "sleep":
        time.sleep(60)
    elif mode == "memory":
        blocks = []
        while True:
            blocks.append(bytearray(64 * 1024 * 1024))
    elif mode == "crash":
        import ctypes

        ctypes.string_at(0)  # a real segmentation fault: a sandboxed child cannot signal itself
    elif mode == "flood":
        while True:
            sys.stdout.write("x" * 65536)
            sys.stdout.flush()
    elif mode == "leave_helper":  # exits normally but leaves a helper process behind, like an OCR engine would
        subprocess.Popen(["/bin/sleep", os.environ["TEST_SLEEP_SECONDS"]])
        print("exiting", flush=True)
    elif mode == "grandchild_sleep":
        subprocess.Popen(["/bin/sleep", "60"])
        print("spawned", flush=True)
        time.sleep(60)
    elif mode == "ok":
        print('{"ok": true}')


def _raw(libc, number: int, *args) -> None:
    import ctypes

    if libc.syscall(number, *args) < 0:
        raise OSError(ctypes.get_errno(), "raw syscall refused")


def _forked_verdict(operation) -> str:
    """Runs `operation` in a forked descendant (the group leader cannot setsid) and reports how it ended."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        try:
            operation()
            verdict = "escaped"
        except OSError as exc:
            verdict = f"blocked {exc.errno}"
        os.write(write_end, verdict.encode())
        os._exit(0)
    os.close(write_end)
    verdict = os.read(read_end, 100).decode()
    os.waitpid(pid, 0)
    return verdict


def _read_victim_environ() -> None:
    """Reads the environment of another same-UID process (the stand-in for the worker or its Celery
    master, which hold the database and signing secrets)."""
    try:
        content = open(f"/proc/{os.environ['TEST_VICTIM_PID']}/environ").read()
        print("READ", "secret-visible" if "DEMO_SECRET=s3cret" in content else "secret-missing")
    except OSError as exc:
        print("denied", type(exc).__name__)


if __name__ == "__main__":
    main()
