#!/usr/bin/env python3
"""Mutants of the production OpenCode controls, applied one per disposable job (opencode_validation v1.0).

    mutants.py list                                    JSON list of inventory IDs (the workflow matrix)
    mutants.py apply ID                                apply one mutant to the working tree; exit 3 if invalid
    mutants.py control --result R.json --out V.json    verdict on the unmutated control run
    mutants.py judge ID --control R0.json [--result R.json] [--timed-out] [--apply-failed] --out V.json
    mutants.py aggregate --control V0.json --dir DIR --out SUMMARY.json

Each entry is (id, weakened control, file, exact snippet, replacement). The snippet must occur exactly once,
the mutated file must compile, and the change must be exactly the intended one.

Verdicts: CAUGHT only when the suite ran completely, discovered exactly the control's test IDs, had no
infrastructure error and no skip, and at least one named test failed in its own body. A suite that passes is
MISSED. A changed test universe, a skip or an invalid mutant is INVALID; a crash, a missing report or an error
outside a test body is ERROR; a run that hit the time limit is TIMEOUT. Only CAUGHT counts.

Run identity (PR, head, base, run, attempt, inventory, manifests, validator) comes from the environment set
by the workflow; the aggregate rejects any result whose identity differs. Not collected by unittest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import suite  # noqa: E402

S = '.github/scripts/'
WORKFLOW = ROOT / '.github/workflows/opencode-security-validation.yml'
VERDICTS = ('CAUGHT', 'MISSED', 'INVALID', 'ERROR', 'TIMEOUT')
IDENTITY_KEYS = ('pr', 'head_sha', 'base_sha', 'run_id', 'run_attempt', 'inventory_sha256', 'required_sha256',
                 'allowed_skips_sha256', 'validator_sha256')

MUTATIONS = [
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
    ('P2-4a', 'oversized files reach the model (both size checks removed)', '.github/scripts/opencode_sandbox.py', "        if size > MAX_FILE_BYTES:\n            return 'excluded', 'larger than the per-file limit'\n        with open(path, 'rb') as handle:\n            data = handle.read(MAX_FILE_BYTES + 1)\n    except OSError:\n        return 'excluded', 'unreadable'\n    if len(data) > MAX_FILE_BYTES:\n        return 'excluded', 'larger than the per-file limit'\n", "        with open(path, 'rb') as handle:\n            data = handle.read()\n    except OSError:\n        return 'excluded', 'unreadable'\n"),
    ('P1-2h', 'collector follows a swapped work tree', S + 'opencode_collect.py', "if os.path.islink(worktree) or os.path.realpath(worktree) != os.path.abspath(worktree):", 'if False:'),
    ('P1-2i', 'collector ignores agent-owned ancestors', S + 'opencode_collect.py', "if os.environ.get('OPENCODE_REQUIRE_ROOT_CHAIN') == '1' else []", 'if False else []'),
    ('P1-2j', 'read-only check accepts modifications', S + 'opencode_review.py', "if not (change.get('path') == 'REVIEW/diff.patch'", "if False and not (change.get('path') == 'REVIEW/diff.patch'"),
    ('P1-2k', 'reviewer config allows edits', S + 'opencode_sandbox.py', "'edit': 'deny' if mode in ('review', 'probe') else 'allow'", "'edit': 'allow'"),
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
    ('P2-3a', 'review inputs read from the live PR', S + 'opencode_review.py', "compare = f'repos/{repo}/compare/{base_sha}...{head_sha}'", "compare = f'repos/{repo}/pulls/{number}'"),
    ('P2-3b', 'stale-on-arrival not detected', S + 'opencode_review.py', "stale_on_arrival = (head.get('sha') != sha or pr_base.get('sha') != base_sha\n                        or pr.get('state') != 'open' or bool(pr.get('merged')))", 'stale_on_arrival = False'),
    ('P2-3c', 'base change does not stale a review', S + 'opencode_review.py', "and (base_sha is None or marker.get('base_commit') == base_sha))", ')'),
    ('P2-3d', 'quoted paths not decoded', S + 'opencode_review.py', "    if not (len(token) >= 2 and token[0] == '\"' and token[-1] == '\"'):\n        return token", '    return token'),
    ('P2-3e', 'diff/API path mismatch tolerated', S + 'opencode_review.py', 'if not named <= api_names:', 'if False:'),
    ('P2-3f', 'truncated file list tolerated', S + 'opencode_review.py', 'or len(files) >= 300:', 'or False:'),
    ('P2-3g', 'stale_on_arrival ignored in status', S + 'opencode_review.py', "and not marker.get('stale_on_arrival')", ''),
    ('P2-4b', 'NUL-containing files reach the model', S + 'opencode_sandbox.py', "    if b'\\0' in data:\n        return 'excluded', 'binary content'", '    pass'),
    ('P2-4c', 'non-UTF-8 files reach the model', S + 'opencode_sandbox.py', "    except UnicodeDecodeError:\n        return 'excluded', 'not valid UTF-8'", "    except UnicodeDecodeError:\n        return 'ok', ''"),
    ('P2-4d', 'secret hits ignored', S + 'opencode_sandbox.py', '    if secret_hits:', '    if False:'),
    ('P2-4e', 'secret scan keeps a leading word boundary', S + 'opencode_sandbox.py', 'gh[pousr]_[A-Za-z0-9]{30,}|', r'\\bgh[pousr]_[A-Za-z0-9]{30,}|'),
    ('P2-4f', 'total size unbounded', S + 'opencode_sandbox.py', 'or total > MAX_TOTAL_BYTES:', 'or False:'),
    ('R-1', 'model eligibility ignores terms', S + 'opencode_router.py', "meta.get('terms_verified') is not True or not (", 'False or not ('),
    ('R-2', 'reviewer may equal implementer', S + 'opencode_review.py', "if policy['require_distinct_models'] and model in", 'if False and model in'),
    ('R-3', 'reviewer session reuse allowed', S + 'opencode_review.py', "if reviewer_session in {r['session']", "if False and reviewer_session in {r['session']"),
    ('R-4', 'review posted as approval', S + 'opencode_review.py', "'event': 'COMMENT'", "'event': 'APPROVE'"),
    ('R-5', 'manifest schema unchecked', S + 'opencode_publish.py', 'set(manifest) != TOP_KEYS', 'False'),
    ('R-6', 'generation failure still publishes', S + 'opencode_publish.py', "if env.get('GENERATION_STATUS') != 'success':", 'if False:'),
    ('R-7', 'forged baseline accepted', S + 'opencode_publish.py', 'if base != authorized:', 'if False:'),
    ('R-8', 'gate accepts any commenter id', S + 'opencode_gate.py', "if not is_bot and user.get('id') not in allowed_ids:", 'if False:'),
    ('G-1', 'agent processes are not reaped after the run', S + 'opencode_guard.py', "    survivor = reap_agent_processes()                  #", "    survivor = ''                  #"),
    ('G-2', 'a surviving agent process does not fail the run', S + 'opencode_guard.py', "    if survivor:\n        status, reason = 'failed', survivor", "    if False:\n        status, reason = 'failed', survivor"),
    ('G-3', 'the reap never kills the agent processes', S + 'opencode_guard.py', "        subprocess.run(['sudo', '-n', 'pkill', '-KILL', '-u', AGENT_USER], check=False, capture_output=True)\n        left", "        left"),
    ('G-4', 'the survivor check is not consulted', S + 'opencode_guard.py', "        if left.returncode == 1:                     # pgrep: no process matched", "        if True:                     # pgrep: no process matched"),
    ('G-5', 'a process check that cannot run counts as none left', S + 'opencode_guard.py', "            return f'agent_process_check_failed:{left.returncode}'", "            return ''"),
    ('G-6', 'the survivor check runs without sudo and can miss hidden processes', S + 'opencode_guard.py', "left = subprocess.run(['sudo', '-n', 'pgrep', '-u', AGENT_USER]", "left = subprocess.run(['pgrep', '-u', AGENT_USER]"),
]


class InvalidMutant(Exception):
    pass


def inventory() -> dict:
    found = {}
    for entry in MUTATIONS:
        if entry[0] in found:
            raise InvalidMutant(f'duplicate inventory id {entry[0]}')
        found[entry[0]] = entry
    return found


def validator_sha256() -> str:
    """One hash over every validator file and the workflow that runs them."""
    digest = hashlib.sha256()
    for path in sorted(HERE.glob('*.py')) + sorted(HERE.glob('*.txt')) + [WORKFLOW]:
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0' + suite.sha256(path).encode() + b'\n')
    return digest.hexdigest()


def identity() -> dict:
    env = os.environ
    return {'pr': env.get('VALIDATION_PR', ''), 'head_sha': env.get('VALIDATION_HEAD_SHA', ''),
            'base_sha': env.get('VALIDATION_BASE_SHA', ''), 'run_id': env.get('GITHUB_RUN_ID', ''),
            'run_attempt': env.get('GITHUB_RUN_ATTEMPT', ''), 'inventory_sha256': suite.sha256(Path(__file__)),
            'required_sha256': suite.sha256(suite.REQUIRED), 'allowed_skips_sha256': suite.sha256(suite.ALLOWED_SKIPS),
            'validator_sha256': validator_sha256()}


def apply(ident: str, root: Path = ROOT) -> dict:
    entries = inventory()
    if ident not in entries:
        raise InvalidMutant(f'unknown mutant {ident}')
    _, finding, rel, old, new = entries[ident]
    path = root / rel
    text = path.read_text()
    if text.count(old) != 1:
        raise InvalidMutant(f'snippet occurs {text.count(old)} times in {rel}')
    mutated = text.replace(old, new, 1)
    if mutated == text:
        raise InvalidMutant('the replacement changes nothing')
    start = text.index(old)
    if mutated[:start] != text[:start] or mutated[start + len(new):] != text[start + len(old):]:
        raise InvalidMutant('the change is not exactly the intended one')
    try:
        compile(mutated, rel, 'exec')              # syntax check only; nothing is written
    except (SyntaxError, ValueError) as error:
        raise InvalidMutant(f'mutated {rel} does not compile: {error}') from None
    path.write_text(mutated)
    return {'id': ident, 'finding': finding, 'file': rel, 'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'after_sha256': hashlib.sha256(mutated.encode()).hexdigest()}


def control_verdict(result: dict) -> dict:
    """The unmutated suite must run completely and pass everything, including every required privileged test."""
    problems = [f'infrastructure error: {e}' for e in result['infra_errors']]
    if len(result['ids']) < 50:
        problems.append(f'only {len(result["ids"])} tests discovered')
    for test_id in result['ids']:
        record = result['outcomes'].get(test_id, {'outcome': 'missing', 'detail': ''})
        if record['outcome'] != 'pass':
            problems.append(f'{test_id}: {record["outcome"]} {record["detail"]}'.strip())
    required = suite.manifest(suite.REQUIRED)
    problems += [f'required privileged test {i} absent from the validated suite' for i in required
                 if i.split('.')[0].startswith('test_opencode_') and i not in result['ids']]
    if not result['environment']['disposable'] or not result['environment']['opencode_binary_present']:
        problems.append(f'control environment incomplete: {result["environment"]}')
    return {'kind': 'control', 'verdict': 'PASSED' if not problems else 'FAILED', 'problems': problems,
            'tests': len(result['ids']), 'ids_sha256': hashlib.sha256('\n'.join(result['ids']).encode()).hexdigest(),
            'identity': identity()}


def judge(ident: str, control: dict, result: dict | None, timed_out=False, apply_error=None, applied=None) -> dict:
    verdict, detail, caught_by = None, '', []
    if apply_error:
        verdict, detail = 'INVALID', apply_error
    elif timed_out:
        verdict, detail = 'TIMEOUT', 'the mutated suite hit the time limit'
    elif result is None:
        verdict, detail = 'ERROR', 'no usable suite report'
    elif result['infra_errors']:
        verdict, detail = 'ERROR', '; '.join(result['infra_errors'])[:500]
    elif set(result['ids']) != set(control['ids']) or len(result['ids']) != len(control['ids']):
        verdict, detail = 'INVALID', 'the discovered test IDs differ from the control run'
    else:
        outcomes = [(i, result['outcomes'].get(i)) for i in result['ids']]
        odd = [i for i, r in outcomes if r is None or r['outcome'] in ('skip', 'xpass', 'xfail')]
        outside = [i for i, r in outcomes if r and r['outcome'] in ('fail', 'error') and not r['in_body']]
        caught_by = [i for i, r in outcomes if r and r['outcome'] in ('fail', 'error') and r['in_body']]
        if odd:
            verdict, detail = 'INVALID', f'skipped, missing or expected-failure outcome: {odd[:5]}'
        elif outside:
            verdict, detail = 'ERROR', f'failure outside a test body: {outside[:5]}'
        elif caught_by:
            verdict = 'CAUGHT'
        else:
            verdict, detail = 'MISSED', 'every test passed under the mutation'
    return {'kind': 'mutant', 'id': ident, 'verdict': verdict, 'detail': detail, 'caught_by': caught_by,
            'applied': applied, 'identity': identity()}


def aggregate(control: dict, results: list[dict]) -> dict:
    expected = identity()
    problems = []
    if control.get('kind') != 'control' or control.get('verdict') != 'PASSED':
        problems.append('the control run did not pass: no mutant verdict is valid')
    if control.get('identity') != expected:
        problems.append('the control run has a different run identity')
    seen = {}
    for record in results:
        ident = record.get('id')
        if record.get('kind') != 'mutant' or record.get('verdict') not in VERDICTS or not isinstance(ident, str):
            problems.append(f'malformed result: {str(record)[:200]}')
            continue
        if ident in seen:
            problems.append(f'duplicate result for {ident}')
        seen[ident] = record
        if record.get('identity') != expected:
            problems.append(f'{ident}: run identity differs from this run')
    wanted = set(inventory())
    problems += [f'missing result for {i}' for i in sorted(wanted - set(seen))]
    problems += [f'result for an id outside the inventory: {i}' for i in sorted(set(seen) - wanted)]
    counts = {v: sum(1 for r in seen.values() if r['verdict'] == v) for v in VERDICTS}
    problems += [f'{i}: {r["verdict"]} {r["detail"]}'.strip() for i, r in sorted(seen.items()) if r['verdict'] != 'CAUGHT']
    return {'kind': 'aggregate', 'passed': not problems, 'counts': counts, 'inventory': len(wanted),
            'problems': problems, 'identity': expected, 'control_tests': control.get('tests')}


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    p = sub.add_parser('apply')
    p.add_argument('id')
    p.add_argument('--out', type=Path)
    p = sub.add_parser('control')
    p.add_argument('--result', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    p = sub.add_parser('judge')
    p.add_argument('id')
    p.add_argument('--control', required=True, type=Path)
    p.add_argument('--result', type=Path)
    p.add_argument('--applied', type=Path)
    p.add_argument('--timed-out', action='store_true')
    p.add_argument('--apply-failed', action='store_true')
    p.add_argument('--out', required=True, type=Path)
    p = sub.add_parser('aggregate')
    p.add_argument('--control', required=True, type=Path)
    p.add_argument('--dir', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'list':
        print(json.dumps(sorted(inventory())))
        return 0
    if args.command == 'apply':
        try:
            record = apply(args.id)
        except InvalidMutant as error:
            print(f'INVALID {args.id}: {error}', file=sys.stderr)
            if args.out:
                args.out.write_text(json.dumps({'error': str(error)}))
            return 3
        if args.out:
            args.out.write_text(json.dumps(record))
        print(f'applied {args.id} to {record["file"]}')
        return 0
    if args.command == 'control':
        result = _load(args.result)
        verdict = control_verdict(result) if result else {'kind': 'control', 'verdict': 'FAILED',
                                                          'problems': ['no usable control report'], 'identity': identity()}
        args.out.write_text(json.dumps(verdict, indent=1))
        for problem in verdict['problems']:
            print(f'FAIL {problem}')
        print(f'control: {verdict["verdict"]}, {verdict.get("tests", 0)} tests')
        return 0 if verdict['verdict'] == 'PASSED' else 2
    if args.command == 'judge':
        control = _load(args.control)
        applied = _load(args.applied) if args.applied else None
        apply_error = (applied or {}).get('error') or ('the mutant could not be applied' if args.apply_failed else None)
        result = None if (args.timed_out or apply_error or not args.result) else _load(args.result)
        record = judge(args.id, control, result, timed_out=args.timed_out, apply_error=apply_error,
                       applied=None if apply_error else applied)
        args.out.write_text(json.dumps(record, indent=1))
        print(f'{record["verdict"]} {args.id} {record["detail"]} {record["caught_by"][:3]}')
        return 0
    control = _load(args.control) or {}
    results = [_load(p) or {'malformed': p.name} for p in sorted(args.dir.rglob('*.json'))]
    summary = aggregate(control, results)
    args.out.write_text(json.dumps(summary, indent=1))
    for problem in summary['problems']:
        print(f'FAIL {problem}')
    print(f'aggregate: {"PASSED" if summary["passed"] else "FAILED"}; {summary["counts"]} of {summary["inventory"]}; '
          f'identity {summary["identity"]}')
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
