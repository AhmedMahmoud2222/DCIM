"""Strict cleanup helpers for the deployment tests (strict_cleanup v1.0).

Not collected by unittest (no test_ prefix). Everything here fails loudly: a mount that survives, a
path that is not the directory the test created, or a delete that errors raises instead of passing.

Mount points come from /proc/self/mountinfo. The kernel octal-escapes only space, tab, newline and
backslash in that file; carriage return, vertical tab and form feed stay literal. So records are split
on LF alone, fields on ASCII space alone, and the file is read without newline translation.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

MOUNTINFO = '/proc/self/mountinfo'
_ESCAPE = re.compile(r'\\([0-7]{3})')


class MountLeak(RuntimeError):
    """A mount created by the test harness could not be removed."""


class CleanupRefused(RuntimeError):
    """A removal was refused because it would not be confined to the directory the test created."""


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
    """Delete a temporary tree that may once have held a mount. Refuses a symlink, refuses while any
    mount is at or below it, and never hides a delete error."""
    if os.path.islink(path):
        raise CleanupRefused(f'refusing to delete {path}: it is a symlink')
    live = mounts_under(path)
    if live:
        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')
    rmtree(path)


def remove_registered(path: str, expected: str) -> None:
    """Remove `path` only if it still resolves to the directory that was created. A path swapped for a
    symlink to somewhere else is refused, so cleanup cannot leave the disposable tree."""
    if canonical(path) != expected or os.path.islink(path):
        raise CleanupRefused(f'refusing to delete {path}: it no longer resolves to {expected}')
    remove_unmounted_tree(path)


def temp_dir(case, prefix: str, parent: str | None = None) -> str:
    """Create a temporary directory and register its strict removal in the same step, before anything
    that can fail. A cleanup error is reported next to whatever the test itself did."""
    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    case.addCleanup(remove_registered, path, canonical(path))
    return path


def remove_privileged_tree(path: str, priv: list[str] | None) -> None:
    """Strict removal of a tree that root populated. Same mount and symlink refusals, then `rm -rf`
    confined to one filesystem, whose failure or leftovers raise."""
    if os.path.islink(path):
        raise CleanupRefused(f'refusing to delete {path}: it is a symlink')
    live = mounts_under(path)
    if live:
        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')
    done = subprocess.run([*(priv or []), 'rm', '-rf', '--one-file-system', path], capture_output=True, text=True)
    if done.returncode != 0 or os.path.lexists(path):
        raise OSError(f'could not remove {path}: {done.stderr.strip() or "it still exists"}')


def privileged_temp_dir(case, prefix: str, priv: list[str] | None) -> str:
    """mktemp -d as root, with strict privileged removal registered immediately."""
    made = subprocess.run([*(priv or []), 'mktemp', '-d', f'/tmp/{prefix}XXXXXX'], capture_output=True, text=True, check=True)
    path = made.stdout.strip()
    case.addCleanup(remove_privileged_tree, path, priv)
    return path
