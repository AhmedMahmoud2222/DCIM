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
    enter_sandbox(
        cpu_seconds=3, address_space_bytes=512 * 1024 * 1024, require_seccomp=True,
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
        open(target, "w").write("fine")
        print("wrote", open(target).read())
    elif mode == "list_media":
        try:
            print("LISTED", os.listdir(os.environ["TEST_PROBE"]))
        except OSError:
            print("denied")
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
        os.kill(os.getpid(), signal.SIGSEGV)
    elif mode == "flood":
        while True:
            sys.stdout.write("x" * 65536)
            sys.stdout.flush()
    elif mode == "grandchild_sleep":
        subprocess.Popen(["/bin/sleep", "60"])
        print("spawned", flush=True)
        time.sleep(60)
    elif mode == "ok":
        print('{"ok": true}')


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
