"""OpenCode sandbox, guard, proxy and catalog tests (tests v1.0). No network, no sudo."""
import contextlib
import copy
import gzip
import http.client
import http.server
import io
import importlib
import importlib.util
import socket
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
    return importlib.import_module(name)   # one shared instance per module, as in production


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

    def sandbox_files(self, tmp, files):
        repo = make_repo(tmp, {})
        for rel, data in files.items():
            full = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            Path(full).write_bytes(data)
        subprocess.run(['git', '-C', repo, 'add', '-A', '-f'], check=True)
        meta = sandbox.prepare(repo, os.path.join(tmp, 'w'), os.path.join(tmp, 's'), 'a' * 40, set())
        present = {str(p.relative_to(os.path.join(tmp, 'w'))) for p in Path(tmp, 'w').rglob('*') if p.is_file()}
        return meta, present

    def test_secret_after_the_old_two_mib_scan_boundary_never_reaches_the_model(self):
        token = ('ghp_' + 'A1b2C3d4' * 5).encode()
        with tempfile.TemporaryDirectory() as tmp:
            # 3 MiB file with the secret at 2.5 MiB, beyond the previous scan window
            meta, present = self.sandbox_files(tmp, {'big/notes.txt': b'x' * 2_600_000 + token + b'y' * 500_000,
                                                     'ok.txt': b'fine\n'})
        self.assertNotIn('big/notes.txt', present)                      # excluded, not copied
        self.assertEqual(present, {'ok.txt'})
        reasons = {e['path']: e['reason'] for e in meta['excluded']}
        self.assertEqual(reasons['big/notes.txt'], 'larger than the per-file limit')

    def test_secret_near_the_end_of_a_file_inside_the_limit_is_found(self):
        token = ('ghp_' + 'A1b2C3d4' * 5).encode()
        for offset in (0, 1_000_000, sandbox.MAX_FILE_BYTES - len(token) - 1):
            data = b'a' * offset + token + b'\n'
            self.assertLessEqual(len(data), sandbox.MAX_FILE_BYTES)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(sandbox.SandboxError, msg=str(offset)):
                    self.sandbox_files(tmp, {'src/long.py': data})

    def test_secret_after_early_nul_bytes_never_reaches_the_model(self):
        token = ('ghp_' + 'A1b2C3d4' * 5).encode()
        for payload in (b'\0' + b'z' * 5000 + token, b'text\0\0' + token, token + b'\0',
                        b'a' * 10000 + b'\0' + token):
            with tempfile.TemporaryDirectory() as tmp:
                meta, present = self.sandbox_files(tmp, {'data/blob.bin': payload, 'ok.txt': b'fine\n'})
            self.assertEqual(present, {'ok.txt'}, payload[:12])           # the NUL file is not exposed
            self.assertEqual({e['path']: e['reason'] for e in meta['excluded']}['data/blob.bin'], 'binary content')

    def test_unclassifiable_files_are_excluded_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta, present = self.sandbox_files(tmp, {
                'latin1.txt': 'caf\xe9'.encode('latin-1'), 'png.png': b'\x89PNG\r\n\x1a\n\0\0\0', 'ok.txt': b'fine\n',
                'utf8.txt': 'caf\u00e9\n'.encode()})
        self.assertEqual(present, {'ok.txt', 'utf8.txt'})
        reasons = {e['path']: e['reason'] for e in meta['excluded']}
        self.assertEqual(reasons['latin1.txt'], 'not valid UTF-8')
        self.assertEqual(reasons['png.png'], 'binary content')

    def test_explicit_limits_are_documented_in_code_and_enforced(self):
        self.assertEqual(sandbox.MAX_FILE_BYTES, 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {'a.txt': 'a', 'b.txt': 'b', 'c.txt': 'c'})
            with mock.patch.object(sandbox, 'MAX_FILES', 2):
                with self.assertRaises(sandbox.SandboxError) as raised:
                    sandbox.prepare(repo, os.path.join(tmp, 'w'), os.path.join(tmp, 's'), 'a' * 40, set())
            self.assertIn('size limits', str(raised.exception))
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {'a.txt': 'abcdef'})
            with mock.patch.object(sandbox, 'MAX_TOTAL_BYTES', 3):
                with self.assertRaises(sandbox.SandboxError):
                    sandbox.prepare(repo, os.path.join(tmp, 'w'), os.path.join(tmp, 's'), 'a' * 40, set())

    def test_new_secret_formats_are_detected(self):
        # Built at run time so this file itself contains no secret-shaped literal (the sandbox would
        # otherwise refuse to copy the repository that holds its own tests).
        samples = ['-----BEGIN ' + 'OPENSSH PRIVATE KEY-----', '-----BEGIN ' + 'PGP PRIVATE KEY BLOCK-----',
                   'ey' + 'JhbGciOiJIUzI1NiJ9.ey' + 'JzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk',
                   'sk_live_' + 'a1b2c3d4e5f6g7h8i9j0k1', 'AccountKey=' + 'A' * 44 + '==', 'glpat-' + 'x' * 20,
                   'npm_' + 'a' * 36, 'ASIA' + 'A' * 16]
        for sample in samples:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(sandbox.SandboxError, msg=sample[:20]):
                    self.sandbox_files(tmp, {'x.txt': sample.encode()})

    def test_the_repository_itself_passes_its_own_secret_scan(self):
        # If any tracked file held a secret-shaped literal the sandbox would refuse every run.
        tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True).stdout.split(b'\0')
        hits = [p.decode() for p in tracked if p and os.path.isfile(ROOT / p.decode()) and not os.path.islink(ROOT / p.decode())
                and sandbox.classify_file(str(ROOT / p.decode()))[0] == 'secret']
        self.assertEqual(hits, [])

    def test_whole_file_scan_has_no_window_to_straddle(self):
        token = ('ghp_' + 'A1b2C3d4' * 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'f.txt')
            for pad in (65535, 65536, 1048576 - 100):
                path.write_text('a' * pad + token)
                verdict = sandbox.classify_file(str(path))[0]
                self.assertEqual(verdict, 'secret' if path.stat().st_size <= sandbox.MAX_FILE_BYTES else 'excluded', pad)

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


FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'opencode_1_19_3'


def fixture_lines(name):
    return (FIXTURES / f'{name}.jsonl').read_text().splitlines()


class GuardTests(unittest.TestCase):
    """Command construction and the guard entry point. Lifecycle rules: test_opencode_guard_fixtures."""

    def test_command_runs_as_isolated_user_with_clean_env_and_timeout(self):
        cmd = guard.build_command('opencode/glm-5.3-flash-free', '{"a":1}', '/srv/ocagent-home', '/srv/ocimpl/work', 25, 't')
        self.assertEqual(cmd[:4], ['sudo', '-n', '-u', 'ocagent'])
        self.assertEqual(cmd[4:6], ['timeout', '--kill-after=30s'])
        self.assertIn('25m', cmd)
        self.assertIn('-i', cmd[cmd.index('env'):cmd.index('env') + 2])
        joined = ' '.join(cmd)
        for leaked in ('GITHUB_TOKEN', 'GH_TOKEN', 'OPENCODE_API_KEY=', 'ACTIONS_', 'RUNNER_'):
            self.assertNotIn(leaked, joined)
        # flags that exist in OpenCode 1.19.3 (packages/core/src/flag/flag.ts)
        for flag in ('OPENCODE_DISABLE_PROJECT_CONFIG=1', 'OPENCODE_PURE=1', 'OPENCODE_DISABLE_MODELS_FETCH=1',
                     'OPENCODE_DISABLE_AUTOUPDATE=1', 'OPENCODE_CONFIG_CONTENT={"a":1}'):
            self.assertIn(flag, cmd)
        self.assertNotIn('OPENCODE_DISABLE_EXTERNAL_SKILLS=1', cmd)   # not a 1.19.3 flag: it did nothing
        run = cmd[cmd.index('opencode'):]
        self.assertEqual(run[:6], ['opencode', 'run', '--pure', '--format', 'json', '--model'])
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
        ok = mock.Mock(stdout='\n'.join(fixture_lines('success_edit')), stderr='', returncode=0)
        code, result, out = self.run_main(ok)
        self.assertEqual((code, result['status']), (0, 'success'))
        self.assertIn('generation=success', out)
        self.assertTrue(result['session_id'].startswith('ses_'))
        for returncode in (1, 124, 137, -9):
            bad = mock.Mock(stdout='\n'.join(fixture_lines('success_edit')), stderr='boom', returncode=returncode)
            code, result, out = self.run_main(bad)
            self.assertEqual((code, result['status']), (1, 'failed'), returncode)
            self.assertIn('generation=failed', out)
            self.assertEqual(result['session_id'], '')

    def test_main_rejects_real_failure_transcripts(self):
        for name in ('fail_provider_500', 'fail_unauthorized_401', 'fail_truncated_length', 'fail_ends_on_tool_calls'):
            done = mock.Mock(stdout='\n'.join(fixture_lines(name)), stderr='', returncode=0)  # even with a clean exit
            code, result, _out = self.run_main(done)
            self.assertEqual((code, result['status']), (1, 'failed'), name)

    def test_main_backstop_timeout_kills_agent_user_and_fails(self):
        code, result, out = self.run_main(raises=subprocess.TimeoutExpired('x', 1))
        self.assertEqual((code, result['status'], result['reason']), (1, 'failed', 'timeout'))
        self.assertIn('generation=failed', out)

    def test_review_mode_requires_a_session_identity_and_writes_the_final_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            text_out = Path(tmp, 'review.txt')
            old = dict(os.environ)
            try:
                os.environ['GUARD_MODE'] = 'review'
                os.environ['GUARD_TEXT_OUT'] = str(text_out)
                done = mock.Mock(stdout='\n'.join(fixture_lines('success_text')), stderr='', returncode=0)
                code, result, _ = self.run_main(done)
                self.assertEqual(code, 0)
                self.assertEqual(text_out.read_text(), 'Nothing to change.')
            finally:
                os.environ.clear()
                os.environ.update(old)


