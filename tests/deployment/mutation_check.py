#!/usr/bin/env python3
"""Mutation inventory for the OpenCode security controls (mutation check v1.0).

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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
    ('P2-c5', 'tree remover deletes through a live mount', 'tests/deployment/test_opencode_runtime.py', "    if live:\n        raise MountLeak(f'refusing to delete", "    if False:\n        raise MountLeak(f'refusing to delete"),
    ('P2-c6', 'probe deletes the directory before unmounting', 'tests/deployment/test_opencode_runtime.py', "        for attempt in (['umount', target]", "        remove(probe)\n        for attempt in (['umount', target]"),
    ('P2-d1', 'mountinfo paths are not decoded', 'tests/deployment/test_opencode_runtime.py', "    return _MOUNTINFO_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)", "    return field"),
    ('P2-d2', 'mountinfo backslash escape not decoded', 'tests/deployment/test_opencode_runtime.py', "_MOUNTINFO_ESCAPE = re.compile(r'\\\\([0-7]{3})')", "_MOUNTINFO_ESCAPE = re.compile(r'\\\\(0[0-7]{2})')"),
    ('P2-d3', 'mount table compares only the first path word', 'tests/deployment/test_opencode_runtime.py', "    return [decode_mountinfo(line.split()[4]) for line in lines if line.strip()]", "    return [line.split()[4] for line in lines if line.strip()]"),
    ('P2-d4', 'mount prefix match ignores the separator', 'tests/deployment/test_opencode_runtime.py', "    prefix = path.rstrip('/') + '/'\n    return [point", "    prefix = path.rstrip('/')\n    return [point"),
    ('P2-t1', 'temp directory cleanup not registered at creation', 'tests/deployment/test_opencode_runtime.py', "    case.addCleanup(remove_unmounted_tree, path)\n    return path", "    return path"),
    ('P2-t2', 'tree remover swallows deletion errors', 'tests/deployment/test_opencode_runtime.py', "    rmtree(path)\n\n\ndef probe_mount_capability", "    try:\n        rmtree(path)\n    except OSError:\n        pass\n\n\ndef probe_mount_capability"),
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
    ('P2-4a', 'oversized files reach the model', S + 'opencode_sandbox.py', 'if size > MAX_FILE_BYTES:', 'if False:'),
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


def run_one(item, keep=False):
    ident, finding, rel, old, new = item
    source = ROOT / rel
    text = source.read_text()
    if text.count(old) != 1:
        return {'id': ident, 'finding': finding, 'file': rel, 'result': 'INVALID', 'detail': f'snippet occurs {text.count(old)} times'}
    work = Path(tempfile.mkdtemp(prefix='mut-'))
    try:
        shutil.copytree(ROOT / '.github', work / '.github', ignore=shutil.ignore_patterns('node_modules'))
        shutil.copytree(ROOT / 'tests', work / 'tests')
        (work / rel).write_text(text.replace(old, new, 1))
        env = {**os.environ, 'OPENCODE_SKIP_E2E': '1'}
        done = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/deployment', '-p', 'test_*.py'],
                              cwd=work, capture_output=True, text=True, env=env, timeout=400)
        failing = [line for line in done.stderr.splitlines() if line.startswith(('FAIL:', 'ERROR:'))]
        return {'id': ident, 'finding': finding, 'file': rel, 'result': 'CAUGHT' if done.returncode else 'MISSED',
                'failing_tests': len(failing), 'first_failure': failing[0][:140] if failing else ''}
    except subprocess.TimeoutExpired:
        return {'id': ident, 'finding': finding, 'file': rel, 'result': 'TIMEOUT', 'detail': 'suite hung under the mutation'}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--json')
    args = parser.parse_args()
    items = [m for m in MUTATIONS if not args.only or m[0] in args.only]
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run_one, items))
    for r in results:
        print(f"{r['result']:8} {r['id']:7} {r['finding']}  [{r.get('failing_tests', '-')} failing]")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    summary = {k: sum(1 for r in results if r['result'] == k) for k in ('CAUGHT', 'MISSED', 'INVALID', 'TIMEOUT')}
    print(summary)
    return 1 if summary['MISSED'] or summary['INVALID'] or summary['TIMEOUT'] else 0


if __name__ == '__main__':
    sys.exit(main())
