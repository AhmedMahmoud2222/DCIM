#!/usr/bin/env python3
"""Runs a deployment test suite and records one outcome per test ID (opencode_validation v1.0).

    suite.py run --out RESULT.json [--pattern GLOB]     run the suite; exit 0 when it ran (whatever it found)
    suite.py check-ci RESULT.json                       CI gate: no infrastructure error, no failure, no skip
                                                        outside allowed_skips.txt, every privileged_required.txt
                                                        ID present and passed

A test counts as failed "in its body" only when the traceback passes through the test method itself; an
exception from setUp, setUpClass or a module import is an infrastructure error, not a detection.
Not collected by unittest (no test_ prefix).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
REQUIRED = HERE / 'privileged_required.txt'
ALLOWED_SKIPS = HERE / 'allowed_skips.txt'
SCHEMA = 1


def manifest(path: Path) -> list[str]:
    """Test IDs, one per line; '#' starts a comment. allowed_skips.txt lines are 'ID  # reason'."""
    ids = []
    for line in path.read_text().splitlines():
        entry = line.split('#', 1)[0].strip()
        if entry:
            ids.append(entry)
    return ids


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def is_placeholder(test) -> bool:
    """The loader's stand-ins for a module that failed to import, and unittest's holder for class/module setup."""
    return type(test).__name__ in ('_FailedTest', '_ErrorHolder', 'ModuleImportFailure')


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes: dict[str, dict] = {}
        self.infra: list[str] = []

    def _record(self, test, outcome, err=None):
        if is_placeholder(test):
            self.infra.append(f'{test.id()}: {self._first_line(err)}')
            return
        in_body = False
        if err is not None:
            name = getattr(test, '_testMethodName', None)
            in_body = any(frame.name == name for frame in traceback.extract_tb(err[2]))
        previous = self.outcomes.get(test.id())
        if previous and previous['outcome'] in ('fail', 'error'):
            return                                  # a failing subtest already decided this test
        self.outcomes[test.id()] = {'outcome': outcome, 'in_body': in_body, 'detail': self._first_line(err)}

    @staticmethod
    def _first_line(err) -> str:
        if err is None:
            return ''
        return ''.join(traceback.format_exception_only(err[0], err[1])).strip().splitlines()[-1][:300]

    def addSuccess(self, test):
        super().addSuccess(test)
        if test.id() not in self.outcomes:
            self._record(test, 'pass')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, 'fail', err)

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, 'error', err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if is_placeholder(test):
            self.infra.append(f'{test.id()}: skipped at class or module level: {reason}')
        else:
            self.outcomes[test.id()] = {'outcome': 'skip', 'in_body': False, 'detail': reason[:300]}

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, 'xfail')

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, 'xpass')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._record(test, 'fail' if failed else 'error', err)


def environment() -> dict:
    binary = TESTS.parents[1] / '.github/opencode/runtime/node_modules/opencode-linux-x64/bin/opencode'
    return {'uid': os.geteuid(), 'disposable': os.environ.get('OPENCODE_DISPOSABLE_HOST') == '1',
            'runner_environment': os.environ.get('RUNNER_ENVIRONMENT', ''),
            'python': sys.version.split()[0], 'opencode_binary_present': binary.exists()}


def run(pattern: str, out: Path) -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern=pattern, top_level_dir=str(TESTS))
    discovered = sorted(t.id() for t in flatten(suite) if not is_placeholder(t))
    quiet = os.environ.get('OPENCODE_SUITE_QUIET') == '1'
    stream = unittest.runner._WritelnDecorator(sys.stderr)
    result = RecordingResult(stream, descriptions=False, verbosity=1 if quiet else 2)
    suite.run(result)
    result.printErrors()
    infra =list(result.infra) + [f'loader: {e.strip().splitlines()[-1][:300]}' for e in loader.errors]
    infra += [f'{t.id()}: failed to import' for t in flatten(suite) if type(t).__name__ == '_FailedTest']
    missing = [i for i in discovered if i not in result.outcomes]
    infra += [f'{i}: discovered but never reported' for i in missing]
    counts = {}
    for record in result.outcomes.values():
        counts[record['outcome']] = counts.get(record['outcome'], 0) + 1
    out.write_text(json.dumps({
        'schema': SCHEMA, 'pattern': pattern, 'ids': discovered, 'outcomes': result.outcomes,
        'infra_errors': sorted(set(infra)), 'counts': counts, 'environment': environment(),
        'required_sha256': sha256(REQUIRED), 'allowed_skips_sha256': sha256(ALLOWED_SKIPS),
    }, indent=1, sort_keys=True))
    return 0


def check_ci(result: dict) -> list[str]:
    """Problems that make a CI run unacceptable. Empty means: everything ran, every required privileged test passed."""
    problems = [f'infrastructure error: {e}' for e in result['infra_errors']]
    if not result['ids']:
        problems.append('no tests were discovered')
    allowed = set(manifest(ALLOWED_SKIPS))
    for test_id in result['ids']:
        record = result['outcomes'].get(test_id)
        if record is None:
            problems.append(f'{test_id}: no outcome')
        elif record['outcome'] == 'skip' and test_id not in allowed:
            problems.append(f'{test_id}: skipped ({record["detail"]})')
        elif record['outcome'] not in ('pass', 'skip'):
            problems.append(f'{test_id}: {record["outcome"]} ({record["detail"]})')
    for test_id in manifest(REQUIRED):
        record = result['outcomes'].get(test_id)
        if test_id not in result['ids']:
            problems.append(f'required privileged test {test_id} was not discovered')
        elif record is None or record['outcome'] != 'pass':
            problems.append(f'required privileged test {test_id} did not pass')
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run')
    runner.add_argument('--out', required=True, type=Path)
    runner.add_argument('--pattern', default='test_opencode_*.py')
    checker = sub.add_parser('check-ci')
    checker.add_argument('result', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'run':
        return run(args.pattern, args.out)
    result = json.loads(args.result.read_text())
    problems = check_ci(result)
    required = manifest(REQUIRED)
    passed = sum(1 for i in required if result['outcomes'].get(i, {}).get('outcome') == 'pass')
    skipped = sum(1 for r in result['outcomes'].values() if r['outcome'] == 'skip')
    for problem in problems:
        print(f'FAIL {problem}')
    print(f'tests: {len(result["ids"])} discovered, outcomes {result["counts"]}; environment {result["environment"]}')
    print(f'privileged required: {passed}/{len(required)} passed, {skipped} skipped; '
          f'manifest sha256 {result["required_sha256"]}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