ENABLED_REGISTRY = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
for _model in ('opencode/glm-5.3-flash-free', 'opencode/mimo-v2.6-flash-free', 'opencode/deepseek-v4.1-flash-free'):
    ENABLED_REGISTRY['models'][_model].update(terms_verified=True, terms_review={'reviewed_by': 'o', 'date': 'd', 'source': 's'},
                                              repo_code_approved=True, operational_status='healthy')
BOUND = 'glm-5.3-flash-free'
CHAT = '/inference/openai/v1/chat/completions'


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


def body(**overrides):
    data = {'model': BOUND, 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True}
    data.update(overrides)
    return json.dumps(data).encode()


class ProxyBindingTests(unittest.TestCase):
    """Direct behavioural tests: crafted raw HTTP requests against a live proxy instance."""

    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.log = os.path.join(cls.tmp, 'rejections.log')
        cls.server, cls.proxy = proxy.serve('REAL-KEY', f'http://127.0.0.1:{cls.upstream.server_address[1]}', BOUND,
                                            log_path=cls.log)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.upstream.shutdown()
        __import__('shutil').rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _Upstream.seen.clear()
        del self.proxy.rejections[:]

    def raw(self, method, path, payload=None, headers=None, encode_chunked=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Content-Type': 'application/json'} if headers is None else headers
        try:
            conn.request(method, path, body=payload, headers=headers,
                         encode_chunked=encode_chunked)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def assert_blocked(self, code, *args, **kwargs):
        before = len(_Upstream.seen)
        status, text = self.raw(*args, **kwargs)
        self.assertIn(status, (400, 403, 405, 413), text)
        self.assertEqual(len(_Upstream.seen), before, 'request reached the provider')
        self.assertEqual(self.proxy.rejections[-1]['rejected'], code, self.proxy.rejections)

    # ---- allowed
    def test_bound_model_request_is_forwarded_with_the_real_credential(self):
        status, text = self.raw('POST', CHAT, body(), {'Content-Type': 'application/json',
                                                       'Authorization': 'Bearer sandbox-placeholder', 'Cookie': 'a=b'})
        self.assertEqual((status, text), (200, b'data: one\n\ndata: two\n\n'))
        path, headers, sent = _Upstream.seen[0]
        self.assertEqual(headers['Authorization'], 'Bearer REAL-KEY')
        self.assertNotIn('Cookie', headers)
        self.assertEqual(sent, body())                      # forwarded byte for byte
        self.assertEqual(self.proxy.rejections, [])

    def test_model_listing_is_the_only_get(self):
        self.assertEqual(self.raw('GET', '/inference/openai/v1/models')[0], 200)
        self.assertEqual(self.raw('GET', '/zen/v1/models')[0], 200)
        self.assert_blocked('endpoint_not_allowed', 'GET', '/inference/openai/v1/chat/completions')

    # ---- model substitution
    def test_every_other_model_name_is_rejected(self):
        for model in ('deepseek-v4.1-flash-free', 'mimo-v2.6-flash-free', 'nemotron-3-ultra-free', 'deepseek-v4.1-flash',
                      'glm-5.3', 'gpt-5.4-nano', 'claude-opus-5', 'opencode/glm-5.3-flash-free', 'opencode-go/kimi-k3',
                      'GLM-5.3-FLASH-FREE', 'glm-5.3-flash-free ', ' glm-5.3-flash-free', 'glm-5.3-flash-free\n', '', '*'):
            self.assert_blocked('model_mismatch', 'POST', CHAT, body(model=model))

    def test_malformed_model_values_are_rejected(self):
        for value in (None, 5, True, ['glm-5.3-flash-free'], {'id': BOUND}, [BOUND, 'gpt-5']):
            self.assert_blocked('model_mismatch', 'POST', CHAT, body(model=value))
        self.assert_blocked('model_missing', 'POST', CHAT, json.dumps({'messages': [{'role': 'user', 'content': 'x'}]}).encode())

    def test_duplicate_keys_cannot_smuggle_a_second_model(self):
        for raw_body in (b'{"model": "glm-5.3-flash-free", "model": "gpt-5.4-nano", "messages": [{"role": "user", "content": "x"}]}',
                         b'{"model": "gpt-5.4-nano", "model": "glm-5.3-flash-free", "messages": [{"role": "user", "content": "x"}]}',
                         b'{"model": "glm-5.3-flash-free", "messages": [{"role": "user", "content": "x"}], "model": "x"}'):
            self.assert_blocked('duplicate_json_key', 'POST', CHAT, raw_body)

    def test_routing_and_fallback_fields_are_rejected(self):
        for field in ('models', 'provider', 'route', 'fallbacks', 'plugins', 'transforms', 'extra_body', 'base_url',
                      'api_key', 'engine', 'Model', 'MODEL'):
            self.assert_blocked('unsupported_field', 'POST', CHAT, body(**{field: ['gpt-5.4-nano']}))

    # ---- operations and formats
    def test_other_endpoints_and_methods_are_rejected(self):
        for path in ('/inference/openai/v1/responses', '/inference/openai/v1/completions', '/inference/openai/v1/embeddings',
                     '/inference/openai/v1/images/generations', '/inference/anthropic/v1/messages', '/zen/v1/chat/completions',
                     '/zen/v1/messages', '/v1/chat/completions', '/', '/inference/openai/v1/chat/completions/extra',
                     '/inference/openai/v1/models', '/inference/openai/v1/files'):
            if path == '/inference/openai/v1/models':   # POST to the listing endpoint
                self.assert_blocked('endpoint_not_allowed', 'POST', path, body())
            else:
                self.assert_blocked('endpoint_not_allowed', 'POST', path, body())
        for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            status, _ = self.raw(method, CHAT, body() if method in ('PUT', 'PATCH') else None)
            self.assertEqual(status, 405, method)
            self.assertEqual(self.proxy.rejections[-1]['rejected'], 'method_not_allowed')
        self.assertEqual(_Upstream.seen, [])

    def test_path_tricks_are_rejected(self):
        for path in (CHAT + '?model=gpt-5', CHAT + '?', CHAT + '#frag', '/inference/openai/v1/../v1/chat/completions',
                     '/inference//openai/v1/chat/completions', '/inference/openai/v1%2fchat/completions',
                     '/inference/openai/v1/chat/completions%00', '\\inference\\openai\\v1\\chat\\completions'):
            self.assert_blocked('malformed_path', 'POST', path, body())

    def test_uninspectable_or_malformed_bodies_are_rejected(self):
        self.assert_blocked('content_encoding_not_supported', 'POST', CHAT, gzip.compress(body()),
                            {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
        self.assert_blocked('transfer_encoding_not_supported', 'POST', CHAT, [body()],
                            {'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'}, encode_chunked=True)
        self.assert_blocked('content_type_not_json', 'POST', CHAT, body(), {'Content-Type': 'text/plain'})
        self.assert_blocked('content_type_not_json', 'POST', CHAT, body(), {})
        self.assert_blocked('malformed_json', 'POST', CHAT, b'{"model": "glm-5.3-flash-free", ')
        self.assert_blocked('malformed_json', 'POST', CHAT, b'\xff\xfe{"model": "glm-5.3-flash-free"}')
        self.assert_blocked('malformed_json', 'POST', CHAT, b'\xef\xbb\xbf' + body())
        self.assert_blocked('non_standard_json', 'POST', CHAT, b'{"model": "glm-5.3-flash-free", "messages": [1], "temperature": NaN}')
        self.assert_blocked('body_not_object', 'POST', CHAT, b'[]')
        self.assert_blocked('body_not_object', 'POST', CHAT, b'"glm-5.3-flash-free"')
        self.assert_blocked('empty_body', 'POST', CHAT, b'')
        self.assert_blocked('messages_invalid', 'POST', CHAT, body(messages=[]))
        self.assert_blocked('messages_invalid', 'POST', CHAT, body(messages='hi'))
        self.assert_blocked('malformed_json', 'POST', CHAT, b'{"a":' * 10000 + b'1' + b'}' * 10000)  # deep nesting

    def test_oversized_and_invalid_lengths_are_rejected(self):
        status, _ = self.raw('POST', CHAT, b'x', {'Content-Type': 'application/json', 'Content-Length': '99999999999'})
        self.assertEqual(status, 413)
        self.assertEqual(self.proxy.rejections[-1]['rejected'], 'body_too_large_or_invalid_length')
        self.assertEqual(_Upstream.seen, [])

    def test_get_with_a_body_is_rejected(self):
        self.assert_blocked('unexpected_body', 'GET', '/zen/v1/models', b'{"model": "x"}', {'Content-Type': 'application/json'})

    # ---- logging
    def test_rejections_are_logged_without_secrets_or_prompt_content(self):
        secret_prompt = 'my password is hunter2 and ghp_' + 'Q' * 30
        os.truncate(self.log, 0) if os.path.exists(self.log) else None
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.raw('POST', CHAT, body(model='gpt-5.4-nano', messages=[{'role': 'user', 'content': secret_prompt}]),
                     {'Content-Type': 'application/json', 'Authorization': 'Bearer attacker-supplied-token-123'})
        logged = open(self.log).read() + err.getvalue()
        for forbidden in ('hunter2', 'ghp_', 'attacker-supplied-token', 'REAL-KEY', 'gpt-5.4-nano', 'hello'):
            self.assertNotIn(forbidden, logged)
        entry = json.loads(open(self.log).read().splitlines()[-1])
        self.assertEqual(set(entry), {'rejected', 'method', 'path'})
        self.assertEqual(entry['rejected'], 'model_mismatch')

    def test_hostile_paths_are_sanitized_in_logs(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        sock.sendall(b'POST /inference/openai/v1/x\x1b[31mred HTTP/1.0\r\nContent-Type: application/json\r\n'
                     b'Content-Length: 2\r\n\r\n{}')
        sock.recv(4096)
        sock.close()
        self.assertTrue(self.proxy.rejections)
        for entry in self.proxy.rejections:
            self.assertRegex(entry['path'], r'^[A-Za-z0-9/._?-]*$')


class ProxyStartupPolicyTests(unittest.TestCase):
    """The proxy refuses to be bound to a model the router would never choose."""

    def bind(self, model, registry=ENABLED_REGISTRY, denied=frozenset()):
        return proxy.validate_bound_model(model, registry, set(denied))

    def test_enabled_free_model_binds_to_its_bare_id(self):
        self.assertEqual(self.bind('opencode/glm-5.3-flash-free'), 'glm-5.3-flash-free')
        self.assertEqual(self.bind('glm-5.3-flash-free'), 'glm-5.3-flash-free')

    def test_paid_unlisted_and_invented_models_cannot_be_bound(self):
        for model in ('opencode/deepseek-v4.1-flash', 'opencode-go/kimi-k3', 'anthropic/claude-opus-5', 'opencode/glm-5.3',
                      'opencode/invented-free', 'gpt-5.4-nano', '', 'opencode/glm-5.3-flash-free/../x'):
            with self.assertRaises(proxy.PolicyError, msg=model):
                self.bind(model)

    def test_disabled_unapproved_unverified_or_unhealthy_models_cannot_be_bound(self):
        shipped = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
        with self.assertRaises(proxy.PolicyError) as raised:
            self.bind('opencode/glm-5.3-flash-free', shipped)          # everything ships disabled
        self.assertEqual(raised.exception.code, 'model_disabled_or_unhealthy')
        for field, value in (('terms_verified', False), ('terms_review', None), ('repo_code_approved', False),
                             ('operational_status', 'degraded'), ('operational_status', 'unknown')):
            registry = copy.deepcopy(ENABLED_REGISTRY)
            registry['models']['opencode/glm-5.3-flash-free'][field] = value
            with self.assertRaises(proxy.PolicyError, msg=field):
                self.bind('opencode/glm-5.3-flash-free', registry)
        self.assertIsNotNone(self.bind('opencode/glm-5.3-flash-free'))   # control

    def test_nemotron_stays_unbindable_until_the_owner_enables_it(self):
        with self.assertRaises(proxy.PolicyError):
            self.bind('opencode/nemotron-3-ultra-free')

    def test_reviewer_cannot_be_bound_to_an_implementation_model(self):
        with self.assertRaises(proxy.PolicyError) as raised:
            self.bind('opencode/mimo-v2.6-flash-free', denied={'opencode/mimo-v2.6-flash-free'})
        self.assertEqual(raised.exception.code, 'model_denied_for_this_role')
        with self.assertRaises(proxy.PolicyError):
            self.bind('mimo-v2.6-flash-free', denied={'opencode/mimo-v2.6-flash-free'})   # prefix-insensitive

    def test_weakened_separation_policy_refuses_to_start(self):
        weakened = copy.deepcopy(ENABLED_REGISTRY)
        weakened['agent_separation']['require_distinct_models'] = False
        with self.assertRaises(router.RoutingError):
            self.bind('opencode/glm-5.3-flash-free', weakened)

    def test_cli_refuses_to_start_and_never_prints_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, 'registry.json')
            registry.write_text(json.dumps(json.loads((ROOT / '.github/opencode/model-registry.json').read_text())))
            old = dict(os.environ)
            try:
                os.environ['OPENCODE_API_KEY'] = 'SUPER-SECRET-KEY'
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        mock.patch.object(proxy, 'serve', side_effect=AssertionError('proxy must not start')):
                    code = proxy.main(['p', str(Path(tmp, 'port')), 'opencode/glm-5.3-flash-free', str(registry)])
                self.assertEqual(code, 1)
                self.assertNotIn('SUPER-SECRET-KEY', out.getvalue())
                self.assertIn('model_disabled_or_unhealthy', out.getvalue())
                self.assertNotIn('OPENCODE_API_KEY', os.environ)         # key removed from the environment
                self.assertFalse(Path(tmp, 'port').exists())
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_proxy_instance_binds_exactly_one_model(self):
        server, px = proxy.serve('K', 'http://127.0.0.1:9', BOUND)
        try:
            self.assertEqual(px.bound, BOUND)
            for model in ('mimo-v2.6-flash-free', 'deepseek-v4.1-flash-free'):
                with self.assertRaises(proxy.PolicyError):
                    proxy.check_request('POST', CHAT, {'Content-Type': 'application/json'}, body(model=model), px.bound)
        finally:
            server.server_close()


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
