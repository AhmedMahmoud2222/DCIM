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
    enter_sandbox(cpu_seconds=3, address_space_bytes=512 * 1024 * 1024, require_seccomp=True)
    if mode == "socket_python":
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


if __name__ == "__main__":
    main()
