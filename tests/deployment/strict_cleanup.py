"""Strict cleanup helpers for the deployment tests (strict_cleanup v2.0).

Not collected by unittest (no test_ prefix). Everything here fails loudly: a mount that survives, a
path that is not the directory the test created, or a delete that errors raises instead of passing.

Mount points come from /proc/self/mountinfo. The kernel octal-escapes only space, tab, newline and
backslash in that file; carriage return, vertical tab and form feed stay literal. So records are split
on LF alone, fields on ASCII space alone, and the file is read without newline translation.
"""
from __future__ import annotations

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


def _children(dir_fd: int, here: str, root_dev: int, mounted: set[str]) -> None:
    """Delete everything under an already-open directory, relative to its descriptor. Symlinks are
    unlinked, never followed; a mount point or a directory on another device stops the walk."""
    with os.scandir(dir_fd) as scan:
        names = [entry.name for entry in scan]
    for name in names:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            continue
        below = os.path.join(here, name)
        if info.st_dev != root_dev or below in mounted:
            raise MountLeak(f'refusing to delete through {below}: it is a mount point')
        child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
        try:
            if os.fstat(child).st_ino != info.st_ino:
                raise CleanupRefused(f'{below} changed while it was being deleted')
            _children(child, below, root_dev, mounted)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)


def remove_verified(path: str, expected: tuple[int, int]) -> None:
    """Delete `path` only if it is still the directory that was created (same st_dev and st_ino).

    The directory is opened with O_NOFOLLOW and checked with fstat on the descriptor, so a rename-and-
    replace at the same pathname, a symlink swap, or a swapped ancestor is refused, and everything below
    is removed relative to that descriptor. No live mount at or below the path is ever deleted through.
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
        live = mounts_under(path)
        if live:
            raise MountLeak(f'refusing to delete {path}: still mounted at {live}')
        _children(fd, canonical(path), info.st_dev, set(mount_points()))
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
    live = mounts_under(path)
    if live:
        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')
    done = subprocess.run([*(priv or []), sys.executable, '-I', os.path.abspath(__file__), 'rm-verified',
                           path, str(expected[0]), str(expected[1])], capture_output=True, text=True)
    if done.returncode == 3:
        raise CleanupRefused(done.stderr.strip() or f'refusing to delete {path}')
    if done.returncode == 4:
        raise MountLeak(done.stderr.strip() or f'refusing to delete {path}: mounted')
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
