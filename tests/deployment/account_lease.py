"""Lifecycle of the system account the deployment tests run as (account_lease v1.0).

Not collected by unittest (no test_ prefix). The runtime tests need an unprivileged UID. This module makes
sure the suite only ever removes an account it created itself, only when no other run still uses it, and
that removal happens on success, on a failing setUpClass and when another worker finishes last.

Rules
  * An account that already exists without our marker is PRE-EXISTING and is never modified or removed.
  * An account this module creates carries a marker in its GECOS comment, so a later run (or a parallel
    worker) can recognise it as suite-owned and join it instead of recreating it.
  * Every user of the account holds a lease file; creation, joining and removal happen under one flock.
  * The last live lease holder removes a suite-owned account. Leases of dead processes are pruned.
  * An account that still has running processes is not removed; that is reported as AccountInUse.
"""
from __future__ import annotations

import fcntl
import os
import secrets
import stat
import subprocess

MARKER = 'oc-test-account'
LEASE_ROOT = '/tmp/oc-test-account-leases'


class AccountInUse(RuntimeError):
    """A suite-owned account could not be removed because processes still run as it."""


class AccountCleanupFailed(RuntimeError):
    """A suite-owned account could not be removed."""


def _start_time(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/stat') as handle:
            return handle.read().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return ''


def _alive(pid: int, started: str) -> bool:
    return bool(started) and _start_time(pid) == started


def _lease_dir() -> str:
    """A world-writable sticky directory (like /tmp) so root and unprivileged runs can share leases."""
    try:
        os.mkdir(LEASE_ROOT, 0o1777)
        os.chmod(LEASE_ROOT, 0o1777)
    except FileExistsError:
        pass
    info = os.lstat(LEASE_ROOT)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or not info.st_mode & stat.S_ISVTX:
        raise AccountCleanupFailed(f'{LEASE_ROOT} is not a sticky directory; refusing to use it')
    return LEASE_ROOT


class Passwd:
    """Account database access. Replaceable in tests."""

    def __init__(self, priv):
        self.priv = list(priv or [])

    def comment(self, name):
        """GECOS field, or None when the account does not exist."""
        found = subprocess.run(['getent', 'passwd', name], capture_output=True, text=True)
        if found.returncode != 0 or not found.stdout.strip():
            return None
        return found.stdout.strip().split(':')[4]

    def create(self, name):
        done = subprocess.run([*self.priv, 'useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin',
                               '--comment', f'{MARKER} (created by the deployment tests)', name],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise AccountCleanupFailed(f'cannot create the test account {name}: {done.stderr.strip()}')

    def remove(self, name):
        return subprocess.run([*self.priv, 'userdel', name], capture_output=True, text=True).returncode == 0

    def running(self, name):
        return subprocess.run(['pgrep', '-u', name], capture_output=True).returncode == 0


def _roll_back(passwd, name, failure):
    """Undo the creation after a later step failed, and check that it worked. If the account is still
    there, raise AccountCleanupFailed naming both problems; the original error stays as __cause__."""
    try:
        removed = passwd.remove(name) and passwd.comment(name) is None
        problem = '' if removed else 'userdel failed or the account is still present'
    except Exception as error:      # noqa: BLE001 - reported below together with the original failure
        removed, problem = False, f'{type(error).__name__}: {error}'
    if not removed:
        raise AccountCleanupFailed(
            f'lease initialisation for {name} failed ({type(failure).__name__}: {failure}) and the account '
            f'created just before could not be rolled back ({problem}); remove it by hand') from failure


class Lease:
    def __init__(self, name, role, path, passwd, lock_path):
        self.name, self.role, self.path, self.passwd, self.lock_path = name, role, path, passwd, lock_path
        self.released = False

    @property
    def created(self):
        return self.role == 'created'

    def release(self):
        """Give the lease back; the last holder removes a suite-owned account. Idempotent."""
        if self.released:
            return
        self.released = True
        with open(self.lock_path, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            if self.role == 'preexisting' or self.passwd.comment(self.name) is None:
                return
            if not (self.passwd.comment(self.name) or '').startswith(MARKER):
                return                                     # not ours (or changed since): never removed
            if _live_leases(self.lock_path, self.name):
                return                                     # another run still uses it; the last one removes it
            if self.passwd.running(self.name):
                raise AccountInUse(f'{self.name} still has running processes; it was left in place')
            if not self.passwd.remove(self.name) or self.passwd.comment(self.name) is not None:
                raise AccountCleanupFailed(f'could not remove the suite-owned account {self.name}')


def _live_leases(lock_path, name):
    directory = os.path.dirname(lock_path)
    live = []
    for entry in os.listdir(directory):
        if not entry.startswith(name + '.') or entry.endswith('.lock'):
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path) as handle:
                pid, started, _role = handle.read().split()
        except (OSError, ValueError):
            pid, started = '0', ''
        if _alive(int(pid), started):
            live.append(path)
        else:
            try:
                os.unlink(path)                            # stale lease of a dead process
            except FileNotFoundError:
                pass
    return live


def acquire(name, priv, passwd=None, lease_root=None):
    """Join or create the account under the shared lock and return a Lease. If anything after creating the
    account fails, the account is removed again before the error propagates."""
    passwd = passwd or Passwd(priv)
    directory = lease_root or _lease_dir()
    lock_path = os.path.join(directory, f'{name}.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        pass
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _live_leases(lock_path, name)                      # prune stale leases first
        existing = passwd.comment(name)
        created = False
        if existing is None:
            passwd.create(name)
            created = True
            role = 'created'
        elif existing.startswith(MARKER):
            role = 'joined'                                # suite-owned, created by another worker or run
        else:
            role = 'preexisting'                           # somebody else's account: never touched
        path = os.path.join(directory, f'{name}.{os.getpid()}.{secrets.token_hex(4)}')
        try:
            with open(path, 'w') as handle:
                handle.write(f'{os.getpid()} {_start_time(os.getpid())} {role}\n')
        except BaseException as failure:
            if created:                                    # only an account THIS call created is ever rolled back
                _roll_back(passwd, name, failure)
            raise
    return Lease(name, role, path, passwd, lock_path)


def lease_for_class(cls, name, priv, **kwargs):
    """acquire() plus immediate registration of the release as a class cleanup, so a setUpClass that raises
    afterwards still gives the account back."""
    lease = acquire(name, priv, **kwargs)
    cls.addClassCleanup(lease.release)
    return lease
