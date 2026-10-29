"""Adversarial regression tests for the Claude agent's trust and write boundaries (tests v1.0).

P1: executable agent configuration (.claude/, .mcp.json, hooks) is restored by the action from the
    PR base branch, so only the approved, protected default branch may be a base.
P2: every write is re-authorized against live PR state before it happens, so a PR that closes,
    merges, moves HEAD or loses its branch mid-run can never cause a branch to be created or moved.

The guard process tests run the real script against a scripted local GitHub API.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('test_claude_agent_helpers', HERE / 'test_claude_agent.py')
claude, guard = base.claude, base.guard
event, pr, decide, load_workflow = base.event, base.pr, base.decide, base.load_workflow
REPO, SHA, BASE_SHA, PROTECTED_MAIN, OWNER_ID = base.REPO, base.SHA, base.BASE_SHA, base.PROTECTED_MAIN, base.OWNER_ID
CHILD, OTHER = 'c' * 40, 'd' * 40
TARGET = {'repository': REPO, 'number': 7, 'head_ref': 'feature/x', 'authorized_sha': SHA}


def live_ref(sha=SHA, ref='feature/x'):
    return {'ref': f'refs/heads/{ref}', 'object': {'sha': sha}}


class BaseTrustTests(unittest.TestCase):
    """P1: untrusted base-branch configuration must never reach a credentialed run."""

    def test_only_the_approved_protected_default_branch_is_a_valid_base(self):
        self.assertTrue(decide(event('@claude fix'))[0])
        for base_ref in ('develop', 'release/1', 'main-evil', 'refs/heads/main', 'Main', 'main ', '', None,
                         'attacker/hooks'):
            ok, reason = decide(event('@claude fix'), pull=pr(base_ref=base_ref))
            self.assertFalse(ok, base_ref)
            self.assertIn('approved', reason)

    def test_unprotected_missing_or_malformed_base_protection_rejected(self):
        for branch in ({'name': 'main', 'protected': False}, {'name': 'main'}, {'name': 'main', 'protected': 'true'},
                       {'name': 'main', 'protected': 1}, {'name': 'other', 'protected': True}, {}, None, [], 'x'):
            self.assertFalse(decide(event('@claude fix'), base_branch=branch)[0], branch)
        self.assertFalse(claude.decide('issue_comment', event('@claude fix'), REPO, {OWNER_ID}, pr())[0])

    def test_default_branch_must_still_be_the_approved_base(self):
        self.assertFalse(decide(event('@claude fix', default_branch='develop'))[0])
        self.assertFalse(decide(event('@claude fix', default_branch=None))[0])

    def test_approved_base_constants_agree(self):
        self.assertEqual(claude.APPROVED_BASE_REF, guard.APPROVED_BASE_REF)
        self.assertEqual(claude.APPROVED_BASE_REF, 'main')

    def test_gate_fetches_live_base_protection_without_a_secret(self):
        gate = load_workflow()['jobs']['gate']
        self.assertIn('branches/main', (ROOT / '.github/workflows/claude-agent.yml').read_text())
        auth = next(s for s in gate['steps'] if s.get('id') == 'gate')
        self.assertIn('BASE_BRANCH_JSON_PATH', auth['env'])
        self.assertNotIn('secrets.', json.dumps(gate))


class WriteGuardLogicTests(unittest.TestCase):
    """P2: the live-state check that authorizes every write."""

    def check(self, pull=None, base=PROTECTED_MAIN, ref_obj=None, expected=SHA):
        guard.check_live(pr() if pull is None else pull, base, live_ref() if ref_obj is None else ref_obj,
                         TARGET, expected)

    def denied(self, **kwargs):
        with self.assertRaises(guard.Denied):
            self.check(**kwargs)

    def test_unchanged_live_state_is_allowed(self):
        self.check()

    def test_closed_or_merged_pr_denied(self):
        self.denied(pull=pr(state='closed'))
        self.denied(pull=pr(merged=True))
        self.denied(pull=pr(state='closed', merged=True))

    def test_head_change_denied(self):
        self.denied(pull=pr(sha=OTHER))
        self.denied(ref_obj=live_ref(sha=OTHER))
        self.denied(expected=CHILD)

    def test_head_branch_deleted_or_renamed_denied_so_it_is_never_recreated(self):
        self.denied(ref_obj={})
        self.denied(ref_obj=live_ref(ref='claude/pr-7-1'))
        self.denied(pull=pr(ref='claude/pr-7-1'))

    def test_retargeted_or_unprotected_base_denied(self):
        self.denied(pull=pr(base_ref='attacker/hooks'))
        self.denied(base={'name': 'main', 'protected': False})
        self.denied(base=None)

    def test_fork_or_wrong_pr_denied(self):
        self.denied(pull=pr(head_repo='fork/DCIM'))
        self.denied(pull=pr(base_repo='fork/DCIM'))
        self.denied(pull=pr(number=8))
        self.denied(pull=[])


class FakeGitHub(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        status, body = self.routes.get(self.path, (404, {'message': 'Not Found'}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class WriteGuardProcessTests(unittest.TestCase):
    """Runs the real guard process; exit code 2 from the hook is what blocks the write."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeGitHub)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state.json'
        self.set_live()

    def tearDown(self):
        self.tmp.cleanup()

    def set_live(self, pull=None, base=PROTECTED_MAIN, ref_obj=(200, None), extra=None):
        ref_status, ref_body = ref_obj
        FakeGitHub.routes = {
            f'/repos/{REPO}/pulls/7': (200, pr() if pull is None else pull),
            f'/repos/{REPO}/branches/main': (200, base),
            f'/repos/{REPO}/git/ref/heads/feature/x': (ref_status, live_ref() if ref_body is None else ref_body),
            **(extra or {}),
        }

    def run_guard(self, mode, stdin='{"tool_name": "mcp__github_file_ops__commit_files"}', env=None):
        guard_env = {'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                     'GITHUB_API_URL': f'http://127.0.0.1:{self.port}', 'CLAUDE_GUARD_TOKEN': 'x',
                     'CLAUDE_GUARD_REPOSITORY': REPO, 'CLAUDE_GUARD_NUMBER': '7',
                     'CLAUDE_GUARD_HEAD_REF': 'feature/x', 'CLAUDE_GUARD_AUTHORIZED_SHA': SHA,
                     'CLAUDE_GUARD_STATE': str(self.state), **(env or {})}
        return subprocess.run([sys.executable, '-I', str(ROOT / '.github/scripts/claude_write_guard.py'), mode],
                              input=stdin, env=guard_env, capture_output=True, text=True, timeout=60)

    def assertBlocked(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('claude write guard', result.stderr)

    def test_unchanged_pr_allows_the_write(self):
        self.assertEqual(self.run_guard('hook').returncode, 0)

    def test_pr_closed_or_merged_before_branch_creation_is_blocked(self):
        for pull in (pr(state='closed'), pr(state='closed', merged=True), pr(merged=True)):
            self.set_live(pull=pull)
            self.assertBlocked(self.run_guard('hook'))

    def test_deleted_head_branch_is_never_recreated(self):
        self.set_live(ref_obj=(404, {'message': 'Not Found'}))
        self.assertBlocked(self.run_guard('hook'))

    def test_head_changed_by_someone_else_is_blocked(self):
        self.set_live(pull=pr(sha=OTHER), ref_obj=(200, live_ref(sha=OTHER)))
        self.assertBlocked(self.run_guard('hook'))

    def test_retargeted_or_unprotected_base_is_blocked(self):
        self.set_live(pull=pr(base_ref='attacker/hooks'))
        self.assertBlocked(self.run_guard('hook'))
        self.set_live(base={'name': 'main', 'protected': False})
        self.assertBlocked(self.run_guard('hook'))

    def test_preflight_fails_closed_with_exit_1(self):
        self.assertEqual(self.run_guard('preflight', stdin='').returncode, 0)
        self.set_live(pull=pr(state='closed'))
        self.assertEqual(self.run_guard('preflight', stdin='').returncode, 1)

    def test_errors_and_malformed_input_block(self):
        self.assertBlocked(self.run_guard('hook', stdin='not json'))
        self.assertBlocked(self.run_guard('hook', stdin='[]'))
        self.assertBlocked(self.run_guard('hook', stdin='{"tool_name": "Bash"}'))
        self.assertBlocked(self.run_guard('hook', stdin='{"tool_name": 7}'))
        self.assertBlocked(self.run_guard('hook', env={'CLAUDE_GUARD_TOKEN': ''}))
        self.assertBlocked(self.run_guard('hook', env={'CLAUDE_GUARD_AUTHORIZED_SHA': 'abc'}))
        self.assertBlocked(self.run_guard('hook', env={'CLAUDE_GUARD_HEAD_REF': 'bad ref;rm'}))
        self.assertBlocked(self.run_guard('hook', env={'CLAUDE_GUARD_NUMBER': 'x'}))
        self.assertBlocked(self.run_guard('hook', env={'GITHUB_API_URL': 'http://127.0.0.1:9'}))  # unreachable
        FakeGitHub.routes[f'/repos/{REPO}/pulls/7'] = (500, {})
        self.assertBlocked(self.run_guard('hook'))
        self.assertEqual(self.run_guard('nonsense').returncode, 1)

    def test_our_own_commit_advances_the_expected_head(self):
        self.set_live(pull=pr(sha=CHILD), ref_obj=(200, live_ref(sha=CHILD)),
                      extra={f'/repos/{REPO}/git/commits/{CHILD}': (200, {'parents': [{'sha': SHA}]})})
        self.assertBlocked(self.run_guard('hook'))  # not yet recorded: head differs from the authorized one
        self.assertEqual(self.run_guard('record').returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())['expected_head'], CHILD)
        self.assertEqual(self.run_guard('hook').returncode, 0)
        self.assertEqual(self.run_guard('verify').returncode, 0)

    def test_foreign_head_change_locks_every_later_write(self):
        self.set_live(pull=pr(sha=OTHER), ref_obj=(200, live_ref(sha=OTHER)),
                      extra={f'/repos/{REPO}/git/commits/{OTHER}': (200, {'parents': [{'sha': BASE_SHA}]})})
        self.run_guard('record')
        self.assertTrue(json.loads(self.state.read_text())['locked'])
        self.set_live()  # even if the head returns to the authorized commit, the lock holds
        self.assertBlocked(self.run_guard('hook'))
        self.assertEqual(self.run_guard('verify').returncode, 1)

    def test_record_failure_locks_and_corrupt_state_blocks(self):
        self.set_live(extra={f'/repos/{REPO}/pulls/7': (500, {})})
        self.run_guard('record')
        self.assertTrue(json.loads(self.state.read_text())['locked'])
        self.state.write_text('{corrupt')
        self.set_live()
        self.assertBlocked(self.run_guard('hook'))
        self.assertEqual(self.run_guard('verify').returncode, 1)

    def test_state_defaults_to_runner_temp_and_requires_it(self):
        env = {'CLAUDE_GUARD_STATE': '', 'RUNNER_TEMP': self.tmp.name}
        self.assertEqual(self.run_guard('hook', env=env).returncode, 0)
        self.set_live(pull=pr(sha=CHILD), ref_obj=(200, live_ref(sha=CHILD)),
                      extra={f'/repos/{REPO}/git/commits/{CHILD}': (200, {'parents': [{'sha': SHA}]})})
        self.run_guard('record', env=env)
        self.assertEqual(json.loads((Path(self.tmp.name) / 'claude_guard_state.json').read_text())['expected_head'], CHILD)
        self.assertBlocked(self.run_guard('hook', env={'CLAUDE_GUARD_STATE': '', 'RUNNER_TEMP': ''}))

    def test_verify_passes_when_nothing_was_written(self):
        self.assertEqual(self.run_guard('verify', stdin='').returncode, 0)


class WorkflowWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.wf = load_workflow()
        self.job = self.wf['jobs']['claude']
        self.steps = self.job['steps']
        self.action = next(s for s in self.steps if s.get('id') == 'claude')

    def index(self, predicate):
        return next(i for i, s in enumerate(self.steps) if predicate(s))

    def test_closed_pr_branch_creation_path_aborts_on_invalid_prefix(self):
        prefix = self.action['with']['branch_prefix']
        # Same validation the pinned action applies before it creates any branch (branch.ts).
        valid = re.compile(r'^[a-zA-Z0-9@_][a-zA-Z0-9/_.#+,@()-]*$')
        for generated in (f'{prefix}pr-7-20261029-1200', f'{prefix}issue-7-20261029-1200'):
            self.assertIsNone(valid.match(generated), generated)
        self.assertNotIn('branch_name_template', self.action['with'])

    def test_every_write_tool_is_guarded_before_and_recorded_after(self):
        settings = json.loads(self.action['with']['settings'])
        for event_name, mode in (('PreToolUse', 'hook'), ('PostToolUse', 'record')):
            entry = settings['hooks'][event_name][0]
            for tool in ('mcp__github_file_ops__commit_files', 'mcp__github_file_ops__delete_files'):
                self.assertRegex(tool, '^(?:' + entry['matcher'] + ')$')
            command = entry['hooks'][0]['command']
            self.assertTrue(command.startswith('python3 "${{ runner.temp }}/claude_write_guard.py"'), command)
            self.assertTrue(command.endswith(f' {mode}'))
            self.assertLess(entry['hooks'][0]['timeout'], 120)

    def test_guard_comes_from_the_workflow_commit_not_the_pr_checkout(self):
        trusted = self.index(lambda s: s.get('with', {}).get('path') == 'trusted-guard')
        install = self.index(lambda s: 'install -m 0444 trusted-guard' in s.get('run', ''))
        pr_head = self.index(lambda s: s.get('with', {}).get('ref') == '${{ needs.gate.outputs.authorized_sha }}')
        run_claude = self.index(lambda s: s.get('id') == 'claude')
        self.assertEqual(self.steps[trusted]['with']['ref'], '${{ github.sha }}')
        self.assertTrue(trusted < install < pr_head < run_claude)
        self.assertIn('${RUNNER_TEMP}/claude_write_guard.py', self.steps[install]['run'])
        self.assertIn('rm -rf trusted-guard', self.steps[install]['run'])
        # No step may execute the guard from the (PR-controlled) workspace.
        for step in self.steps:
            run = step.get('run', '').replace('trusted-guard/.github/scripts/claude_write_guard.py', '')
            self.assertNotIn('.github/scripts/claude_write_guard.py', run)

    def test_live_revalidation_before_and_after_checkout_and_final_verify(self):
        pre = [i for i, s in enumerate(self.steps) if s.get('run', '').endswith('claude_write_guard.py" preflight')]
        pr_head = self.index(lambda s: s.get('with', {}).get('ref') == '${{ needs.gate.outputs.authorized_sha }}')
        run_claude = self.index(lambda s: s.get('id') == 'claude')
        self.assertEqual(len(pre), 2)
        self.assertLess(pre[0], pr_head)
        self.assertTrue(pr_head < pre[1] < run_claude)
        verify = self.steps[self.index(lambda s: s.get('run', '').endswith('claude_write_guard.py" verify'))]
        self.assertEqual(verify['if'], 'always()')
        self.assertGreater(self.steps.index(verify), run_claude)

    def test_guard_environment_comes_from_gate_outputs_only(self):
        env = self.job['env']
        self.assertEqual(env['CLAUDE_GUARD_NUMBER'], '${{ needs.gate.outputs.number }}')
        self.assertEqual(env['CLAUDE_GUARD_AUTHORIZED_SHA'], '${{ needs.gate.outputs.authorized_sha }}')
        self.assertEqual(env['CLAUDE_GUARD_HEAD_REF'], '${{ needs.gate.outputs.head_ref }}')
        self.assertEqual(env['CLAUDE_GUARD_TOKEN'], '${{ github.token }}')

    def test_job_env_uses_only_contexts_available_at_job_level(self):
        # actionlint: `runner` is not available in jobs.<id>.env; the guard derives its state path
        # from RUNNER_TEMP at runtime instead.
        self.assertNotIn('runner.', json.dumps(self.job['env']))
        self.assertNotIn('CLAUDE_GUARD_STATE', self.job['env'])

    def test_token_scopes_and_opencode_isolation_unchanged(self):
        self.assertEqual(self.job['permissions'],
                         {'contents': 'write', 'pull-requests': 'write', 'issues': 'write', 'actions': 'read'})
        self.assertEqual(self.wf['jobs']['gate']['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        self.assertEqual(self.wf['permissions'], {})
        self.assertEqual(self.action['with']['allowed_bots'], '')
        self.assertIs(self.action['with']['use_commit_signing'], True)
        self.assertNotIn('--allowedTools', self.action['with']['claude_args'])
        text = (ROOT / '.github/workflows/claude-agent.yml').read_text()
        self.assertNotIn('OPENCODE', text)
        self.assertNotIn('opencode_gate', text)


if __name__ == '__main__':
    unittest.main()
