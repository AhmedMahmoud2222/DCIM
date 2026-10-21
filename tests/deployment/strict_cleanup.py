"""Strict cleanup helpers for the deployment tests (strict_cleanup v4.0).

Not collected by unittest (no test_ prefix). Everything here fails loudly: a mount that survives, a
path that is not the directory the test created, or a delete that errors raises instead of passing.

Mount points come from /proc/self/mountinfo. The kernel octal-escapes only space, tab, newline and
backslash in that file; carriage return, vertical tab and form feed stay literal. So records are split
on LF alone, fields on ASCII space alone, and the file is read without newline translation.
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

MOUNTINFO = '/proc/self/mountinfo'
_ESCAPE = re.compile(r'\\([0-7]{3})')


class MountLeak(RuntimeError):
    """A mount created by the test harness could not be removed."""


class CleanupRefused(RuntimeError):
    """A removal was refused because it would not be confined to the directory the test created."""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)


def decode_mountinfo(field: str) -> str:
    """Undo the octal escapes (space \\040, tab \\011, newline \\012, backslash \\134) in one pass, so an
    escaped backslash followed by digits is not decoded twice."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def canonical(path: str) -> str:
    """Resolve symlink aliases the way the kernel reports mount points."""
    return os.path.realpath(path)


def read_mountinfo() -> str:
    # newline='' disables universal-newline translation; surrogateescape keeps undecodable bytes intact
    with open(MOUNTINFO, encoding=sys.getfilesystemencoding(), errors='surrogateescape', newline='') as handle:
        return handle.read()


def parse_mountinfo(text: str) -> list[str]:
    """Canonical, decoded mount point of every record. Records end at LF only; fields split at ' ' only."""
    points = []
    for record in text.split('\n'):
        fields = record.split(' ')
        if len(fields) > 4:
            points.append(canonical(decode_mountinfo(fields[4])))
    return points


def mount_points(text: str | None = None) -> list[str]:
    return parse_mountinfo(read_mountinfo() if text is None else text)


def mounts_at(path: str, text: str | None = None) -> list[str]:
    """Mount points equal to `path` (both canonicalized); one entry per stacked mount."""
    wanted = canonical(path)
    return [point for point in mount_points(text) if point == wanted]


def mounts_under(path: str, text: str | None = None) -> list[str]:
    """Mount points at or below `path` (both canonicalized). The separator prevents /a matching /ab."""
    wanted = canonical(path)
    prefix = wanted.rstrip('/') + '/'
    return [point for point in mount_points(text) if point == wanted or point.startswith(prefix)]


def remove_unmounted_tree(path: str, rmtree=shutil.rmtree) -> None:
    """Delete a tree that may once have held a mount, when no creation-time identity is available.
    Refuses a symlink, refuses while any mount is at or below it, never hides a delete error."""
    if os.path.islink(path):
        raise CleanupRefused(f'refusing to delete {path}: it is a symlink')
    live = mounts_under(path)
    if live:
        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')
    rmtree(path)


def identity(path: str) -> tuple[int, int]:
    """(st_dev, st_ino) of a directory, read without following a symlink."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise CleanupRefused(f'{path} is not a real directory')
    return (info.st_dev, info.st_ino)


def check_identity(path: str, expected: tuple[int, int]) -> None:
    """Raise CleanupRefused unless `path` is, right now, the directory with the recorded identity.
    Opens with O_NOFOLLOW and compares fstat of the descriptor, so a symlink, a renamed original or a
    replacement at the same pathname is refused. Call it before any operation that acts on the path."""
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError as error:
        raise CleanupRefused(f'refusing to act on {path}: it cannot be opened as the directory that was created '
                             f'({error.strerror})') from error
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if (info.st_dev, info.st_ino) != tuple(expected):
        raise CleanupRefused(f'refusing to act on {path}: it is not the directory that was created (identity changed)')


class BoundaryUnavailable(CleanupRefused):
    """The kernel facility that makes recursive deletion safe (openat2 with RESOLVE_NO_XDEV, statx mount ids)
    is missing, forbidden or unsupported here. Deletion is refused; there is no pathname fallback."""


_RESOLVE = 0x01 | 0x02 | 0x04          # RESOLVE_NO_XDEV | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS
_SYS_OPENAT2 = 437                      # the same number on x86_64 and arm64
_SYS_ARCH_OK = ('x86_64', 'aarch64', 'arm64')


class _OpenHow(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64), ('resolve', ctypes.c_uint64)]


class _Timespec(ctypes.Structure):
    _fields_ = [('sec', ctypes.c_int64), ('nsec', ctypes.c_uint32), ('pad', ctypes.c_int32)]


class _Statx(ctypes.Structure):
    _fields_ = [('mask', ctypes.c_uint32), ('blksize', ctypes.c_uint32), ('attributes', ctypes.c_uint64),
                ('nlink', ctypes.c_uint32), ('uid', ctypes.c_uint32), ('gid', ctypes.c_uint32),
                ('mode', ctypes.c_uint16), ('pad', ctypes.c_uint16), ('ino', ctypes.c_uint64),
                ('size', ctypes.c_uint64), ('blocks', ctypes.c_uint64), ('attributes_mask', ctypes.c_uint64),
                ('atime', _Timespec), ('btime', _Timespec), ('ctime', _Timespec), ('mtime', _Timespec),
                ('rdev_major', ctypes.c_uint32), ('rdev_minor', ctypes.c_uint32),
                ('dev_major', ctypes.c_uint32), ('dev_minor', ctypes.c_uint32),
                ('mnt_id', ctypes.c_uint64), ('rest', ctypes.c_uint64 * 13)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _open_below(dir_fd: int, name: str, flags: int) -> int:
    """openat2(dir_fd, name) that cannot cross a mount point (including a bind mount of the same filesystem,
    which an st_dev comparison does not see), follow a symlink or follow a magic link. EXDEV means the name is
    a mount point or leads into one."""
    import platform
    if platform.machine() not in _SYS_ARCH_OK:
        raise BoundaryUnavailable(f'openat2 is not wired up for {platform.machine()}; refusing to delete')
    how = _OpenHow(flags | os.O_CLOEXEC | os.O_NOFOLLOW, 0, _RESOLVE)
    fd = _libc().syscall(_SYS_OPENAT2, dir_fd, os.fsencode(name), ctypes.byref(how), ctypes.sizeof(how))
    if fd >= 0:
        return fd
    code = ctypes.get_errno()
    if code == errno.EXDEV:
        raise MountLeak(f'refusing to delete through {name!r}: it is, or leads into, a mount point')
    if code in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):
        raise BoundaryUnavailable(f'openat2(RESOLVE_NO_XDEV) is unavailable here ({os.strerror(code)}); refusing to delete')
    raise OSError(code, os.strerror(code), name)


def mount_id(fd: int) -> int:
    """The kernel's unique id of the mount that holds the object `fd` refers to (statx STATX_MNT_ID)."""
    libc = _libc()
    if not hasattr(libc, 'statx'):
        raise BoundaryUnavailable('statx is unavailable; refusing to delete')
    buffer = _Statx()
    if libc.statx(fd, b'', 0x1000, 0x1000, ctypes.byref(buffer)) != 0:       # AT_EMPTY_PATH, STATX_MNT_ID
        code = ctypes.get_errno()
        raise BoundaryUnavailable(f'statx failed ({os.strerror(code)}); refusing to delete')
    if not buffer.mask & 0x1000:
        raise BoundaryUnavailable('this kernel does not report mount ids (statx STATX_MNT_ID); refusing to delete')
    return buffer.mnt_id


def boundary_available() -> bool:
    """True when the primitives the deletion boundary needs work on this machine."""
    try:
        fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.close(_open_below(fd, '.', os.O_PATH))
            mount_id(fd)
        finally:
            os.close(fd)
    except (CleanupRefused, OSError):
        return False
    return True


def _children(dir_fd: int, here: str, root_mount: int, hook=None) -> None:
    """Delete everything under an already-open directory, relative to its descriptor.

    Every entry is opened with openat2(RESOLVE_NO_XDEV | NO_SYMLINKS | NO_MAGICLINKS) from the directory
    descriptor and its mount id is compared with the worktree's, so the walk cannot enter another mount
    (bind mounts of the same filesystem included), whatever happens to any pathname meanwhile. Nothing here
    consults a mount table or a pathname. A mount point is refused, never unmounted. `hook(stage, name)` is
    a test seam."""
    hook = hook or (lambda stage, name: None)
    with os.scandir(dir_fd) as scan:
        names = [entry.name for entry in scan]
    hook('listed', here)
    for name in names:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        hook('before-open', name)
        below = os.path.join(here, name)
        if not stat.S_ISDIR(info.st_mode):
            handle = _open_below(dir_fd, name, os.O_PATH)                 # a file mount point is refused too
            try:
                if mount_id(handle) != root_mount:
                    raise MountLeak(f'refusing to delete {below}: it is on another mount')
            finally:
                os.close(handle)
            os.unlink(name, dir_fd=dir_fd)
            continue
        child = _open_below(dir_fd, name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            opened = os.fstat(child)
            if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
                raise CleanupRefused(f'{below} changed while it was being deleted')
            if mount_id(child) != root_mount:
                raise MountLeak(f'refusing to delete {below}: it is on another mount')
            _children(child, below, root_mount, hook)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)


