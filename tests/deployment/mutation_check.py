#!/usr/bin/env python3
"""Mutation inventory for the OpenCode security controls (mutation check v2.0).

Each entry replaces one exact snippet in a copy of the repository, runs the deployment test
suite against the copy, and expects at least one test to fail ("caught"). A "MISSED" mutation
means a control is not protected by any test. Not collected by unittest (no test_ prefix).

    python3 tests/deployment/mutation_check.py [--workers 4] [--only ID ...] [--json OUT]

The end-to-end tests are skipped here (OPENCODE_SKIP_E2E=1) because they need the real binary
and take minutes; the suite still runs the real-git, real-UID and real-transcript tests.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strict_cleanup  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
S = '.github/scripts/'

# (id, finding, file, exact old snippet, replacement)
MUTATIONS = [
    # --- P1-1 proxy model enforcement
    ('P1-1a', 'proxy forwards any model', S + 'opencode_proxy.py', "data['model'] != bound", 'False'),
    ('P1-1b', 'duplicate JSON keys accepted', S + 'opencode_proxy.py', 'object_pairs_hook=_no_duplicate_keys,\n                          parse_constant', 'parse_constant'),
    ('P1-1c', 'unknown routing fields accepted', S + 'opencode_proxy.py', 'if extra:', 'if False:'),
    ('P1-1d', 'any POST endpoint accepted', S + 'opencode_proxy.py', 'if raw_path not in POST_PATHS:', 'if False:'),
    ('P1-1e', 'compressed bodies accepted', S + 'opencode_proxy.py', "if encoding != 'identity':", 'if False:'),
    ('P1-1f', 'chunked bodies accepted', S + 'opencode_proxy.py', "if headers.get('Transfer-Encoding') is not None:", 'if False:'),
    ('P1-1g', 'ineligible model can be bound', S + 'opencode_proxy.py', 'if router.blockers(registry', 'if False and router.blockers(registry'),
    ('P1-1h', 'reviewer may be bound to implementer model', S + 'opencode_proxy.py', 'if full in {router.normalize(m) for m in denied}:', 'if False:'),
    ('P1-1i', 'paid/unapproved model can be bound', S + 'opencode_proxy.py', "if full not in router.APPROVED_MODELS or not full.endswith('-free'):", 'if False:'),
    ('P1-1j', 'unsanitized path in rejection log', S + 'opencode_proxy.py', "re.sub(r'[^A-Za-z0-9/._-]', '?', path.split('?')[0])[:80]", 'path'),
    ('P1-1k', 'query strings accepted', S + 'opencode_proxy.py', "if '?' in raw_path or '#' in raw_path", "if '#' in raw_path"),
    ('P1-1l', 'agent config lacks small_model', S + 'opencode_sandbox.py', "        'small_model': model,\n", ''),
    # --- P1-2 reviewer filesystem isolation
    ('P1-2a', 'parent directory writable by others', S + 'opencode_fsisolate.py', 'os.chmod(root, 0o755)', 'os.chmod(root, 0o777)'),
    ('P1-2b', 'source files writable', S + 'opencode_fsisolate.py', 'os.chmod(path, 0o555 if executable else 0o444)', 'os.chmod(path, 0o666)'),
    ('P1-2c', 'source directories writable', S + 'opencode_fsisolate.py', 'os.chmod(path, 0o555)\n        for name in files:', 'os.chmod(path, 0o777)\n        for name in files:'),
    ('P1-2d', 'chain check ignores writable ancestors', S + 'opencode_fsisolate.py', "if info.st_mode & 0o022 and not", "if False and not"),
    ('P1-2e', 'chain check ignores ownership', S + 'opencode_fsisolate.py', 'if info.st_uid != owner:', 'if False:'),
    ('P1-2f', 'probe treats any outcome as denied', S + 'opencode_fsisolate.py', 'return (error.errno in DENIED), f', 'return True, f'),
    ('P1-2g', 'digest ignores file content', S + 'opencode_fsisolate.py', 'h.update(chunk)', 'pass'),
    ('P1-2l', 'preflight ignores a writable parent', S + 'opencode_fsisolate.py', "    if not (_can(parent, os.W_OK) and _can(parent, os.X_OK)):\n        return False", "    return False"),
    ('P1-2m', 'preflight ignores writable files and directories', S + 'opencode_fsisolate.py', "        if _can(base, os.W_OK):\n            bad_dirs.append(base)", "        pass"),
    ('P1-2n', 'destructive probe runs on real trees', S + 'opencode_fsisolate.py', "    if not os.path.isfile(marker):\n        raise IsolationError(", "    if False:\n        raise IsolationError("),
    ('P1-2o', 'preflight ignores a symlinked tree', S + 'opencode_fsisolate.py', "_verdict(results, 'work tree is a real directory, not a symlink', stat.S_ISDIR(info.st_mode), 'lstat')", "_verdict(results, 'work tree is a real directory, not a symlink', True, 'lstat')"),
    ('P1-2p', 'agent-owned read-only entries accepted', S + 'opencode_fsisolate.py', "    return os.geteuid() != 0 and info.st_uid == os.geteuid()", "    return False"),
    ('P1-2q', 'entries owned by other uids accepted', S + 'opencode_fsisolate.py', "        elif info.st_uid != 0:\n            not_root.append(full)", "        else:\n            pass"),
    ('P1-2r', 'hard-linked source files accepted', S + 'opencode_fsisolate.py', "                if info.st_nlink > 1:", "                if False:"),
    ('P1-2s', 'agent-owned ancestors accepted', S + 'opencode_fsisolate.py', " or _owner_can_chmod(os.lstat(parent)):", ":"),
    ('P2-m1', 'partial bind mount is left behind', S + 'opencode_fsisolate.py', "    for cmd in (['umount', target], ['umount', '-l', target]):", "    for cmd in ():"),
    ('P2-m2', 'stuck partial mount reported as unavailable', S + 'opencode_fsisolate.py', "    raise IsolationError(f'bind mount at {target} could not be made read-only and could not be removed')", "    return False"),
    ('P2-m3', 'missing mount privileges raise instead of reporting unavailable', S + 'opencode_fsisolate.py', "    except OSError:\n        return False                               # no mount binary", "    except ZeroDivisionError:\n        return False                               # no mount binary"),
    ('P2-m4', 'test cleanup tolerates a surviving mount', 'tests/deployment/test_opencode_runtime.py', "    if leftover > 0:\n        raise AssertionError", "    if False:\n        raise AssertionError"),
    ('P2-c1', 'probe does not verify the unmount', 'tests/deployment/test_opencode_runtime.py', "        if mounted(target):\n            raise MountLeak(f'probe bind mount", "        if False:\n            raise MountLeak(f'probe bind mount"),
    ('P2-c2', 'probe never retries the unmount', 'tests/deployment/test_opencode_runtime.py', "for attempt in (['umount', target], ['umount', '-l', target], ['umount', '-l', target]):", "for attempt in (['umount', target],):"),
    ('P2-c3', 'probe trusts the exit status over the mount table', 'tests/deployment/test_opencode_runtime.py', "            if not mounted(target):\n                break\n            run(attempt)", "            r = run(attempt)\n            if r.returncode == 0:\n                break"),
    ('P2-c4', 'probe swallows directory removal failures', 'tests/deployment/test_opencode_runtime.py', "        raise MountLeak(f'probe directory {probe} could not be cleaned up: {error}') from error", "        pass"),
    ('P2-c6', 'probe deletes the directory before unmounting', 'tests/deployment/test_opencode_runtime.py', "        for attempt in (['umount', target]", "        remove(probe)\n        for attempt in (['umount', target]"),
    ('P2-c5', 'tree remover deletes through a live mount', 'tests/deployment/strict_cleanup.py', "    if live:\n        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')\n    rmtree(path)", "    rmtree(path)"),
    ('P2-d1', 'mountinfo paths are not decoded', 'tests/deployment/strict_cleanup.py', "    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)", "    return field"),
    ('P2-d2', 'mountinfo backslash escape not decoded', 'tests/deployment/strict_cleanup.py', "_ESCAPE = re.compile(r'\\\\([0-7]{3})')", "_ESCAPE = re.compile(r'\\\\(0[0-7]{2})')"),
    ('P2-d3', 'mountinfo path field is not decoded in the parser', 'tests/deployment/strict_cleanup.py', "points.append(canonical(decode_mountinfo(fields[4])))", "points.append(canonical(fields[4]))"),
    ('P2-d4', 'mount prefix match ignores the separator', 'tests/deployment/strict_cleanup.py', "    prefix = wanted.rstrip('/') + '/'\n", "    prefix = wanted.rstrip('/')\n"),
    ('P2-d5', 'records split on any line boundary', 'tests/deployment/strict_cleanup.py', "    for record in text.split('\\n'):\n        fields = record.split(' ')", "    for record in text.splitlines():\n        fields = record.split(' ')"),
    ('P2-d6', 'fields split on any whitespace', 'tests/deployment/strict_cleanup.py', "        fields = record.split(' ')\n        if len(fields) > 4:", "        fields = record.split()\n        if len(fields) > 4:"),
    ('P2-d7', 'mountinfo read with newline translation', 'tests/deployment/strict_cleanup.py', "errors='surrogateescape', newline='') as handle", "errors='surrogateescape') as handle"),
    ('P2-d8', 'queried path not canonicalized (mounts_at)', 'tests/deployment/strict_cleanup.py', "    wanted = canonical(path)\n    return [point for point in mount_points(text) if point == wanted]", "    wanted = path\n    return [point for point in mount_points(text) if point == wanted]"),
    ('P2-d9', 'mount points not canonicalized', 'tests/deployment/strict_cleanup.py', "            points.append(canonical(decode_mountinfo(fields[4])))", "            points.append(decode_mountinfo(fields[4]))"),
    ('P2-t1', 'temp directory cleanup not registered at creation', 'tests/deployment/strict_cleanup.py', "    case.addCleanup(remove_verified, path, identity(path))\n    return path", "    return path"),
    ('P2-t2', 'tree remover swallows deletion errors', 'tests/deployment/strict_cleanup.py', "        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')\n    rmtree(path)", "        raise MountLeak(f'refusing to delete {path}: still mounted at {live}')\n    try:\n        rmtree(path)\n    except OSError:\n        pass"),
    ('P2-i3', 'final rmdir skips the identity re-check', 'tests/deployment/strict_cleanup.py', "        if (again.st_dev, again.st_ino) != tuple(expected):\n            raise CleanupRefused(refusal)\n        os.rmdir", "        os.rmdir"),
    ('P2-i4', 'cleanup follows a symlinked path', 'tests/deployment/strict_cleanup.py', "_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)", "_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_CLOEXEC', 0)"),
    ('P2-i5', 'identity captured from a followed symlink', 'tests/deployment/strict_cleanup.py', "    info = os.lstat(path)\n    if not stat.S_ISDIR(info.st_mode):\n        raise CleanupRefused(f'{path} is not a real directory')", "    info = os.stat(path)\n    if not stat.S_ISDIR(info.st_mode):\n        raise CleanupRefused(f'{path} is not a real directory')"),
    ('P2-p2', 'privileged cleanup ignores the helper result', 'tests/deployment/strict_cleanup.py', "    if done.returncode != 0 or os.path.lexists(path):\n        raise OSError", "    if False:\n        raise OSError"),
    ('P2-p4', 'privileged temp dir records no identity', 'tests/deployment/strict_cleanup.py', "    case.addCleanup(remove_privileged_tree, path, priv, identity(path))", "    case.addCleanup(remove_privileged_tree, path, priv, (0, 0))"),
    ('P2-k1', 'runtime tool directory not registered as class cleanup', 'tests/deployment/test_opencode_runtime.py', "cls.tool = class_temp_dir(cls, 'ocfs-tool-')", "cls.tool = tempfile.mkdtemp(prefix='ocfs-tool-')"),
    ('P2-k2', 'proxy log directory not registered as class cleanup', 'tests/deployment/test_opencode_sandbox.py', "cls.tmp = class_temp_dir(cls, 'ocsb-')", "cls.tmp = tempfile.mkdtemp(prefix='ocsb-')"),
    ('P2-k3', 'class cleanup registered after setup can fail', 'tests/deployment/strict_cleanup.py', "    cls.addClassCleanup(remove_verified, path, identity(path))", "    pass"),
    ('P2-r1', 'mutation worktree cleanup ignores errors', 'tests/deployment/mutation_check.py', "    except Exception as error:      # noqa: BLE001 - every failure is reported, none is hidden\n        return {'state': 'FAILED'", "    except ZeroDivisionError as error:\n        return {'state': 'FAILED'"),
    ('P2-r2', 'mutation runner hides cleanup failure behind the suite verdict', 'tests/deployment/mutation_check.py', "    if outcome['state'] == 'FAILED':\n        # a worktree", "    if False:\n        # a worktree"),
    ('P2-r4', 'mutation runner exit code ignores cleanup failures', 'tests/deployment/mutation_check.py', "def exit_code(summary):\n    return 1 if", "def exit_code(summary):\n    return 0 if True else 1 if"),
    ('P2-a3', 'worker errors escape the guard', 'tests/deployment/mutation_check.py', "    except Exception as error:      # noqa: BLE001 - a worker error must not abort the run or skip cleanup\n        result.update", "    except ZeroDivisionError as error:      # noqa: BLE001\n        result.update"),
    ('P2-a4', 'worker errors do not fail the run', 'tests/deployment/mutation_check.py', "or summary['TIMEOUT'] or summary['ERROR']\n", "or summary['TIMEOUT']\n"),
    ('P2-l1', 'a pre-existing account can be removed', 'tests/deployment/account_lease.py', "            if self.role == 'preexisting' or self.passwd.comment(self.name) is None:\n                return\n            if not (self.passwd.comment(self.name) or '').startswith(MARKER):\n                return                                     # not ours (or changed since): never removed\n", "            if self.passwd.comment(self.name) is None:\n                return\n"),
    ('P2-l2', 'account removed while another worker uses it', 'tests/deployment/account_lease.py', "            if _live_leases(self.lock_path, self.name):\n                return", "            if False:\n                return"),
    ('P2-l3', 'account removed while processes run as it', 'tests/deployment/account_lease.py', "            if self.passwd.running(self.name):\n                raise AccountInUse", "            if False:\n                raise AccountInUse"),
    ('P2-l4', 'account release not registered as class cleanup', 'tests/deployment/account_lease.py', "    cls.addClassCleanup(lease.release)", "    pass"),
    ('P2-l6', 'created account carries no ownership marker', 'tests/deployment/account_lease.py', "'--comment', f'{MARKER} (created by the deployment tests)', name],", "name],"),
    ('P2-l7', 'stale leases are never pruned', 'tests/deployment/account_lease.py', "            try:\n                os.unlink(path)                            # stale lease of a dead process", "            try:\n                pass                            # stale lease of a dead process"),
    ('P2-l8', 'account rollback result is not verified', 'tests/deployment/account_lease.py', "        removed = passwd.remove(name) and passwd.comment(name) is None\n", "        passwd.remove(name)\n        removed = True\n"),
    ('P2-l9', 'a failed account rollback is swallowed', 'tests/deployment/account_lease.py', "    if not removed:\n        raise AccountCleanupFailed(", "    if False:\n        raise AccountCleanupFailed("),
    ('P2-l10', 'rollback can remove an account this call did not create', 'tests/deployment/account_lease.py', "            if created:                                    # only an account THIS call created is ever rolled back\n", "            if True:\n"),
    ('P2-l11', 'rollback failure drops the original error', 'tests/deployment/account_lease.py', "remove it by hand') from failure\n\n\nclass Lease:", "remove it by hand') from None\n\n\nclass Lease:"),
    ('P2-p1', 'privileged cleanup passes no identity to the root helper', 'tests/deployment/strict_cleanup.py', "'rm-verified',\n                           path, str(expected[0]), str(expected[1])]", "'rm-verified',\n                           path, '0', '0']"),
    ('P2-l5', 'account kept after a failure following its creation', 'tests/deployment/account_lease.py', "                _roll_back(passwd, name, failure)\n            raise", "                pass\n            raise"),
    ('P2-b1', 'deletion does not verify the worktree identity on the descriptor', 'tests/deployment/strict_cleanup.py', "        if (info.st_dev, info.st_ino) != tuple(expected):\n            raise CleanupRefused(refusal)\n        root_mount", "        root_mount"),
    ('P2-b2', 'deletion compares the device only', 'tests/deployment/strict_cleanup.py', "        if (info.st_dev, info.st_ino) != tuple(expected):\n            raise CleanupRefused(refusal)\n        root_mount", "        if (info.st_dev,) != tuple(expected)[:1]:\n            raise CleanupRefused(refusal)\n        root_mount"),
    ('P2-b3', 'RESOLVE_NO_XDEV dropped (mount-id check is then the only layer)', 'tests/deployment/strict_cleanup.py', "_RESOLVE = 0x01 | 0x02 | 0x04", "_RESOLVE = 0x02 | 0x04"),
    ('P2-b4', 'directory mount-id check dropped (RESOLVE_NO_XDEV is then the only layer)', 'tests/deployment/strict_cleanup.py', "            if mount_id(child) != root_mount:\n                raise MountLeak(f'refusing to delete {below}: it is on another mount')\n", ""),
    ('P2-b5', 'file mount-id check dropped', 'tests/deployment/strict_cleanup.py', "                if mount_id(handle) != root_mount:\n                    raise MountLeak(f'refusing to delete {below}: it is on another mount')\n", "                pass\n"),
    ('P2-b6', 'a worktree that is itself a mount point is deleted through', 'tests/deployment/strict_cleanup.py', "            os.close(_open_below(fd, '..', os.O_RDONLY | os.O_DIRECTORY))     # fails with EXDEV at a mount root", "            pass"),
    ('P2-b7', 'final rmdir skips the identity re-check', 'tests/deployment/strict_cleanup.py', "        if (again.st_dev, again.st_ino) != tuple(expected):\n            raise CleanupRefused(refusal)\n        os.rmdir(name, dir_fd=parent)", "        os.rmdir(name, dir_fd=parent)"),
    ('P2-b8', 'a replaced descendant directory is entered', 'tests/deployment/strict_cleanup.py', "            if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:", "            if False:"),
    ('P2-b9', 'unsupported openat2 errors fall through as ordinary errors', 'tests/deployment/strict_cleanup.py', "    if code in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):", "    if False:"),
    ('P2-b10', 'unwired architectures are not refused', 'tests/deployment/strict_cleanup.py', "    if platform.machine() not in _SYS_ARCH_OK:", "    if False:"),
    ('P2-b11', 'missing mount ids are accepted', 'tests/deployment/strict_cleanup.py', "    if not buffer.mask & 0x1000:", "    if False:"),
    ('P2-b12', 'the root helper does not report an unavailable boundary distinctly', 'tests/deployment/strict_cleanup.py', "        except BoundaryUnavailable as error:\n            print(error, file=sys.stderr)\n            return 6\n", ""),
    ('P2-b13', 'symlinks are followed when opening entries', 'tests/deployment/strict_cleanup.py', "    how = _OpenHow(flags | os.O_CLOEXEC | os.O_NOFOLLOW, 0, _RESOLVE)", "    how = _OpenHow(flags | os.O_CLOEXEC, 0, 0)"),
    ('P2-s3', 'isolation is not required', 'tests/deployment/mutation_check.py', "    if not isolation_available():\n        return {**result", "    if False:\n        return {**result"),
    ('P2-s4', 'the control run is skipped', 'tests/deployment/mutation_check.py', "    control_result = None\n    if control:", "    control_result = None\n    if False:"),
    ('P2-s5', 'a failing control does not stop the run', 'tests/deployment/mutation_check.py', "        if control_result['baseline'] != 'PASSED' or control_result['cleanup'] == 'FAILED':\n            return control_result, []", "        if False:\n            return control_result, []"),
    ('P2-s6', 'the namespace wrapper reports no leaked mounts', 'tests/deployment/ns_run.py', "    leaked = len(strict_cleanup.mounts_under(cwd))", "    leaked = 0"),
    ('P2-s8', 'a mount in the host namespace does not stop the delete', 'tests/deployment/mutation_check.py', "        if live:\n            return {'state': 'FAILED', 'leaked_mounts': leaked,\n                    'detail': f'mount(s) are present", "        if False:\n            return {'state': 'FAILED', 'leaked_mounts': leaked,\n                    'detail': f'mount(s) are present"),
    ('P2-s9', 'the worktree identity is not re-checked before the delete', 'tests/deployment/mutation_check.py', "        strict_cleanup.check_identity(str(work), expected)\n        live = ", "        live = "),
    ('P2-s10', 'a worktree without a recorded identity is deleted like an identified one', 'tests/deployment/mutation_check.py', "    finally:\n        outcome = discard_unidentified(work) if expected is None else cleanup_worktree(work, expected, leaked)", "    finally:\n        outcome = cleanup_worktree(work, expected, leaked)"),
    ('P2-s1', 'mutated suites do not get a private PID namespace', 'tests/deployment/mutation_check.py', "UNSHARE = ['unshare', '--mount', '--pid', '--fork', '--mount-proc', '--propagation', 'private']\nWRAPPER", "UNSHARE = ['unshare', '--mount', '--fork', '--propagation', 'private']\nWRAPPER"),
    ('P2-s7', 'the isolated copy lacks scripts/', 'tests/deployment/mutation_check.py', "COPY_TREES = ('.github', 'tests', 'scripts', 'docs')              # everything the deployment tests read from the repo\n", "COPY_TREES = ('.github', 'tests', 'docs')\n"),
    ('P2-4a', 'oversized files reach the model (both size checks removed)', '.github/scripts/opencode_sandbox.py', "        if size > MAX_FILE_BYTES:\n            return 'excluded', 'larger than the per-file limit'\n        with open(path, 'rb') as handle:\n            data = handle.read(MAX_FILE_BYTES + 1)\n    except OSError:\n        return 'excluded', 'unreadable'\n    if len(data) > MAX_FILE_BYTES:\n        return 'excluded', 'larger than the per-file limit'\n", "        with open(path, 'rb') as handle:\n            data = handle.read()\n    except OSError:\n        return 'excluded', 'unreadable'\n"),
    ('P2-s2', 'the wrapper runs the suite in a namespace whose root is not private', 'tests/deployment/ns_run.py', "    if not root_is_private(strict_cleanup.read_mountinfo()):", "    if False:"),
    ('P2-s13', 'a root mount with propagation tags is treated as private', 'tests/deployment/ns_run.py', "            return not any(tag.startswith(('shared:', 'master:')) or tag == 'unbindable' for tag in optional)", "            return True"),
    ('P2-s12', 'the report is trusted even when it is absent', 'tests/deployment/mutation_check.py', '            raise RuntimeError(f\'the namespace wrapper produced no usable report (exit status {done.returncode}): \'\n                               f\'{(done.stderr or "")[-300:].strip()}\') from None', '            leaked, timed_out = 0, False'),
    ('P2-u2', 'a leaked test account is not removed after the run', 'tests/deployment/mutation_check.py', "    removed = subprocess.run([*_privilege_prefix(), 'userdel', name], capture_output=True, text=True)\n", "    removed = subprocess.CompletedProcess([], 0, '', '')\n"),
    ('P2-u3', 'a test account that cannot be removed does not fail the run', 'tests/deployment/mutation_check.py', "        if account_error:\n            outcome = {'state': 'FAILED'", "        if False:\n            outcome = {'state': 'FAILED'"),
    ('P2-s11', 'mutated suites keep the shared temporary directory', 'tests/deployment/mutation_check.py', "'TMPDIR': str(scratch), 'OPENCODE_TEST_ACCOUNT': account}\n        report = ", "'OPENCODE_TEST_ACCOUNT': account}\n        report = "),
    ('P2-u1', 'isolated runs share one test account name', 'tests/deployment/mutation_check.py', "'TMPDIR': str(scratch), 'OPENCODE_TEST_ACCOUNT': account}\n        report = ", "'TMPDIR': str(scratch), 'OPENCODE_TEST_ACCOUNT': 'ocprobe'}\n        report = "),
    ('P1-2h', 'collector follows a swapped work tree', S + 'opencode_collect.py', "if os.path.islink(worktree) or os.path.realpath(worktree) != os.path.abspath(worktree):", 'if False:'),
    ('P1-2i', 'collector ignores agent-owned ancestors', S + 'opencode_collect.py', "if os.environ.get('OPENCODE_REQUIRE_ROOT_CHAIN') == '1' else []", 'if False else []'),
    ('P1-2j', 'read-only check accepts modifications', S + 'opencode_review.py', "if not (change.get('path') == 'REVIEW/diff.patch'", "if False and not (change.get('path') == 'REVIEW/diff.patch'"),
    ('P1-2k', 'reviewer config allows edits', S + 'opencode_sandbox.py', "'edit': 'deny' if mode == 'review' else 'allow'", "'edit': 'allow'"),
    # --- P1-3 completion transcript
    ('P1-3a', 'sessions may differ', S + 'opencode_guard.py', "if event.get('sessionID') != session:", 'if False:'),
    ('P1-3b', 'unfinished step accepted', S + 'opencode_guard.py', 'if open_step is not None:\n        return fail', 'if False:\n        return fail'),
    ('P1-3c', 'unmatched step_finish accepted', S + 'opencode_guard.py', "if open_step is None or part['messageID'] != open_step[0]:\n                return fail('unmatched_step_finish')", 'if False:\n                return fail(\'unmatched_step_finish\')'),
    ('P1-3d', 'non-stop reasons accepted', S + 'opencode_guard.py', "if reason not in ('stop', 'tool-calls'):", 'if False:'),
    ('P1-3e', 'error events tolerated', S + 'opencode_guard.py', "if kind == 'error':\n            return fail('agent_reported_error')", "if False:\n            return fail('agent_reported_error')"),
    ('P1-3f', 'timestamps may go backwards', S + 'opencode_guard.py', 'or stamp < last_stamp:', 'or False:'),
    ('P1-3g', 'stop with tool calls accepted', S + 'opencode_guard.py', "if reason == 'stop' and open_step[1]:", 'if False:'),
    ('P1-3h', 'no terminal stop required', S + 'opencode_guard.py', "if not finished or finished[-1][0] != 'stop':", 'if False:'),
    ('P1-3i', 'step after stop accepted', S + 'opencode_guard.py', "if open_step is not None or (finished and finished[-1][0] == 'stop'):", 'if open_step is not None:'),
    ('P1-3j', 'non-zero exit accepted', S + 'opencode_guard.py', 'if exit_code != 0:\n        return fail', 'if False:\n        return fail'),
    ('P1-3k', 'timeout accepted', S + 'opencode_guard.py', 'if timed_out:\n        return fail', 'if False:\n        return fail'),
    ('P1-3l', 'malformed parts accepted', S + 'opencode_guard.py', "or part.get('sessionID') != session", 'or False'),
    ('P1-3m', 'tool calls without tools accepted', S + 'opencode_guard.py', "if reason == 'tool-calls' and not open_step[1]:", 'if False:'),
    # --- P2-1/P2-2/P2-5 publication
    ('P2-1a', 'no final authorization before update', S + 'opencode_publish.py', '            check_pr_target(api, env)\n            ahead', '            ahead'),
    ('P2-2a', 'update is not an exact-SHA compare-and-swap', S + 'opencode_publish.py', "f'--force-with-lease=refs/heads/{branch}:{expected}',\n                   remote,", "remote,"),
    ('P2-2b', 'descent from baseline not checked', S + 'opencode_publish.py', "if ahead.get('status') != 'ahead' or ahead.get('behind_by') != 0:", 'if False:'),
    ('P2-2c', 'non-SHA expectation accepted', S + 'opencode_publish.py', 'if not (SHA_RE.match(expected) and SHA_RE.match(new)):', 'if False:'),
    ('P2-5a', 'failure always reported as unchanged', S + 'opencode_publish.py', 'elif observed == base:', 'elif True:'),
    ('P2-5b', 'deletion never confirmed', S + 'opencode_publish.py', "    try:\n        left = remote_sha(api, branch)\n    except PublishError as error:\n        return f'could not confirm whether {branch} still exists ({error})'\n    return '' if left is None else f'{branch} still exists at {left}'", "    return ''"),
    ('P2-5c', 'ambiguous commit not investigated', S + 'opencode_publish.py', "        except AmbiguousApiError:\n            tip = remote_sha(api, work_branch)", "        except ZeroDivisionError:\n            tip = remote_sha(api, work_branch)"),
    ('P2-5d', 'ambiguous ref creation not investigated', S + 'opencode_publish.py', "    except AmbiguousApiError:\n        observed = remote_sha(api, work_branch)", "    except ZeroDivisionError:\n        observed = remote_sha(api, work_branch)"),
    ('P2-5e', 'server errors treated as definite', S + 'opencode_publish.py', 'cls = AmbiguousApiError if error.code >= 500 else PublishError', 'cls = PublishError'),
    ('P2-5f', 'evidence not recorded', S + 'opencode_publish.py', "evidence['pr_branch_observed_after_failure'] = observed", 'pass'),
    # --- P2-3 immutable review inputs
    ('P2-3a', 'review inputs read from the live PR', S + 'opencode_review.py', "compare = f'repos/{repo}/compare/{base_sha}...{head_sha}'", "compare = f'repos/{repo}/pulls/{number}'"),
    ('P2-3b', 'stale-on-arrival not detected', S + 'opencode_review.py', "stale_on_arrival = (head.get('sha') != sha or pr_base.get('sha') != base_sha\n                        or pr.get('state') != 'open' or bool(pr.get('merged')))", 'stale_on_arrival = False'),
    ('P2-3c', 'base change does not stale a review', S + 'opencode_review.py', "and (base_sha is None or marker.get('base_commit') == base_sha))", ')'),
    ('P2-3d', 'quoted paths not decoded', S + 'opencode_review.py', "    if not (len(token) >= 2 and token[0] == '\"' and token[-1] == '\"'):\n        return token", '    return token'),
    ('P2-3e', 'diff/API path mismatch tolerated', S + 'opencode_review.py', 'if not named <= api_names:', 'if False:'),
    ('P2-3f', 'truncated file list tolerated', S + 'opencode_review.py', 'or len(files) >= 300:', 'or False:'),
    ('P2-3g', 'stale_on_arrival ignored in status', S + 'opencode_review.py', "and not marker.get('stale_on_arrival')", ''),
    # --- P2-4 confidentiality
    ('P2-4b', 'NUL-containing files reach the model', S + 'opencode_sandbox.py', "    if b'\\0' in data:\n        return 'excluded', 'binary content'", '    pass'),
    ('P2-4c', 'non-UTF-8 files reach the model', S + 'opencode_sandbox.py', "    except UnicodeDecodeError:\n        return 'excluded', 'not valid UTF-8'", "    except UnicodeDecodeError:\n        return 'ok', ''"),
    ('P2-4d', 'secret hits ignored', S + 'opencode_sandbox.py', '    if secret_hits:', '    if False:'),
    ('P2-4e', 'secret scan keeps a leading word boundary', S + 'opencode_sandbox.py', 'gh[pousr]_[A-Za-z0-9]{30,}|', r'\\bgh[pousr]_[A-Za-z0-9]{30,}|'),
    ('P2-4f', 'total size unbounded', S + 'opencode_sandbox.py', 'or total > MAX_TOTAL_BYTES:', 'or False:'),
    # --- earlier controls that must stay protected
    ('R-1', 'model eligibility ignores terms', S + 'opencode_router.py', "meta.get('terms_verified') is not True or not (", 'False or not ('),
    ('R-2', 'reviewer may equal implementer', S + 'opencode_review.py', "if policy['require_distinct_models'] and model in", 'if False and model in'),
    ('R-3', 'reviewer session reuse allowed', S + 'opencode_review.py', "if reviewer_session in {r['session']", "if False and reviewer_session in {r['session']"),
    ('R-4', 'review posted as approval', S + 'opencode_review.py', "'event': 'COMMENT'", "'event': 'APPROVE'"),
    ('R-5', 'manifest schema unchecked', S + 'opencode_publish.py', 'set(manifest) != TOP_KEYS', 'False'),
    ('R-6', 'generation failure still publishes', S + 'opencode_publish.py', "if env.get('GENERATION_STATUS') != 'success':", 'if False:'),
    ('R-7', 'forged baseline accepted', S + 'opencode_publish.py', 'if base != authorized:', 'if False:'),
    ('R-8', 'gate accepts any commenter id', S + 'opencode_gate.py', "if user.get('id') not in allowed_ids:", 'if False:'),
]


SUITE = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/deployment', '-p', 'test_*.py']


def _privilege_prefix():
    """Namespaces and root-owned leftovers need root; a non-root runner uses passwordless sudo, as the mount
    tests themselves do."""
    return [] if os.geteuid() == 0 else (['sudo', '-n'] if shutil.which('sudo') else [])


COPY_TREES = ('.github', 'tests', 'scripts', 'docs')              # everything the deployment tests read from the repo
COPY_FILES = ('docker-compose.yml', 'docker-compose.production.yml')
UNSHARE = ['unshare', '--mount', '--pid', '--fork', '--mount-proc', '--propagation', 'private']
WRAPPER = str(Path(__file__).resolve().parent / 'ns_run.py')
SUITE_TIMEOUT = 400
_ISOLATION = {}


def isolation_available():
    """True when a mutated suite can be run as PID 1 of private PID and mount namespaces AND the descriptor-
    based deletion boundary works. If not, nothing is run: an unisolated hostile suite is never started."""
    if 'ok' not in _ISOLATION:
        try:
            probe = subprocess.run([*_privilege_prefix(), *UNSHARE, '--', 'true'], capture_output=True, timeout=60)
            _ISOLATION['ok'] = probe.returncode == 0 and strict_cleanup.boundary_available()
        except (OSError, subprocess.SubprocessError):
            _ISOLATION['ok'] = False
    return _ISOLATION['ok']


def isolated_command(work, report, suite, env):
    """The command line that runs `suite` in `work` as PID 1 of private PID + mount namespaces (see ns_run.py).
    Mounts it makes die with the namespace, and so does every process it leaves behind."""
    inner = [sys.executable, '-I', WRAPPER, str(report), str(work), str(SUITE_TIMEOUT), '--', *suite]
    prefix = _privilege_prefix()
    if not prefix:
        return [*UNSHARE, '--', *inner]
    groups = ','.join(str(g) for g in os.getgroups())
    drop = ['setpriv', f'--reuid={os.getuid()}', f'--regid={os.getgid()}',
            f'--groups={groups}' if groups else '--clear-groups']
    return [*prefix, *UNSHARE, '--', *drop, 'env', '-i', *[f'{k}={v}' for k, v in env.items()], *inner]


def delete_tree(path, expected):
    """Identity-verified, descriptor-relative delete of the worktree that never leaves its mount (see
    strict_cleanup.remove_verified). A seam: tests replace it."""
    if os.geteuid() == 0:
        strict_cleanup.remove_verified(path, expected)
    else:
        strict_cleanup.remove_privileged_tree(path, _privilege_prefix(), expected)


def cleanup_worktree(work, expected, leaked=0):
    """Delete a mutation worktree after its suite has run.

    The suite ran as PID 1 of private PID and mount namespaces that no longer exist, so no process of the
    suite is alive and none of its mounts is visible here: there is nothing to unmount, and this function
    never unmounts anything. It only (1) re-checks the worktree identity, (2) refuses if the host namespace
    nevertheless has a mount at or below it (that would be a leak from somewhere else, reported as a failure),
    and (3) deletes with the descriptor-relative, mount-id-checked remover. `leaked` is the number of mounts
    the suite left inside its own namespace, reported for information.

    Returns {'state': 'clean' | 'reclaimed' | 'FAILED', 'leaked_mounts': n, 'detail': str}.
    """
    try:
        strict_cleanup.check_identity(str(work), expected)
        live = strict_cleanup.mounts_under(str(work))
        if live:
            return {'state': 'FAILED', 'leaked_mounts': leaked,
                    'detail': f'mount(s) are present below {work} in the host namespace: {live}; nothing was unmounted or deleted'}
        delete_tree(str(work), expected)
    except Exception as error:      # noqa: BLE001 - every failure is reported, none is hidden
        return {'state': 'FAILED', 'leaked_mounts': leaked, 'detail': f'{type(error).__name__}: {error}'}
    return {'state': 'reclaimed' if leaked else 'clean', 'leaked_mounts': leaked, 'detail': ''}


def reap_account(name):
    """Remove the account a mutated suite was given. The name is unique to that run, so nothing else can be using
    it, and the suite's processes are gone (their PID namespace has ended). Mutants of the account-lease module
    can leave it behind; that is reported, not hidden. Returns (existed, error or '')."""
    if subprocess.run(['getent', 'passwd', name], capture_output=True).returncode != 0:
        return False, ''
    removed = subprocess.run([*_privilege_prefix(), 'userdel', name], capture_output=True, text=True)
    if removed.returncode != 0 or subprocess.run(['getent', 'passwd', name], capture_output=True).returncode == 0:
        return True, f'account {name} could not be removed: {removed.stderr.strip() or "it still exists"}'
    return True, ''


def discard_unidentified(work):
    """The worktree was just created and nothing was put in it, but its identity could not be recorded.
    An empty directory can be removed safely with rmdir; anything else is reported, never deleted."""
    try:
        os.rmdir(work)
    except Exception as error:      # noqa: BLE001
        return {'state': 'FAILED', 'leaked_mounts': 0,
                'detail': f'identity of {work} could not be recorded and it could not be removed: {type(error).__name__}: {error}'}
    return {'state': 'clean', 'leaked_mounts': 0, 'detail': ''}


def run_one(item, keep=False, suite=None, parent=None):
    ident, finding, rel, old, new = item
    source = ROOT / rel
    text = source.read_text()
    if text.count(old) != 1:
        return {'id': ident, 'finding': finding, 'file': rel, 'result': 'INVALID', 'detail': f'snippet occurs {text.count(old)} times'}
    return _execute({'id': ident, 'finding': finding, 'file': rel}, rel, text.replace(old, new, 1), suite, parent)


def baseline(suite=None, parent=None):
    """The control run: the UNMUTATED suite, in the same copy, namespaces and environment as every mutation.
    A mutation can only be called caught if this passes, because only then does a failure belong to the
    mutation. Returns 'PASSED' or 'FAILED' in result['baseline']."""
    result = _execute({'id': 'baseline', 'finding': 'unmutated control run', 'file': '-'}, None, None, suite, parent)
    result['baseline'] = 'PASSED' if result['result'] == 'MISSED' else 'FAILED'      # the suite passing is the goal here
    return result


def _execute(result, rel, mutated_text, suite, parent):
    if not isolation_available():
        return {**result, 'result': 'ERROR', 'cleanup': 'not started',
                'leaked_mounts': 0, 'detail': 'private PID/mount namespaces or the openat2 deletion boundary are '
                                              'unavailable (need root or passwordless sudo, unshare, Linux >= 5.8); '
                                              'the mutated suite was not started'}
    work = Path(tempfile.mkdtemp(prefix='mut-', dir=parent))
    # From here on the worktree exists: every step that follows, including recording its identity, is inside
    # the guard, and the guard always ends in an explicit cleanup outcome.
    expected = None
    report_dir = report_identity = None
    leaked = 0
    account = 'ocm' + secrets.token_hex(5)                 # this run's own test account (see test_opencode_runtime.AGENT)
    try:
        expected = strict_cleanup.identity(str(work))      # identity captured at creation
        os.chmod(work, 0o755)                              # the unprivileged test user must be able to traverse it
        scratch = work / 'tmp'                             # every temporary tree the mutated suite makes lives here
        scratch.mkdir(mode=0o755)
        os.chmod(scratch, 0o755)
        report_dir = Path(tempfile.mkdtemp(prefix='mut-report-', dir=parent))
        report_identity = strict_cleanup.identity(str(report_dir))
        for tree in COPY_TREES:
            shutil.copytree(ROOT / tree, work / tree, ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
        for name in COPY_FILES:
            shutil.copyfile(ROOT / name, work / name)
        if rel is not None:
            (work / rel).write_text(mutated_text)
        env = {**os.environ, 'OPENCODE_SKIP_E2E': '1', 'TMPDIR': str(scratch), 'OPENCODE_TEST_ACCOUNT': account}
        report = report_dir / 'report.json'
        done = subprocess.run(isolated_command(work, report, suite or SUITE, env), capture_output=True, text=True,
                              env=env, timeout=SUITE_TIMEOUT + 120)
        try:
            seen = json.loads(report.read_text())            # untrusted telemetry: only integers are used
            leaked = int(seen['leaked_mounts']) if isinstance(seen.get('leaked_mounts'), int) else 0
            timed_out = seen.get('timed_out') is True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # no usable report means the wrapper (and so the isolation) did not run to completion: whatever the exit
            # status says, it is not a verdict about the mutation
            raise RuntimeError(f'the namespace wrapper produced no usable report (exit status {done.returncode}): '
                               f'{(done.stderr or "")[-300:].strip()}') from None
        failing = [line for line in done.stderr.splitlines() if line.startswith(('FAIL:', 'ERROR:'))]
        ran = [line for line in done.stderr.splitlines() if line.startswith('Ran ')]
        result['tests_run'] = int(ran[-1].split()[1]) if ran else 0
        if timed_out:
            result.update({'result': 'TIMEOUT', 'detail': 'suite hung under the mutation'})
        else:
            result.update({'result': 'CAUGHT' if done.returncode else 'MISSED', 'failing_tests': len(failing),
                           'first_failure': failing[0][:140] if failing else ''})
    except subprocess.TimeoutExpired:
        result.update({'result': 'TIMEOUT', 'detail': 'suite hung under the mutation (outer timeout)'})
    except Exception as error:      # noqa: BLE001 - a worker error must not abort the run or skip cleanup
        result.update({'result': 'ERROR', 'detail': f'{type(error).__name__}: {error}'})
    finally:
        outcome = discard_unidentified(work) if expected is None else cleanup_worktree(work, expected, leaked)
        left, account_error = reap_account(account)
        if account_error:
            outcome = {'state': 'FAILED', 'leaked_mounts': outcome['leaked_mounts'],
                       'detail': (outcome['detail'] + ' | ' if outcome['detail'] else '') + account_error}
        result['account_left_behind'] = left
        if report_dir is not None:
            try:
                strict_cleanup.remove_verified(str(report_dir), report_identity)
            except Exception as error:      # noqa: BLE001
                outcome = {'state': 'FAILED', 'leaked_mounts': outcome['leaked_mounts'],
                           'detail': (outcome['detail'] + ' | ' if outcome['detail'] else '')
                           + f'report directory: {type(error).__name__}: {error}'}
    result['cleanup'] = outcome['state']
    result['leaked_mounts'] = outcome['leaked_mounts']
    if outcome['state'] == 'FAILED':
        # a worktree or mount that could not be removed is a failure of the run, whatever the suite said
        result['suite_result'] = result.get('result')
        result['result'] = 'CLEANUP_FAILED'
        result['detail'] = (result.get('detail', '') + ' | ' if result.get('detail') else '') + outcome['detail']
    return result


def summarize(results):
    keys = ('CAUGHT', 'MISSED', 'INVALID', 'TIMEOUT', 'ERROR', 'CLEANUP_FAILED')
    return {k: sum(1 for r in results if r['result'] == k) for k in keys}


def exit_code(summary):
    return 1 if (summary['MISSED'] or summary['INVALID'] or summary['TIMEOUT'] or summary['ERROR']
                 or summary['CLEANUP_FAILED']) else 0


def run_all(items, workers=4, suite=None, parent=None, control=True):
    """Run the control (unmutated) suite first, then the mutations. If the control does not pass, no mutation
    is run and no verdict is produced: a failure under a mutation would not prove the mutation was caught.
    Returns (control_result or None, results)."""
    control_result = None
    if control:
        control_result = baseline(suite=suite, parent=parent)
        if control_result['baseline'] != 'PASSED' or control_result['cleanup'] == 'FAILED':
            return control_result, []
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda item: run_one(item, suite=suite, parent=parent), items))
    return control_result, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--json')
    parser.add_argument('--no-control', action='store_true',
                        help='skip the unmutated control run (development only; the verdicts are then NOT evidence)')
    args = parser.parse_args()
    items = [m for m in MUTATIONS if not args.only or m[0] in args.only]
    control, results = run_all(items, workers=args.workers, control=not args.no_control)
    if control is not None:
        print(f"control run (unmutated): {control['baseline']}, {control.get('tests_run', 0)} tests, cleanup {control['cleanup']}")
        if control['baseline'] != 'PASSED' or control['cleanup'] == 'FAILED':
            print('the unmutated suite does not pass in the isolated copy; no mutation was run and no verdict is valid')
            print(f"  {control.get('first_failure') or control.get('detail', '')}")
            if args.json:
                Path(args.json).write_text(json.dumps({'control': control, 'results': []}, indent=2))
            return 2
    else:
        print('WARNING: no control run; these verdicts are not evidence')
    for r in results:
        note = ''
        if r.get('leaked_mounts'):
            note += f"  leaked {r['leaked_mounts']} mount(s), cleanup {r['cleanup']}"
        if r['result'] in ('CLEANUP_FAILED', 'ERROR'):
            note += f"  {r['detail']}"
        print(f"{r['result']:14} {r['id']:7} {r['finding']}  [{r.get('failing_tests', '-')} failing]{note}")
    if args.json:
        Path(args.json).write_text(json.dumps({'control': control, 'results': results}, indent=2))
    summary = summarize(results)
    print(summary)
    return exit_code(summary)


if __name__ == '__main__':
    sys.exit(main())
