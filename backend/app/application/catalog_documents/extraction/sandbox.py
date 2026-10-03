"""Process sandbox for everything that parses a hostile PDF or runs an OCR engine.

Layers, innermost first:

1. `apply_rlimits`: CPU time, address space, written-file size and core dumps are capped.
2. `block_python_network`: the `socket` module refuses to create sockets, so Python code (pypdf,
   Pillow) cannot open a connection whatever it is asked to do.
3. `install_seccomp_no_network`: a seccomp-bpf filter denies `socket`, `socketpair`, `connect`,
   `bind`, `listen`, `accept`, `sendto`, `sendmsg`, `sendmmsg` and `io_uring_setup` with EPERM.
   The filter is inherited across `fork` and `exec`, so the OCR engine binary, which Python's
   socket patch cannot reach, cannot create a socket either. `PR_SET_NO_NEW_PRIVS` makes this
   possible without privileges, and Docker's default seccomp profile permits it (a network
   namespace would need CAP_SYS_ADMIN, which the worker does not have).

`run_sandboxed_child` is the parent side: fixed argv, a scrubbed environment (no database,
Redis or signing secrets reach the child), its own process group so a timeout kills grandchildren,
a bounded stdout read and a wall-clock limit."""

import os
import platform
import selectors
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# seccomp-bpf constants (linux/seccomp.h, linux/filter.h, linux/audit.h)
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JGE_K = 0x35
_BPF_RET_K = 0x06
_X32_SYSCALL_BIT = 0x40000000

# machine -> (AUDIT_ARCH, denied syscall numbers, deny-all-x32-bit-calls)
_ARCHES: dict[str, tuple[int, tuple[int, ...], bool]] = {
    "x86_64": (0xC000003E, (41, 42, 43, 44, 46, 49, 50, 53, 288, 307, 425), True),
    "aarch64": (0xC00000B7, (198, 199, 200, 201, 202, 203, 206, 211, 242, 269, 425), False),
}


class SandboxUnavailable(RuntimeError):
    """The requested isolation cannot be established on this platform. Callers fail closed."""


def _bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    import struct

    return struct.pack("=HBBI", code, jt, jf, k)


def build_seccomp_program(machine: str | None = None) -> bytes:
    """Returns the raw `sock_filter` array. Exposed so a test can inspect it."""
    audit_arch, denied, deny_x32 = _ARCHES.get(machine or platform.machine(), (0, (), False))
    if not audit_arch:
        raise SandboxUnavailable(f"No seccomp network filter is defined for {machine or platform.machine()}.")
    instructions: list[tuple[int, int, int, int]] = []
    instructions.append((_BPF_LD_W_ABS, 0, 0, 4))  # arch
    instructions.append((_BPF_JMP_JEQ_K, 1, 0, audit_arch))  # arch ok -> skip the kill
    instructions.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS))
    instructions.append((_BPF_LD_W_ABS, 0, 0, 0))  # syscall number
    checks: list[tuple[int, int]] = [(_BPF_JMP_JEQ_K, number) for number in denied]
    if deny_x32:
        checks.insert(0, (_BPF_JMP_JGE_K, _X32_SYSCALL_BIT))
    # layout after the checks: [ALLOW][DENY]; a hit at index i jumps over (remaining checks + ALLOW).
    for index, (code, value) in enumerate(checks):
        remaining = len(checks) - index - 1
        instructions.append((code, remaining + 1, 0, value))
    instructions.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    instructions.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))
    return b"".join(_bpf(*instruction) for instruction in instructions)


def install_seccomp_no_network() -> None:
    """Irreversible for this process and its descendants. Raises SandboxUnavailable."""
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable("seccomp is only available on Linux.")
    import ctypes

    program = build_seccomp_program()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxUnavailable("PR_SET_NO_NEW_PRIVS was refused.")

    class _Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    buffer = ctypes.create_string_buffer(program, len(program))
    fprog = _Fprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise SandboxUnavailable("The seccomp network filter was refused.")


def block_python_network() -> None:
    import socket

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network access is disabled in this process")

    class _BlockedSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            _refuse()

    socket.socket = _BlockedSocket  # type: ignore[misc, assignment]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]
    socket.gethostbyname = _refuse  # type: ignore[assignment]


def apply_rlimits(*, cpu_seconds: int, address_space_bytes: int, file_size_bytes: int = 64 * 1024 * 1024) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (address_space_bytes, address_space_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_bytes, file_size_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):  # pragma: no cover - non-POSIX platforms
        pass


def enter_sandbox(*, cpu_seconds: int, address_space_bytes: int, require_seccomp: bool) -> None:
    """Child-side entry: limits first, then the network blocks. `require_seccomp=False` keeps the
    Python-level block only (a pure-Python parser has no other way to reach the network)."""
    apply_rlimits(cpu_seconds=cpu_seconds, address_space_bytes=address_space_bytes)
    block_python_network()
    try:
        install_seccomp_no_network()
    except SandboxUnavailable:
        if require_seccomp:
            raise


def seccomp_available() -> bool:
    """True when a child can install the seccomp filter here. Probed in a throwaway child because
    the filter cannot be removed from the calling process."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-c",
                "from app.application.catalog_documents.extraction.sandbox import install_seccomp_no_network as i; i()",
            ],
            capture_output=True,
            timeout=15,
            check=False,
            cwd=_package_root(),
            env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _package_root() -> str:
    import app

    return str(Path(app.__file__).resolve().parents[1])


def _child_env() -> dict[str, str]:
    """Nothing from the parent environment except what Python and the OCR engine need."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "OMP_THREAD_LIMIT": "1",
    }
    return env


@dataclass(frozen=True)
class ChildResult:
    returncode: int | None
    stdout: bytes
    timed_out: bool
    output_overflow: bool


def run_sandboxed_child(
    module: str,
    args: list[str],
    stdin_bytes: bytes,
    *,
    wall_seconds: float,
    stdout_cap_bytes: int,
    extra_env: dict[str, str] | None = None,
) -> ChildResult:
    """Runs `python -m <module> <args>` in its own session. Never raises for a misbehaving child:
    a timeout, a crash or an over-long output is reported in the result and the whole process
    group is killed."""
    env = {**_child_env(), **(extra_env or {})}
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, arguments are numbers chosen by the caller
        [sys.executable, "-m", module, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=_package_root(),
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None

    def _feed() -> None:
        try:
            process.stdin.write(stdin_bytes)  # type: ignore[union-attr]
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()

    chunks: list[bytes] = []
    received = 0
    overflow = timed_out = False
    import time

    deadline = time.monotonic() + wall_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not selector.select(timeout=min(remaining, 0.5)):
                if process.poll() is not None and not selector.select(timeout=0):
                    break
                continue
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                break
            received += len(data)
            if received > stdout_cap_bytes:
                overflow = True
                break
            chunks.append(data)
    finally:
        selector.close()
        if timed_out or overflow or process.poll() is None:
            _kill_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot be ignored
            pass
        try:
            process.stdout.close()
        except OSError:
            pass
        feeder.join(timeout=1)
    return ChildResult(process.returncode, b"".join(chunks), timed_out, overflow)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass
