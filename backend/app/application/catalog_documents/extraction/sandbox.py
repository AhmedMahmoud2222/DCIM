"""Process sandbox for everything that parses a hostile PDF or runs an OCR engine.

Layers, innermost first:

1. `apply_rlimits`: CPU time, address space, written-file size and core dumps are capped.
2. `block_python_network`: the `socket` module refuses to create sockets, so Python code (pypdf,
   Pillow) cannot open a connection whatever it is asked to do.
3. `install_seccomp_no_network`: a seccomp-bpf filter denies `socket`, `socketpair`, `connect`,
   `bind`, `listen`, `accept`, `sendto`, `sendmsg`, `sendmmsg` and `io_uring_setup` with EPERM.
   The filter is inherited across `fork` and `exec`, so the OCR engine binary, which Python's
   socket patch cannot reach, cannot create a socket either. It also denies `ptrace` and
   `process_vm_readv/writev`, so a child cannot inspect or patch its same-UID parent. `PR_SET_NO_NEW_PRIVS` makes this
   possible without privileges, and Docker's default seccomp profile permits it (a network
   namespace would need CAP_SYS_ADMIN, which the worker does not have).

4. `install_landlock`: a Landlock filesystem policy. Environment scrubbing alone is not enough: the child
   runs as the worker's UID, so it could read the parent's `/proc/<ppid>/environ` (database and signing
   secrets) or write the shared `/app/media` volume. The policy grants read and execute only on the
   interpreter, libraries, the application package directory and the OCR engine; write only on a private
   temporary directory (OCR); and nothing else. `/proc`, `/sys`, `/app/media`, `/tmp` and the application
   root are unreachable. Landlock needs no privileges and no container capability.

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
import time
from collections.abc import Callable
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
    # network: socket socketpair connect bind listen accept accept4 sendto sendmsg sendmmsg io_uring_setup;
    # process inspection: ptrace process_vm_readv process_vm_writev
    "x86_64": (0xC000003E, (41, 42, 43, 44, 46, 49, 50, 53, 288, 307, 425, 101, 310, 311), True),
    "aarch64": (0xC00000B7, (198, 199, 200, 201, 202, 203, 206, 211, 242, 269, 425, 117, 270, 271), False),
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


# ---------------------------------------------------------------------------- Landlock

_LL_CREATE_RULESET, _LL_ADD_RULE, _LL_RESTRICT_SELF = 444, 445, 446
_LL_CREATE_RULESET_VERSION = 1
_LL_RULE_PATH_BENEATH = 1
_FS_EXECUTE, _FS_WRITE_FILE, _FS_READ_FILE, _FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_FS_REMOVE_DIR, _FS_REMOVE_FILE, _FS_MAKE_DIR, _FS_MAKE_REG = 1 << 4, 1 << 5, 1 << 7, 1 << 8
_FS_ABI1_MASK = (1 << 13) - 1  # all ABI v1 filesystem rights
_FS_REFER, _FS_TRUNCATE, _FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
_FILE_RIGHTS = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV


def default_read_paths(extra: tuple[str, ...] = ()) -> list[str]:
    """Where a child may read: the interpreter, system libraries, the packages it imports and the
    application package directory (not the application root, which holds the shared media volume)."""
    import app

    candidates = [
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/etc",
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        str(Path(app.__file__).resolve().parent),
        *extra,
    ]
    for entry in sys.path:
        if entry and os.path.isdir(entry) and ("site-packages" in entry or "dist-packages" in entry):
            candidates.append(entry)
    seen: list[str] = []
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        if os.path.exists(resolved) and resolved not in seen:
            seen.append(resolved)
    return seen


def install_landlock(*, read_paths: list[str], write_dirs: tuple[str, ...] = (), device_files: tuple[str, ...] = ()) -> None:
    """Irreversible for this process and its descendants. Everything not listed becomes inaccessible
    (including `/proc`, which is how a same-UID child would read its parent's environment). Raises
    SandboxUnavailable when the kernel or the container's seccomp profile does not offer Landlock."""
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable("Landlock is only available on Linux.")
    import ctypes
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(_LL_CREATE_RULESET, None, 0, _LL_CREATE_RULESET_VERSION)
    if abi < 1:
        raise SandboxUnavailable("Landlock is not available (kernel support or container seccomp profile).")
    handled = _FS_ABI1_MASK
    if abi >= 2:
        handled |= _FS_REFER
    if abi >= 3:
        handled |= _FS_TRUNCATE
    if abi >= 5:
        handled |= _FS_IOCTL_DEV
    attribute = ctypes.create_string_buffer(struct.pack("=Q", handled), 8)
    ruleset = libc.syscall(_LL_CREATE_RULESET, attribute, 8, 0)
    if ruleset < 0:
        raise SandboxUnavailable("The Landlock ruleset could not be created.")
    try:
        read_access = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
        write_access = (
            _FS_READ_FILE
            | _FS_READ_DIR
            | _FS_WRITE_FILE
            | _FS_REMOVE_FILE
            | _FS_REMOVE_DIR
            | _FS_MAKE_DIR
            | _FS_MAKE_REG
            | _FS_TRUNCATE
        )

        def allow(path: str, access: int) -> None:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                return  # a path that does not exist grants nothing
            try:
                if not os.path.isdir(path):
                    access &= _FILE_RIGHTS
                rule = ctypes.create_string_buffer(struct.pack("=Qi", access & handled, descriptor), 12)
                if libc.syscall(_LL_ADD_RULE, ruleset, _LL_RULE_PATH_BENEATH, rule, 0) != 0:
                    raise SandboxUnavailable("A Landlock rule could not be added.")
            finally:
                os.close(descriptor)

        for path in read_paths:
            allow(path, read_access)
        for path in write_dirs:
            allow(path, write_access)
        for path in device_files:
            allow(path, _FS_READ_FILE | _FS_WRITE_FILE)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxUnavailable("PR_SET_NO_NEW_PRIVS was refused.")
        if libc.syscall(_LL_RESTRICT_SELF, ruleset, 0) != 0:
            raise SandboxUnavailable("The Landlock policy could not be enforced.")
    finally:
        os.close(ruleset)


def enter_sandbox(
    *,
    cpu_seconds: int,
    address_space_bytes: int,
    require_seccomp: bool,
    require_landlock: bool = True,
    read_paths: tuple[str, ...] = (),
    write_dirs: tuple[str, ...] = (),
) -> None:
    """Child-side entry: limits first, then the network blocks, then the filesystem policy.
    `require_seccomp=False` keeps the Python-level block only (a pure-Python parser has no other way to
    reach the network). `require_landlock=False` is for development hosts without Landlock only: it leaves
    the child able to read the worker's files and process environment."""
    apply_rlimits(cpu_seconds=cpu_seconds, address_space_bytes=address_space_bytes)
    block_python_network()
    try:
        install_seccomp_no_network()
    except SandboxUnavailable:
        if require_seccomp:
            raise
    try:
        install_landlock(
            read_paths=default_read_paths(read_paths),
            write_dirs=write_dirs,
            device_files=tuple(p for p in ("/dev/null", "/dev/urandom", "/dev/zero") if os.path.exists(p)),
        )
    except SandboxUnavailable:
        if require_landlock:
            raise


def _probe(code: str) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-c", code],
            capture_output=True,
            timeout=15,
            check=False,
            cwd=_package_root(),
            env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def seccomp_available() -> bool:
    """True when a child can install the seccomp filter here. Probed in a throwaway child because
    the filter cannot be removed from the calling process."""
    return _probe("from app.application.catalog_documents.extraction.sandbox import install_seccomp_no_network as i; i()")


def landlock_available() -> bool:
    """True when a child can enforce the Landlock filesystem policy here (same reasoning)."""
    return _probe(
        "from app.application.catalog_documents.extraction.sandbox import install_landlock as i, default_read_paths as d; "
        "i(read_paths=d())"
    )


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
    aborted: bool = False  # `tick` reported that the caller no longer owns the work


def run_sandboxed_child(
    module: str,
    args: list[str],
    stdin_bytes: bytes,
    *,
    wall_seconds: float,
    stdout_cap_bytes: int,
    extra_env: dict[str, str] | None = None,
    tick: Callable[[], bool] | None = None,
    tick_every: float = 10.0,
) -> ChildResult:
    """Runs `python -m <module> <args>` in its own session. Never raises for a misbehaving child:
    a timeout, a crash or an over-long output is reported in the result and the whole process
    group is killed. `tick` runs every `tick_every` seconds while the child works (the caller's lease
    heartbeat); when it returns False the child is killed and the result says `aborted`."""
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
    overflow = timed_out = aborted = False
    next_tick = time.monotonic() + tick_every
    deadline = time.monotonic() + wall_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            now = time.monotonic()
            if tick is not None and now >= next_tick:
                next_tick = now + tick_every
                if tick() is False:
                    aborted = True
                    break
            remaining = deadline - now
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
        if timed_out or overflow or aborted or process.poll() is None:
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
    return ChildResult(process.returncode, b"".join(chunks), timed_out, overflow, aborted)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass
