"""Tests of the OpenCode validation tooling and of the test-infrastructure scope (tests v1.0).

Covers tests/deployment/opencode_validation/ (suite recording, CI gate, mutant inventory, verdicts and the
aggregate), the policy of the two workflows that run them, and the complexity budget: the test tree contains
no custom host-safety machinery (process scanning or signalling, namespaces, account leasing, custom deletion).
"""
import ast
import copy
import io
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE / 'opencode_validation'))
suite = importlib.import_module('suite')
mutants = importlib.import_module('mutants')

VALIDATION = ROOT / '.github/workflows/opencode-security-validation.yml'
TOOLS = ROOT / '.github/workflows/deploy-tools-test.yml'
OPENCODE = ROOT / '.github/workflows/opencode.yml'


def run_inner(*cases):
    loaded = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in cases)
    result = suite.RecordingResult(unittest.runner._WritelnDecorator(io.StringIO()), False, 0)
    loaded.run(result)
    return result


class RecordingTests(unittest.TestCase):
    def test_body_failures_setup_errors_skips_and_subtests_are_told_apart(self):
        class Inner(unittest.TestCase):
            def test_pass(self):
                pass

            def test_body_assert(self):
                self.assertEqual(1, 2)

            def test_body_exception(self):
                raise ValueError('negative test detected the weakness')

            def test_skip(self):
                self.skipTest('no')

            def test_subtest(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        self.assertEqual(value, 1)

        class BrokenSetUp(unittest.TestCase):
            def setUp(self):
                raise RuntimeError('fixture broke')

            def test_x(self):
                pass

        result = run_inner(Inner, BrokenSetUp)
        got = {k.rsplit('.', 1)[1]: (v['outcome'], v['in_body']) for k, v in result.outcomes.items()}
        self.assertEqual(got, {'test_pass': ('pass', False), 'test_body_assert': ('fail', True),
                               'test_body_exception': ('error', True), 'test_skip': ('skip', False),
                               'test_subtest': ('fail', True), 'test_x': ('error', False)})
        self.assertEqual(result.infra, [])

    def test_class_setup_errors_and_class_skips_are_infrastructure_errors(self):
        class BrokenClass(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError('class fixture broke')

            def test_x(self):
                pass

        class SkippedClass(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest('prerequisite missing')

            def test_y(self):
                pass

        result = run_inner(BrokenClass, SkippedClass)
        self.assertEqual(len(result.infra), 2, result.infra)
        self.assertEqual(result.outcomes, {})


def record(ids, outcome='pass', **overrides):
    outcomes = {i: {'outcome': outcome, 'in_body': outcome in ('fail', 'error'), 'detail': ''} for i in ids}
    outcomes.update(overrides)
    return {'schema': 1, 'ids': list(ids), 'outcomes': outcomes, 'infra_errors': [], 'counts': {},
            'environment': {'disposable': True, 'opencode_binary_present': True},
            'required_sha256': 'r', 'allowed_skips_sha256': 'a'}


IDS = [f'test_opencode_x.T.test_{n:03d}' for n in range(60)]


class CiGateTests(unittest.TestCase):
    def setUp(self):
        self.required = suite.manifest(suite.REQUIRED)
        self.ids = IDS + self.required

    def test_a_complete_passing_run_is_accepted(self):
        self.assertEqual(suite.check_ci(record(self.ids)), [])

    def test_an_unlisted_skip_a_failure_or_an_infrastructure_error_is_rejected(self):
        skipped = record(self.ids, **{IDS[0]: {'outcome': 'skip', 'in_body': False, 'detail': 'no sudo'}})
        self.assertTrue(any('skipped' in p for p in suite.check_ci(skipped)))
        failed = record(self.ids, **{IDS[1]: {'outcome': 'fail', 'in_body': True, 'detail': 'x'}})
        self.assertTrue(any(': fail' in p for p in suite.check_ci(failed)))
        broken = record(self.ids)
        broken['infra_errors'] = ['setUpClass (m.C): boom']
        self.assertTrue(any('infrastructure' in p for p in suite.check_ci(broken)))

    def test_every_required_privileged_test_must_be_discovered_and_pass(self):
        missing = record(IDS + self.required[1:])
        self.assertTrue(any('was not discovered' in p for p in suite.check_ci(missing)))
        skipped = record(self.ids, **{self.required[0]: {'outcome': 'skip', 'in_body': False, 'detail': 'skip'}})
        self.assertTrue(any('did not pass' in p for p in suite.check_ci(skipped)))

    def test_the_manifests_are_frozen_and_name_real_tests(self):
        self.assertEqual(suite.manifest(suite.ALLOWED_SKIPS), [])
        self.assertGreaterEqual(len(self.required), 30)
        loader = unittest.TestLoader()
        for test_id in self.required:
            self.assertEqual(loader.loadTestsFromName(test_id).countTestCases(), 1, test_id)


class InventoryTests(unittest.TestCase):
    def test_every_mutant_targets_production_code_and_applies_exactly_once(self):
        entries = mutants.inventory()
        self.assertEqual(len(entries), 92)                 # changing the inventory size needs review
        for ident, (_, _, rel, old, new) in entries.items():
            self.assertTrue(rel.startswith('.github/scripts/opencode_'), ident)
            self.assertEqual((ROOT / rel).read_text().count(old), 1, ident)
            self.assertNotEqual(old, new, ident)

    def apply_in_copy(self, entry):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, entry[2])
            target.parent.mkdir(parents=True)
            target.write_text((ROOT / entry[2]).read_text())
            with mock.patch.object(mutants, 'MUTATIONS', [entry]):
                record_ = mutants.apply(entry[0], root=Path(tmp))
            return record_, target.read_text()

    def test_apply_changes_exactly_the_snippet(self):
        entry = mutants.MUTATIONS[0]
        record_, text = self.apply_in_copy(entry)
        original = (ROOT / entry[2]).read_text()
        self.assertEqual(text, original.replace(entry[3], entry[4], 1))
        self.assertNotEqual(record_['before_sha256'], record_['after_sha256'])

    def test_invalid_mutants_are_refused_and_change_nothing(self):
        base = mutants.MUTATIONS[0]
        for entry in ((*base[:3], 'no such snippet anywhere', 'x'), (*base[:4], base[3]), (*base[:4], 'def ((')):
            with self.assertRaises(mutants.InvalidMutant):
                self.apply_in_copy(entry)
        with self.assertRaises(mutants.InvalidMutant):
            mutants.apply('NO-SUCH-ID')


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.control = record(IDS)

    def verdict(self, result, **kwargs):
        return mutants.judge('P1-1a', self.control, result, **kwargs)['verdict']

    def test_caught_needs_a_named_test_failing_in_its_body(self):
        caught = record(IDS, **{IDS[3]: {'outcome': 'fail', 'in_body': True, 'detail': 'x'}})
        self.assertEqual(self.verdict(caught), 'CAUGHT')
        self.assertEqual(mutants.judge('P1-1a', self.control, caught)['caught_by'], [IDS[3]])
        self.assertEqual(self.verdict(record(IDS)), 'MISSED')

    def test_everything_else_is_not_caught(self):
        setup = record(IDS, **{IDS[3]: {'outcome': 'error', 'in_body': False, 'detail': 'setUp'}})
        self.assertEqual(self.verdict(setup), 'ERROR')
        infra = record(IDS)
        infra['infra_errors'] = ['import failed']
        self.assertEqual(self.verdict(infra), 'ERROR')
        self.assertEqual(self.verdict(record(IDS[:-1])), 'INVALID')                      # changed universe
        self.assertEqual(self.verdict(record(IDS + ['test_opencode_x.T.extra'])), 'INVALID')
        skip = record(IDS, **{IDS[0]: {'outcome': 'skip', 'in_body': False, 'detail': ''},
                              IDS[1]: {'outcome': 'fail', 'in_body': True, 'detail': ''}})
        self.assertEqual(self.verdict(skip), 'INVALID')
        self.assertEqual(self.verdict(None), 'ERROR')
        self.assertEqual(self.verdict(None, timed_out=True), 'TIMEOUT')
        self.assertEqual(self.verdict(None, apply_error='snippet occurs 0 times'), 'INVALID')

    def test_the_control_must_pass_completely_on_a_disposable_host(self):
        full = IDS + [i for i in suite.manifest(suite.REQUIRED) if i.startswith('test_opencode_')]
        self.assertEqual(mutants.control_verdict(record(full))['verdict'], 'PASSED')
        for broken in (record(full, **{IDS[0]: {'outcome': 'skip', 'in_body': False, 'detail': ''}}),
                       record(full, **{IDS[0]: {'outcome': 'fail', 'in_body': True, 'detail': ''}}),
                       record(IDS),                                   # a required privileged test is absent
                       record(full[:10])):
            self.assertEqual(mutants.control_verdict(broken)['verdict'], 'FAILED')
        local = record(full)
        local['environment'] = {'disposable': False, 'opencode_binary_present': True}
        self.assertEqual(mutants.control_verdict(local)['verdict'], 'FAILED')


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.control = {'kind': 'control', 'verdict': 'PASSED', 'identity': mutants.identity(), 'tests': 60}
        self.results = [{'kind': 'mutant', 'id': i, 'verdict': 'CAUGHT', 'detail': '', 'identity': mutants.identity()}
                        for i in mutants.inventory()]

    def test_all_caught_with_one_identity_passes(self):
        summary = mutants.aggregate(self.control, self.results)
        self.assertTrue(summary['passed'], summary['problems'])
        self.assertEqual(summary["counts"]["CAUGHT"], 92)

    def test_missing_duplicate_extra_malformed_mixed_or_uncaught_results_fail(self):
        def broken(change):
            results = copy.deepcopy(self.results)
            change(results)
            return mutants.aggregate(self.control, results)
        self.assertFalse(broken(lambda r: r.pop())['passed'])
        self.assertFalse(broken(lambda r: r.append(copy.deepcopy(r[0])))['passed'])
        self.assertFalse(broken(lambda r: r.append({**r[0], 'id': 'EXTRA'}))['passed'])
        self.assertFalse(broken(lambda r: r.append({'malformed': 'x.json'}))['passed'])
        self.assertFalse(broken(lambda r: r[0]['identity'].update(run_attempt='2'))['passed'])
        self.assertFalse(broken(lambda r: r[0].update(verdict='MISSED'))['passed'])

    def test_a_failed_or_foreign_control_invalidates_every_verdict(self):
        self.assertFalse(mutants.aggregate({**self.control, 'verdict': 'FAILED'}, self.results)['passed'])
        foreign = {**self.control, 'identity': {**mutants.identity(), 'head_sha': 'f' * 40}}
        self.assertFalse(mutants.aggregate(foreign, self.results)['passed'])

    def test_the_identity_binds_head_base_run_and_every_validator_file(self):
        with mock.patch.dict(os.environ, {'VALIDATION_HEAD_SHA': 'a' * 40, 'GITHUB_RUN_ATTEMPT': '1'}):
            first = mutants.identity()
        with mock.patch.dict(os.environ, {'VALIDATION_HEAD_SHA': 'b' * 40, 'GITHUB_RUN_ATTEMPT': '1'}):
            self.assertNotEqual(mutants.identity(), first)
        self.assertEqual(set(first), set(mutants.IDENTITY_KEYS))


def load(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def checkouts(workflow):
    for job in workflow['jobs'].values():
        for step in job.get('steps', []):
            if step.get('uses', '').startswith('actions/checkout@'):
                yield step


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.validation = load(VALIDATION)

    def test_triggers_are_label_or_dispatch_never_pull_request_target(self):
        self.assertEqual(self.validation['on'], {'pull_request': {'types': ['labeled']}, 'workflow_dispatch': ''})
        for path in (VALIDATION, TOOLS):
            self.assertNotIn('pull_request_target', path.read_text())

    def test_only_the_owner_on_a_hosted_runner_can_start_it_and_everything_waits_for_that(self):
        auth = self.validation['jobs']['authorize']
        self.assertIn("github.event.label.name == 'security-validation'", auth['if'])
        script = auth['steps'][0]['run']
        self.assertEqual(auth['steps'][0]['env']['SENDER'], '${{ github.event.sender.login }}')
        self.assertEqual(auth['steps'][0]['env']['OWNER'], '${{ github.repository_owner }}')
        self.assertIn('"${SENDER}" != "${OWNER}"', script)
        self.assertIn('github-hosted', script)
        jobs = self.validation['jobs']
        self.assertEqual(jobs['control']['needs'], 'authorize')
        self.assertEqual(jobs['mutant']['needs'], ['authorize', 'control'])
        self.assertIn("needs.authorize.result == 'success'", jobs['aggregate']['if'])

    def test_one_fresh_hosted_vm_per_mutant_and_the_exact_head(self):
        jobs = self.validation['jobs']
        for name, job in jobs.items():
            self.assertEqual(job['runs-on'], 'ubuntu-24.04', name)
            self.assertNotIn('container', job, name)
            self.assertNotIn('services', job, name)
        strategy = jobs['mutant']['strategy']
        self.assertEqual(strategy['fail-fast'], 'false')
        self.assertEqual(strategy['matrix'], {'id': '${{ fromJSON(needs.authorize.outputs.mutants) }}'})
        for step in checkouts(self.validation):
            self.assertEqual(step['with']['ref'], '${{ env.VALIDATION_HEAD_SHA }}')
        self.assertEqual(self.validation['env']['VALIDATION_HEAD_SHA'],
                         '${{ github.event.pull_request.head.sha || github.sha }}')
        self.assertNotIn('cache', VALIDATION.read_text().replace('cancel-in-progress', ''))

    def test_control_and_mutant_runners_install_the_same_pinned_test_dependencies(self):
        jobs = self.validation['jobs']
        prepared = [s['run'] for name in ('control', 'mutant') for s in jobs[name]['steps']
                    if s.get('name') == 'Prepare the disposable runner']
        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[0], prepared[1])                  # control and mutants run on identical setups
        self.assertIn('python3 -m pip install PyYAML==6.0.2', prepared[0])
        self.assertIn('PyYAML==6.0.2', TOOLS.read_text())          # the same pin as the required CI job

    def test_least_privilege_and_no_persisted_credentials(self):
        for path in (VALIDATION, TOOLS):
            workflow = load(path)
            self.assertEqual(workflow['permissions'], {}, path.name)
            for name, job in workflow['jobs'].items():
                self.assertEqual(job.get('permissions'), {'contents': 'read'}, f'{path.name}:{name}')
            text = path.read_text()
            for forbidden in ('secrets.', 'id-token', 'environment:', 'write'):
                self.assertNotIn(forbidden, text, f'{path.name}: {forbidden}')
        for path in (VALIDATION, TOOLS, OPENCODE):
            for step in checkouts(load(path)):
                self.assertEqual(step.get('with', {}).get('persist-credentials'), 'false', path.name)

    def test_ci_runs_every_privileged_test_and_fails_on_skips(self):
        job = load(TOOLS)['jobs']['deployment-tooling']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        steps = {s.get('name', ''): s for s in job['steps']}
        run = steps['Run the deployment tests, privileged OpenCode tests included (any skip fails)']
        self.assertEqual(run['env']['OPENCODE_DISPOSABLE_HOST'], '1')
        self.assertIn("suite.py run --pattern 'test_*.py'", run['run'])
        self.assertIn('suite.py check-ci', run['run'])
        self.assertIn('github-hosted', steps['Prepare the disposable runner and the pinned OpenCode binary']['run'])
        self.assertNotIn('OPENCODE_SKIP_E2E', TOOLS.read_text() + VALIDATION.read_text())


FORBIDDEN_IMPORTS = {'ctypes', 'strict_cleanup', 'account_lease', 'ns_run', 'mutation_check'}
FORBIDDEN_CALLS = {('os', 'kill'), ('os', 'killpg'), ('os', 'unshare'), ('os', 'setns'), ('os', 'pidfd_open'),
                   ('signal', 'pidfd_send_signal'), ('shutil', 'rmtree')}
FORBIDDEN_COMMANDS = {'unshare', 'nsenter', 'kill', 'pkill', 'killall', 'umount', 'userdel'}
RUNNERS = {'run', 'Popen', 'call', 'check_call', 'check_output', 'run_priv', 'as_agent'}


class ComplexityBudgetTests(unittest.TestCase):
    """No custom host-safety subsystem in the test tree: checked on imports and executable references."""

    def modules(self):
        return sorted(p for p in HERE.rglob('*.py') if 'node_modules' not in p.parts and 'fixtures' not in p.parts)

    def test_removed_harness_modules_are_gone(self):
        for name in ('strict_cleanup.py', 'account_lease.py', 'ns_run.py', 'mutation_check.py',
                     'test_strict_cleanup.py', 'test_account_lifecycle.py'):
            self.assertFalse((HERE / name).exists(), name)

    def test_no_forbidden_import_call_or_command(self):
        for path in self.modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                where = f'{path.relative_to(ROOT)}:{getattr(node, "lineno", "?")}'
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split('.')[0] for a in node.names} & FORBIDDEN_IMPORTS, where)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[0], FORBIDDEN_IMPORTS, where)
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    self.assertNotIn((node.value.id, node.attr), FORBIDDEN_CALLS, where)
                elif isinstance(node, ast.keyword) and node.arg == 'ignore_errors':
                    self.fail(f'{where}: errors of a removal are never ignored')
                elif isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                    if name in RUNNERS and node.args:
                        first = node.args[0]
                        command = first.elts[0] if isinstance(first, (ast.List, ast.Tuple)) and first.elts else first
                        if isinstance(command, ast.Constant):
                            self.assertNotIn(command.value, FORBIDDEN_COMMANDS, where)


if __name__ == '__main__':
    unittest.main()