def remove_verified(path: str, expected: tuple[int, int], hook=None) -> None:
    """Delete `path` only if it is still the directory that was created (same st_dev and st_ino), without
    ever leaving that directory's mount.

    The directory is opened with O_NOFOLLOW and checked with fstat on the descriptor, so a rename-and-
    replace at the same pathname, a symlink swap or a swapped ancestor is refused. It must not itself be a
    mount point (its parent must be reachable without crossing a mount). Everything below is removed
    relative to the descriptor through openat2(RESOLVE_NO_XDEV), checked again by statx mount id. If those
    kernel facilities are unavailable the deletion is refused: there is no fallback to pathnames.
    Works as root or as the owner; failures raise.
    """
    refusal = f'refusing to delete {path}: it is not the directory that was created (identity changed, or a symlink)'
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            raise CleanupRefused(refusal) from error
        raise
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != tuple(expected):
            raise CleanupRefused(refusal)
        root_mount = mount_id(fd)
        try:
            os.close(_open_below(fd, '..', os.O_RDONLY | os.O_DIRECTORY))     # fails with EXDEV at a mount root
        except MountLeak as error:
            raise MountLeak(f'refusing to delete {path}: the directory is itself a mount point') from error
        _children(fd, canonical(path), root_mount, hook)
    finally:
        os.close(fd)
    parent = os.open(os.path.dirname(os.path.abspath(path)) or '.', _DIR_FLAGS)
    try:
        name = os.path.basename(os.path.abspath(path))
        again = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (again.st_dev, again.st_ino) != tuple(expected):
            raise CleanupRefused(refusal)
        os.rmdir(name, dir_fd=parent)
    finally:
        os.close(parent)


def temp_dir(case, prefix: str, parent: str | None = None) -> str:
    """Create a temporary directory, record its identity, and register its strict removal in the same
    step, before anything that can fail. A cleanup error is reported next to the test's own outcome."""
    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    case.addCleanup(remove_verified, path, identity(path))
    return path


def class_temp_dir(cls, prefix: str, parent: str | None = None) -> str:
    """temp_dir() for setUpClass: registered with addClassCleanup, which unittest runs even when a later
    line of setUpClass raises (tearDownClass would not run in that case)."""
    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    cls.addClassCleanup(remove_verified, path, identity(path))
    return path


def remove_privileged_tree(path: str, priv: list[str] | None, expected: tuple[int, int]) -> None:
    """Delete a tree that root populated, verifying its identity. The deletion itself runs in a root
    helper process (this file, `rm-verified`) that applies remove_verified, so it is descriptor-relative
    and cannot be redirected by a swapped path. Failures raise with the helper's message."""
    if os.path.islink(path):
        raise CleanupRefused(f'refusing to delete {path}: it is a symlink')
    done = subprocess.run([*(priv or []), sys.executable, '-I', os.path.abspath(__file__), 'rm-verified',
                           path, str(expected[0]), str(expected[1])], capture_output=True, text=True)
    if done.returncode == 3:
        raise CleanupRefused(done.stderr.strip() or f'refusing to delete {path}')
    if done.returncode == 4:
        raise MountLeak(done.stderr.strip() or f'refusing to delete {path}: mounted')
    if done.returncode == 6:
        raise BoundaryUnavailable(done.stderr.strip() or 'the deletion boundary is unavailable')
    if done.returncode != 0 or os.path.lexists(path):
        raise OSError(f'could not remove {path}: {done.stderr.strip() or "it still exists"}')


def privileged_temp_dir(case, prefix: str, priv: list[str] | None) -> str:
    """mktemp -d as root; its identity is recorded and the verified privileged removal registered at once."""
    made = subprocess.run([*(priv or []), 'mktemp', '-d', os.path.join(tempfile.gettempdir(), f'{prefix}XXXXXX')], capture_output=True, text=True, check=True)
    path = made.stdout.strip()
    case.addCleanup(remove_privileged_tree, path, priv, identity(path))
    return path


def _main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == 'rm-verified':
        try:
            remove_verified(argv[2], (int(argv[3]), int(argv[4])))
        except BoundaryUnavailable as error:
            print(error, file=sys.stderr)
            return 6
        except CleanupRefused as error:
            print(error, file=sys.stderr)
            return 3
        except MountLeak as error:
            print(error, file=sys.stderr)
            return 4
        except OSError as error:
            print(f'{argv[2]}: {error}', file=sys.stderr)
            return 5
        return 0
    print('usage: strict_cleanup.py rm-verified PATH DEV INO', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
