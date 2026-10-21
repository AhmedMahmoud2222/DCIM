#!/usr/bin/env python3
"""Filesystem isolation for the sandboxed agent (fsisolate v2.0).

Review layout (all of it controlled by root, so the agent user cannot rename, replace,
delete or symlink anything on the path to the reviewed source):

    /srv/ocreview            root:root 0755   <- parent, not writable by the agent
    /srv/ocreview/src        root:root tree, dirs 0555 / files 0444 (0555 if executable),
                             optionally bind-mounted read-only over itself

The agent's writable area is a separate tree (its HOME). Subcommands:

  build SRC ROOT            copy SRC to ROOT/src as a root-owned read-only tree (run as root)
  mount ROOT                read-only bind mount of ROOT/src over itself (best effort, root)
  chain PATH [UID]          verify every component from / to PATH is a real directory owned
                            by root (or UID for the last one) and not group/world writable
  digest PATH               print a sha256 digest of names, modes, owners and contents
  probe-parent PATH [--forbidden P ...] [--writable P ...]
                            run AS THE AGENT UID against a directory the agent legitimately writes
                            into (the implementation work tree): its parent must not let the agent
                            rename, replace, delete or symlink the work tree itself
  probe PATH [--forbidden P ...] [--writable P ...]
                            run AS THE AGENT UID: the kernel must report no write, rename, delete or
                            link permission over PATH or its ancestors; reads must work; forbidden
                            paths must not be readable. Exit 1 on any miss.

Both probes only ask the kernel for permissions (access(2), effective IDs) and read metadata.
They never modify, rename or delete anything under the real trees. The destructive variants
(destructive_probe*) refuse to run unless the tree sits next to a .fsisolate-disposable marker,
and exist so tests can confirm the permission verdicts against real operations on a fixture.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile

DENIED = {errno.EACCES, errno.EPERM, errno.EROFS}


class IsolationError(Exception):
    pass


def build(src: str, root: str) -> str:
    """Create root-owned read-only ROOT/src from SRC. Must run as root."""
    if os.geteuid() != 0:
        raise IsolationError('build must run as root')
    os.makedirs(root, exist_ok=True)
    os.chown(root, 0, 0)
    os.chmod(root, 0o755)
    dest = os.path.join(root, 'src')
    if os.path.lexists(dest):
        raise IsolationError('destination already exists')
    shutil.copytree(src, dest, symlinks=False)
    for base, dirs, files in os.walk(dest):
        for name in dirs:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise IsolationError('symlink in source tree')
            os.chown(path, 0, 0)
            os.chmod(path, 0o555)
        for name in files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise IsolationError('symlink in source tree')
            executable = os.stat(path).st_mode & 0o111
            os.chown(path, 0, 0)
            os.chmod(path, 0o555 if executable else 0o444)
    os.chown(dest, 0, 0)
    os.chmod(dest, 0o555)
    return dest


def mount_readonly(root: str, run=None) -> bool:
    """Read-only bind mount of ROOT/src over itself. True only when both steps succeeded.

    A bind that succeeded followed by a failed read-only remount leaves a writable self-bind
    behind, so that mount is removed before returning False. If it cannot be removed the
    function raises: a leftover mount must never be reported as "unavailable".
    `run` takes an argv list and returns an object with .returncode (default: subprocess).
    """
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True))
    target = os.path.join(root, 'src')
    try:
        if run(['mount', '--bind', target, target]).returncode != 0:
            return False                           # no mount was created
    except OSError:
        return False                               # no mount binary or no permission to use it
    try:
        if run(['mount', '-o', 'remount,ro,bind', target]).returncode == 0:
            return True
    except OSError:
        pass
    for cmd in (['umount', target], ['umount', '-l', target]):
        try:
            if run(cmd).returncode == 0:
                return False
        except OSError:
            continue
    raise IsolationError(f'bind mount at {target} could not be made read-only and could not be removed')


def chain_problems(path: str, final_uid: int = 0) -> list[str]:
    problems = []
    parts = []
    current = os.path.abspath(path)
    while True:
        parts.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for index, component in enumerate(reversed(parts)):
        info = os.lstat(component)
        last = component == parts[0]
        if stat.S_ISLNK(info.st_mode):
            problems.append(f'{component} is a symlink')
        elif not stat.S_ISDIR(info.st_mode):
            problems.append(f'{component} is not a directory')
        owner = final_uid if last else 0
        if info.st_uid != owner:
            problems.append(f'{component} owned by uid {info.st_uid}, expected {owner}')
        if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and component in ('/tmp',)):
            problems.append(f'{component} is group/world writable')
    return problems


def digest(path: str) -> str:
    h = hashlib.sha256()
    for base, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(dirs + files):
            full = os.path.join(base, name)
            info = os.lstat(full)
            h.update(f'{os.path.relpath(full, path)}\0{info.st_mode:o}\0{info.st_uid}\0{info.st_gid}\0'.encode())
            if stat.S_ISLNK(info.st_mode):
                h.update(os.readlink(full).encode())
            elif stat.S_ISREG(info.st_mode):
                with open(full, 'rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        h.update(chunk)
    return h.hexdigest()


def _denied(operation) -> tuple[bool, str]:
    """True when the operation raised a permission-class error (success = test failure)."""
    try:
        operation()
    except OSError as error:
        return (error.errno in DENIED), f'errno {errno.errorcode.get(error.errno, error.errno)}'
    return False, 'operation SUCCEEDED'


FIXTURE_MARKER = '.fsisolate-disposable'


def require_disposable(path: str) -> None:
    """Destructive probes may run only inside a fixture that a test created for them."""
    marker = os.path.join(os.path.dirname(os.path.abspath(path)), FIXTURE_MARKER)
    if not os.path.isfile(marker):
        raise IsolationError(f'refusing destructive probe: {path} is not marked as a disposable fixture')


def _can(path: str, mode: int) -> bool:
    """Kernel permission verdict for the effective UID. Reads metadata only; never modifies anything."""
    return os.access(path, mode, effective_ids=True, follow_symlinks=False)


def _owner_can_chmod(info: os.stat_result) -> bool:
    """An owner can chmod its own entry (and setfacl it), so an entry the agent owns is never read-only to it."""
    return os.geteuid() != 0 and info.st_uid == os.geteuid()


def _removable(parent: str, name: str) -> bool:
    """Could this UID rename, replace, delete or symlink-over the entry `name` in `parent`?"""
    if not (_can(parent, os.W_OK) and _can(parent, os.X_OK)):
        return False
    info = os.lstat(parent)
    if info.st_mode & stat.S_ISVTX:
        entry = os.lstat(os.path.join(parent, name))
        return os.geteuid() in (0, info.st_uid, entry.st_uid)
    return True


def _ancestor_exposure(path: str) -> list[str]:
    """Every ancestor entry on the way to `path` that this UID could rename or replace."""
    exposed = []
    current = os.path.abspath(path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return exposed
        if _removable(parent, os.path.basename(current)) or _owner_can_chmod(os.lstat(parent)):
            exposed.append(current)
        current = parent


def _tree_exposure(path: str) -> tuple[list[str], list[str], list[str], list[str], int, str | None]:
    """Walk the tree without touching it. Returns (writable dirs, writable files and symlinks,
    entries the agent owns, entries not owned by root, entries checked, a sample file).
    Hard-linked files are reported with the writable files: a second name could sit anywhere."""
    bad_dirs, bad_files, agent_owned, not_root, count, sample = [], [], [], [], 0, None

    def examine(full, info):
        if _owner_can_chmod(info):
            agent_owned.append(full)
        elif info.st_uid != 0:
            not_root.append(full)

    examine(path, os.lstat(path))
    for base, dirs, files in os.walk(path, followlinks=False):
        if _can(base, os.W_OK):
            bad_dirs.append(base)
        for name in dirs + files:
            count += 1
            full = os.path.join(base, name)
            info = os.lstat(full)
            examine(full, info)
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                bad_files.append(full + ' (symlink)')
            elif stat.S_ISREG(info.st_mode):
                sample = sample or full
                if info.st_nlink > 1:
                    bad_files.append(full + ' (hard-linked)')
                elif _can(full, os.W_OK):
                    bad_files.append(full)
    return bad_dirs, bad_files, agent_owned, not_root, count, sample


def _verdict(results, name, ok, detail):
    results.append((name, ok, detail))


def _forbidden_and_writable(results, forbidden, writable):
    for target in forbidden:
        ok, detail = _denied(lambda t=target: os.listdir(t))
        results.append((f'forbidden path unreadable: {target}', ok, detail))
    for target in writable:
        ok = _can(target, os.W_OK | os.X_OK)
        results.append((f'agent area writable: {target}', ok, 'access check'))


def probe(path: str, forbidden: list[str], writable: list[str]) -> list[tuple[str, bool, str]]:
    """Non-destructive isolation check for a tree the agent may only read (the reviewer source).

    Runs as the agent UID. It asks the kernel what this UID may do (access(2) with effective IDs,
    which honours ownership, mode bits, ACLs and read-only mounts) and never writes, renames,
    deletes or links anything under PATH or its ancestors. Destructive behaviour is exercised
    separately, against disposable fixtures only (destructive_probe).
    """
    results: list[tuple[str, bool, str]] = []
    root = os.path.dirname(os.path.abspath(path))
    info = os.lstat(path)
    _verdict(results, 'source is a real directory, not a symlink', stat.S_ISDIR(info.st_mode), 'lstat')
    parent_open = _removable(root, os.path.basename(path))
    _verdict(results, 'create entry or symlink in parent directory denied',
             not (_can(root, os.W_OK) and _can(root, os.X_OK)), 'access check')
    _verdict(results, 'rename source tree away denied', not parent_open, 'access check')
    _verdict(results, 'rename source tree in place denied', not parent_open, 'access check')
    _verdict(results, 'replace source tree with attacker dir denied', not parent_open, 'access check')
    _verdict(results, 'replace source tree with symlink denied', not parent_open, 'access check')
    _verdict(results, 'delete source tree denied', not parent_open, 'access check')
    exposed = _ancestor_exposure(path)
    _verdict(results, 'no ancestor of the source can be renamed or replaced', not exposed,
             'exposed: ' + ', '.join(exposed) if exposed else 'access check')
    bad_dirs, bad_files, agent_owned, not_root, count, sample = _tree_exposure(path)
    _verdict(results, 'create/delete/rename/symlink inside any source directory denied', not bad_dirs,
             ('writable: ' + ', '.join(bad_dirs[:5])) if bad_dirs else f'{count} entries checked')
    _verdict(results, 'no source entry is owned by the agent (owner could chmod, then modify)', not agent_owned,
             ('agent-owned: ' + ', '.join(agent_owned[:5])) if agent_owned else f'{count} entries checked')
    _verdict(results, 'every source entry is root-owned', not not_root,
             ('not root-owned: ' + ', '.join(not_root[:5])) if not_root else 'lstat')
    _verdict(results, 'write/truncate/append/chmod of any source file denied, no symlinks or hard links', not bad_files,
             ('writable: ' + ', '.join(bad_files[:5])) if bad_files else 'access check')
    mount = os.statvfs(path)
    results.append(('read-only mount (informational)', True,
                    'mounted read-only' if mount.f_flag & os.ST_RDONLY else 'permission checks only'))
    if sample:
        results.append(('read existing file (positive control)', _readable(sample), 'read'))
    results.append(('list source tree (positive control)', _listable(path), 'list'))
    _forbidden_and_writable(results, forbidden, writable)
    return results


def probe_parent(path: str, forbidden: list[str], writable: list[str]) -> list[tuple[str, bool, str]]:
    """Non-destructive check for a work tree the agent writes into: the tree itself must be
    writable, but the agent must not be able to rename, replace, delete or symlink the tree.
    Never modifies PATH, its contents or its ancestors."""
    results: list[tuple[str, bool, str]] = []
    root = os.path.dirname(os.path.abspath(path))
    info = os.lstat(path)
    _verdict(results, 'work tree is a real directory, not a symlink', stat.S_ISDIR(info.st_mode), 'lstat')
    _verdict(results, 'create entry or symlink next to the work tree denied',
             not (_can(root, os.W_OK) and _can(root, os.X_OK)), 'access check')
    parent_open = _removable(root, os.path.basename(path))
    for name in ('rename work tree away', 'rename work tree in place', 'replace work tree with attacker dir',
                 'replace work tree with symlink', 'delete work tree'):
        _verdict(results, name + ' denied', not parent_open, 'access check')
    exposed = _ancestor_exposure(path)
    _verdict(results, 'no ancestor of the work tree can be renamed or replaced', not exposed,
             'exposed: ' + ', '.join(exposed) if exposed else 'access check')
    for target in forbidden:
        ok, detail = _denied(lambda t=target: os.listdir(t))
        results.append((f'forbidden path unreadable: {target}', ok, detail))
    results.append(('work tree writable (positive control)', _can(path, os.W_OK | os.X_OK), 'access check'))
    for target in writable:
        results.append((f'agent area writable: {target}', _can(target, os.W_OK | os.X_OK), 'access check'))
    return results


def destructive_probe(path: str, forbidden: list[str], writable: list[str]) -> list[tuple[str, bool, str]]:
    """Attempt every write-type operation as the current (agent) UID. ok=True means correct.

    DESTRUCTIVE: a layout that is wrong lets these operations succeed. It runs only against a
    disposable fixture (see require_disposable); the CLI never reaches it.
    """
    require_disposable(path)
    results: list[tuple[str, bool, str]] = []
    root = os.path.dirname(path)
    victim = None
    for base, _dirs, files in os.walk(path):
        if files:
            victim = os.path.join(base, sorted(files)[0])
            break
    # The agent's own writable area; on the same filesystem as the source so that rename
    # attempts reach the permission check instead of failing early with EXDEV.
    scratch = tempfile.mkdtemp(prefix='probe-', dir=writable[0] if writable else None)
    attacker_dir = os.path.join(scratch, 'attacker')
    os.mkdir(attacker_dir)
    os.symlink('/etc', os.path.join(attacker_dir, 'evil-link'))
    replacement = os.path.join(scratch, 'replacement')
    with open(replacement, 'w') as handle:
        handle.write('attacker content')

    def check(name, operation):
        ok, detail = _denied(operation)
        results.append((name, ok, detail))

    def write_new(directory):
        return lambda: os.close(os.open(os.path.join(directory, 'agent-new-file'), os.O_CREAT | os.O_WRONLY, 0o644))

    check('create file in source tree', write_new(path))
    check('create file in parent directory', write_new(root))
    check('mkdir in source tree', lambda: os.mkdir(os.path.join(path, 'agent-dir')))
    check('symlink in source tree', lambda: os.symlink('/etc/passwd', os.path.join(path, 'agent-link')))
    check('symlink in parent directory', lambda: os.symlink(path, os.path.join(root, 'agent-link')))
    check('rename source tree away', lambda: os.rename(path, os.path.join(scratch, 'moved-src')))
    check('rename source tree in place', lambda: os.rename(path, path + '-renamed'))
    check('replace source tree with attacker dir', lambda: os.rename(attacker_dir, path))
    check('replace source tree with symlink', lambda: os.rename(os.path.join(attacker_dir, 'evil-link'), path))
    check('delete source tree', lambda: os.rmdir(path))
    check('delete parent entry', lambda: shutil.rmtree(root))
    if victim:
        check('open existing file for write', lambda: os.close(os.open(victim, os.O_WRONLY)))
        check('open existing file read-write', lambda: os.close(os.open(victim, os.O_RDWR)))
        check('truncate existing file', lambda: os.truncate(victim, 0))
        check('append to existing file', lambda: os.close(os.open(victim, os.O_WRONLY | os.O_APPEND)))
        check('delete existing file', lambda: os.unlink(victim))
        check('rename existing file', lambda: os.rename(victim, victim + '.x'))
        check('replace existing file via rename', lambda: os.rename(replacement, victim))
        check('chmod existing file', lambda: os.chmod(victim, 0o666))
        check('chown existing file', lambda: os.chown(victim, os.getuid(), os.getgid()))
        check('touch existing file', lambda: os.utime(victim, None))

        def hardlink_then_write():
            alias = os.path.join(scratch, 'alias')
            try:
                os.link(victim, alias)
            except OSError as error:
                if error.errno in DENIED:
                    raise
                raise
            os.close(os.open(alias, os.O_WRONLY))   # an alternate path must not grant write either
        check('write through hard-link alias', hardlink_then_write)
        proc_path = f'/proc/{os.getpid()}/root{victim}'
        check('write through /proc root alias', lambda: os.close(os.open(proc_path, os.O_WRONLY)))
        results.append(('read existing file (positive control)', _readable(victim), 'read'))
    results.append(('list source tree (positive control)', _listable(path), 'list'))
    for target in forbidden:
        ok, detail = _denied(lambda t=target: os.listdir(t))
        results.append((f'forbidden path unreadable: {target}', ok, detail))
    for target in writable:
        try:
            probe_file = os.path.join(target, 'agent-writable-probe')
            with open(probe_file, 'w') as handle:
                handle.write('x')
            os.unlink(probe_file)
            results.append((f'agent area writable: {target}', True, 'ok'))
        except OSError as error:
            results.append((f'agent area writable: {target}', False, str(error)))
    shutil.rmtree(scratch, ignore_errors=True)
    return results


def destructive_probe_parent(path: str, forbidden: list[str], writable: list[str]) -> list[tuple[str, bool, str]]:
    """DESTRUCTIVE counterpart of probe_parent; disposable fixtures only."""
    require_disposable(path)
    results: list[tuple[str, bool, str]] = []
    root = os.path.dirname(path)
    scratch = tempfile.mkdtemp(prefix='probe-', dir=writable[0] if writable else None)
    attacker_dir = os.path.join(scratch, 'attacker')
    os.mkdir(attacker_dir)
    os.symlink('/etc', os.path.join(scratch, 'evil-link'))

    def check(name, operation):
        ok, detail = _denied(operation)
        results.append((name, ok, detail))

    check('create entry next to the work tree', lambda: os.close(os.open(os.path.join(root, 'agent-new'), os.O_CREAT | os.O_WRONLY)))
    check('symlink next to the work tree', lambda: os.symlink(path, os.path.join(root, 'agent-link')))
    check('rename work tree away', lambda: os.rename(path, os.path.join(scratch, 'moved')))
    check('rename work tree in place', lambda: os.rename(path, path + '-renamed'))
    check('replace work tree with attacker dir', lambda: os.rename(attacker_dir, path))
    check('replace work tree with symlink', lambda: os.rename(os.path.join(scratch, 'evil-link'), path))
    check('delete work tree', lambda: shutil.rmtree(path))
    for target in forbidden:
        ok, detail = _denied(lambda t=target: os.listdir(t))
        results.append((f'forbidden path unreadable: {target}', ok, detail))
    results.append(('work tree writable (positive control)', _can(path, os.W_OK | os.X_OK), 'access check'))
    shutil.rmtree(scratch, ignore_errors=True)
    return results


def _readable(path: str) -> bool:
    try:
        with open(path, 'rb') as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _listable(path: str) -> bool:
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ''
    try:
        if command == 'build' and len(argv) == 4:
            print(build(argv[2], argv[3]))
            return 0
        if command == 'mount' and len(argv) == 3:
            ok = mount_readonly(argv[2])
            print('read-only bind mount ' + ('active' if ok else 'unavailable (permission checks remain the gate)'))
            return 0
        if command == 'chain' and len(argv) in (3, 4):
            problems = chain_problems(argv[2], int(argv[3]) if len(argv) == 4 else 0)
            for problem in problems:
                print(f'::error::{problem}')
            return 1 if problems else 0
        if command == 'digest' and len(argv) == 3:
            print(digest(argv[2]))
            return 0
        if command in ('probe', 'probe-parent') and len(argv) >= 3:
            forbidden, writable, mode = [], [], None
            for item in argv[3:]:
                if item in ('--forbidden', '--writable'):
                    mode = item
                elif mode == '--forbidden':
                    forbidden.append(item)
                elif mode == '--writable':
                    writable.append(item)
            results = (probe if command == 'probe' else probe_parent)(argv[2], forbidden, writable)
            for name, ok, detail in results:
                print(('PASS ' if ok else 'FAIL ') + name + f' ({detail})')
            failed = [r for r in results if not r[1]]
            if failed:
                print(f'::error::{len(failed)} isolation check(s) failed for uid {os.getuid()}')
            return 1 if failed else 0
    except (IsolationError, OSError, ValueError) as error:
        print(f'::error::filesystem isolation stopped: {error}')
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
