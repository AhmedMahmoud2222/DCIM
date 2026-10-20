"""OpenCode sandbox, guard, proxy and catalog tests (tests v1.0). No network, no sudo."""
import copy
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sandbox = load('opencode_sandbox')
guard = load('opencode_guard')
proxy = load('opencode_proxy')
catalog = load('opencode_catalog')
router = load('opencode_router')
FAKE_KEY = '-----BEGIN RSA ' + 'PRIVATE KEY-----'
FAKE_TOKEN = 'ghp_' + 'A1b2C3d4' * 5


def make_repo(tmp, files):
    repo = os.path.join(tmp, 'repo')
    os.makedirs(repo, exist_ok=True)
    subprocess.run(['git', '-C', repo, 'init', '-q'], check=True)
    for rel, body in files.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if body is None:
            os.symlink('/etc/passwd', full)
        else:
            Path(full).write_text(body)
    subprocess.run(['git', '-C', repo, 'add', '-A', '-f'], check=True)
    return repo


class ConfidentialityBoundaryTests(unittest.TestCase):
    def prepare(self, files, allowlist=frozenset()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        repo = make_repo(tmp, files)
        return tmp, sandbox.prepare(repo, os.path.join(tmp, 'w'), os.path.join(tmp, 's'), 'a' * 40, set(allowlist))

    def test_credential_like_files_and_untrusted_config_are_not_copied(self):
        tmp, meta = self.prepare({
            'app/main.py': 'print(1)\n', '.env': 'SECRET=1\n', '.env.example': 'A=\n', 'deploy/server.pem': 'x',
            'data/prod.sqlite3': 'x', 'creds/id_rsa': 'x', '.npmrc': 'x', 'opencode.json': '{}',
            '.opencode/commands/x.md': 'x', 'sub/opencode.jsonc': '{}', '.claude/settings.json': '{}',
            'link': None})
        present = {str(p.relative_to(os.path.join(tmp, 'w'))) for p in Path(tmp, 'w').rglob('*') if p.is_file()}
        self.assertEqual(present, {'app/main.py', '.env.example'})
        reasons = {item['path']: item['reason'] for item in meta['excluded']}
        self.assertEqual(reasons['link'], 'symlink or non-regular file')
        self.assertEqual(reasons['.env'], 'credential-like file name')
        self.assertEqual(reasons['opencode.json'], 'untrusted agent configuration')

    def test_sensitive_content_outside_the_command_text_fails_closed(self):
        for body in (f'# note\n{FAKE_KEY}\nabc\n', f'token = "{FAKE_TOKEN}"\n', 'k = "AKIA' + 'ABCDEFGHIJKLMNOP"\n',
                     'xoxb-' + '1234567890-abcdefghij'):
            with self.assertRaises(sandbox.SandboxError, msg=body[:20]):
                self.prepare({'src/clean.py': 'x = 1\n', 'docs/notes.md': body})

    def test_secret_scan_error_never_echoes_the_secret(self):
        try:
            self.prepare({'docs/notes.md': f'{FAKE_TOKEN}\n'})
        except sandbox.SandboxError as error:
            self.assertIn('docs/notes.md', str(error))
            self.assertNotIn(FAKE_TOKEN, str(error))

    def test_owner_allowlist_exempts_known_fixtures_only(self):
        self.prepare({'tests/fixture.txt': f'{FAKE_KEY}\n'}, allowlist={'tests/fixture.txt'})
        with self.assertRaises(sandbox.SandboxError):
            self.prepare({'tests/fixture.txt': f'{FAKE_KEY}\n', 'tests/other.txt': f'{FAKE_KEY}\n'},
                         allowlist={'tests/fixture.txt'})

    def test_task_and_repository_content_are_the_only_model_inputs(self):
        # The guard command carries exactly one free-form argument: the preface plus the
        # authorized task. No issue body, discussion or PR comment path exists.
        cmd = guard.build_command('opencode/x-free', '{}', '/h', '/w', 25, 'do it')
        self.assertEqual(cmd[-1], guard.PREFACE + 'do it')
        self.assertEqual(cmd[cmd.index('--') + 1:], [guard.PREFACE + 'do it'])
        self.assertNotIn('--file', cmd)
        self.assertNotIn('--attach', cmd)
        for source in ('issue.body', 'comments', 'discussion'):
            self.assertNotIn(source, (SCRIPTS / 'opencode_guard.py').read_text())

    def test_router_refuses_sensitive_command_text_before_any_model_call(self):
        enabled = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
        for m in enabled['models'].values():
            m.update(terms_verified=True, repo_code_approved=True, operational_status='healthy',
                     terms_review={'reviewed_by': 'o', 'date': 'd', 'source': 's'})
        for task in (f'use {FAKE_TOKEN}', 'summarize customer data', 'fix credentials handling'):
            with self.assertRaises(router.RoutingError):
                router.route(task, enabled, set(enabled['models']))

    def test_agent_config_is_managed_loopback_only_and_deny_by_default_for_risky_tools(self):
        config = sandbox.agent_config('opencode/glm-5.3-flash-free', 4321)
        options = config['provider']['opencode']['options']
        self.assertEqual(options['baseURL'], 'http://127.0.0.1:4321/inference/openai/v1')
        self.assertEqual(options['apiKey'], 'sandbox-placeholder')
        self.assertEqual(config['enabled_providers'], ['opencode'])
        self.assertEqual(config['share'], 'disabled')
        permission = config['permission']
        for tool in ('webfetch', 'websearch', 'external_directory', 'task'):
            self.assertEqual(permission[tool], 'deny')
        self.assertEqual(permission['read']['*.env'], 'deny')
        self.assertNotIn('ghp_', json.dumps(config))

    def test_agent_config_rejects_paid_or_invented_models(self):
        for model in ('opencode-go/kimi-k3', 'opencode/deepseek-v4.1-flash', 'anthropic/claude-opus-5',
                      'opencode/x-free; rm -rf /', ''):
            with self.assertRaises(sandbox.SandboxError, msg=model):
                sandbox.agent_config(model, 1)


class GuardTests(unittest.TestCase):
    @staticmethod
    def ok_events():
        return [json.dumps({'type': 'step_start', 'part': {}}),
                json.dumps({'type': 'text', 'part': {'text': 'done'}}),
                json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}})]

    def test_only_a_clean_complete_run_is_success(self):
        self.assertEqual(guard.evaluate(self.ok_events(), 0, False), ('success', ''))

    def test_failures_never_count_as_success(self):
        cases = {
            'provider error event': (self.ok_events() + [json.dumps({'type': 'error', 'error': {'name': 'X'}})], 0, False),
            'error before finish': ([json.dumps({'type': 'error'})] + self.ok_events(), 0, False),
            'timeout': (self.ok_events(), 124, True),
            'timeout exit code only': (self.ok_events(), 124, False),
            'kill exit code': (self.ok_events(), 137, False),
            'crash': (self.ok_events(), 1, False),
            'cancellation signal': (self.ok_events(), -15, False),
            'no exit code': (self.ok_events(), None, False),
            'no events': ([], 0, False),
            'only start': ([json.dumps({'type': 'step_start', 'part': {}})], 0, False),
            'truncated by length': ([json.dumps({'type': 'step_finish', 'part': {'reason': 'length'}})], 0, False),
            'ended on tool calls': ([json.dumps({'type': 'step_finish', 'part': {'reason': 'tool-calls'}})], 0, False),
            'missing reason': ([json.dumps({'type': 'step_finish', 'part': {}})], 0, False),
            'later step incomplete': (self.ok_events() + [json.dumps({'type': 'step_finish', 'part': {'reason': 'length'}})], 0, False),
            'garbage line': (self.ok_events() + ['Traceback (most recent call last):'], 0, False),
            'non-object json': (self.ok_events() + ['[1,2]'], 0, False),
            'event without type': (self.ok_events() + ['{"part": {}}'], 0, False),
        }
        for name, (lines, code, timed_out) in cases.items():
            status, reason = guard.evaluate(lines, code, timed_out)
            self.assertEqual(status, 'failed', name)
            self.assertTrue(reason, name)

    def test_untrusted_finish_reason_is_sanitized(self):
        _status, reason = guard.evaluate([json.dumps({'type': 'step_finish', 'part': {'reason': 'x`$(id)\n::error::'}})], 0, False)
        self.assertRegex(reason, r'^incomplete_generation:[a-z_-]*$')

    def test_command_runs_as_isolated_user_with_clean_env_and_timeout(self):
        cmd = guard.build_command('opencode/glm-5.3-flash-free', '{"a":1}', '/srv/ocagent/home', '/srv/ocagent/work', 25, 't')
        self.assertEqual(cmd[:4], ['sudo', '-n', '-u', 'ocagent'])
        self.assertEqual(cmd[4:6], ['timeout', '--kill-after=30s'])
        self.assertIn('25m', cmd)
        self.assertIn('-i', cmd[cmd.index('env'):cmd.index('env') + 2])
        joined = ' '.join(cmd)
        for leaked in ('GITHUB_TOKEN', 'GH_TOKEN', 'OPENCODE_API_KEY=', 'ACTIONS_', 'RUNNER_'):
            self.assertNotIn(leaked, joined)
        self.assertIn('OPENCODE_DISABLE_PROJECT_CONFIG=1', cmd)
        self.assertIn('OPENCODE_CONFIG_CONTENT={"a":1}', cmd)
        run = cmd[cmd.index('opencode'):]
        self.assertEqual(run[:5], ['opencode', 'run', '--format', 'json', '--model'])
        self.assertNotIn('--auto', run)
        self.assertNotIn('--dangerously-skip-permissions', run)

    def run_main(self, completed=None, raises=None):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, 'c.json')
            config.write_text('{}')
            result, out = Path(tmp, 'r.json'), Path(tmp, 'out')
            old = dict(os.environ)
            try:
                os.environ.update({'TASK': 'do it', 'GITHUB_OUTPUT': str(out)})
                with mock.patch.object(guard.subprocess, 'run') as run:
                    if raises:
                        run.side_effect = [raises, mock.Mock(returncode=0)]
                    else:
                        run.return_value = completed
                    code = guard.main(['g', str(result), 'opencode/x-free', str(config), '/h', '/w', '25'])
                return code, json.loads(result.read_text()), out.read_text()
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_main_success_and_failure_exit_codes_and_outputs(self):
        ok = mock.Mock(stdout='\n'.join(self.ok_events()), stderr='', returncode=0)
        code, result, out = self.run_main(ok)
        self.assertEqual((code, result['status']), (0, 'success'))
        self.assertIn('generation=success', out)
        for returncode in (1, 124, 137, -9):
            bad = mock.Mock(stdout='\n'.join(self.ok_events()), stderr='boom', returncode=returncode)
            code, result, out = self.run_main(bad)
            self.assertEqual((code, result['status']), (1, 'failed'), returncode)
            self.assertIn('generation=failed', out)

    def test_main_backstop_timeout_kills_agent_user_and_fails(self):
        code, result, out = self.run_main(raises=subprocess.TimeoutExpired('x', 1))
        self.assertEqual((code, result['status'], result['reason']), (1, 'failed', 'timeout'))
        self.assertIn('generation=failed', out)


class _Upstream(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        _Upstream.seen.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for piece in (b'data: one\n\n', b'data: two\n\n'):
            self.wfile.write(piece)
            self.wfile.flush()

    do_GET = do_POST


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.proxy = proxy.serve('REAL-KEY', f'http://127.0.0.1:{cls.upstream.server_address[1]}')
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.proxy.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.upstream.shutdown()

    def setUp(self):
        _Upstream.seen.clear()

    def request(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_real_key_is_injected_and_placeholder_discarded(self):
        status, body = self.request('POST', '/inference/openai/v1/chat/completions', b'{"m":1}',
                                    {'Authorization': 'Bearer sandbox-placeholder', 'Content-Type': 'application/json',
                                     'X-Api-Key': 'agent-supplied', 'Cookie': 'a=b'})
        self.assertEqual(status, 200)
        self.assertEqual(body, b'data: one\n\ndata: two\n\n')  # streamed through intact
        path, headers, sent = _Upstream.seen[0]
        self.assertEqual(headers['Authorization'], 'Bearer REAL-KEY')
        self.assertNotIn('agent-supplied', json.dumps(headers))
        self.assertNotIn('Cookie', headers)
        self.assertEqual(sent, b'{"m":1}')

    def test_only_allowlisted_paths_and_methods_pass(self):
        for method, path in (('GET', '/'), ('POST', '/v1/chat/completions'), ('POST', '/inference/../admin'),
                             ('POST', '/inference//x'), ('POST', '/inference/%2e%2e/x'),
                             ('POST', '/other/inference/'), ('GET', '/zen/v2/models')):
            status, _ = self.request(method, path, b'x' if method == 'POST' else None)
            self.assertEqual(status, 403, f'{method} {path}')
        self.assertEqual(_Upstream.seen, [])
        self.assertEqual(self.request('GET', '/zen/v1/models')[0], 200)  # legacy catalog route

    def test_unsupported_methods_are_not_forwarded(self):
        status, _ = self.request('DELETE', '/inference/openai/v1/x')
        self.assertNotEqual(status, 200)
        self.assertEqual(_Upstream.seen, [])

    def test_proxy_source_never_logs_or_exposes_the_key(self):
        source = (SCRIPTS / 'opencode_proxy.py').read_text()
        self.assertIn('def log_message(self, fmt, *args):  # no request details in logs', source)
        self.assertNotIn('print(key', source)
        self.assertNotIn('api_key)', source.replace("os.environ.pop('OPENCODE_API_KEY', '')", ''))


class CatalogTests(unittest.TestCase):
    def test_current_endpoint_preferred_legacy_used_as_fallback(self):
        calls = []
        def get(url):
            calls.append(url)
            if 'inference' in url:
                raise urllib.error.URLError('down')
            return {'data': [{'id': 'glm-5.3-flash-free'}]}
        endpoint, ids = catalog.fetch(get, sleep=lambda _s: None)
        self.assertEqual(ids, ['glm-5.3-flash-free'])
        self.assertTrue(endpoint.endswith('/zen/v1/models'))
        self.assertTrue(calls[0].endswith('/inference/openai/v1/models'))
        self.assertEqual(catalog.fetch(lambda u: {'data': [{'id': 'x'}]}, sleep=lambda _s: None)[0], catalog.ENDPOINTS[0])

    def test_no_models_anywhere_fails_safely(self):
        for payload in ({'data': []}, [], {}, 'x', None):
            with self.assertRaises(RuntimeError):
                catalog.fetch(lambda u, p=payload: p, sleep=lambda _s: None)

    def test_missing_key_gives_actionable_error_without_leaking(self):
        old = dict(os.environ)
        try:
            os.environ.pop('OPENCODE_API_KEY', None)
            self.assertEqual(catalog.main(['c', '/nonexistent/out.json']), 1)
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == '__main__':
    unittest.main()
