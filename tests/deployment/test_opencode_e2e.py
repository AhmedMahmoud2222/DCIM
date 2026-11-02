"""End-to-end test with the REAL OpenCode v2 binary, the model-bound proxy and a stub upstream.

Runs only when OPENCODE_BIN points at the pinned binary (CI installs it from the committed
lockfile; locally: `cd .github/opencode/runtime && npm ci --ignore-scripts`). Otherwise every
test is SKIPPED, and a skip is not a pass.

Only the provider is synthetic: a loopback stub that answers with scripted replies for a synthetic key.
The CLI, its permission engine, its event stream and its retry behaviour are the genuine runtime.
"""
import http.server
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
proxy = importlib.import_module('opencode_proxy')
sandbox = importlib.import_module('opencode_sandbox')
guard = importlib.import_module('opencode_guard')

DEFAULT_BIN = ROOT / '.github/opencode/runtime/node_modules/@opencode/cli-linux-x64/bin/opencode'
BIN = os.environ.get('OPENCODE_BIN') or (str(DEFAULT_BIN) if DEFAULT_BIN.exists() else '')
MODEL = 'opencode/glm-5.3-flash-free'
PINNED_VERSION = '2.1.4'


def piece(delta, finish=None):
    return 'data: ' + json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm',
                                  'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'


def text_reply(text='ok'):
    return piece({'role': 'assistant', 'content': ''}) + piece({'content': text}) + piece({}, 'stop') + 'data: [DONE]\n\n'


def tool_reply(name, arguments):
    call = {'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
    return piece({'role': 'assistant', 'tool_calls': [call]}) + piece({}, 'tool_calls') + 'data: [DONE]\n\n'


@unittest.skipUnless(BIN and os.path.exists(BIN), 'real OpenCode v2 binary not installed (set OPENCODE_BIN)')
class RealOpenCodeThroughProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version = subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip()
        cls.seen = []
        cls.behaviour = lambda body, seen_before: ('sse', text_reply())

        class Stub(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                cls.seen.append({'path': self.path, 'model': body.get('model'), 'auth': self.headers.get('Authorization'),
                                 'headers': {k.lower(): v for k, v in self.headers.items()}, 'tools': bool(body.get('tools')),
                                 'roles': [m.get('role') for m in body.get('messages', [])]})
                kind, value = cls.behaviour(body, len(cls.seen))
                if kind == 'status':
                    payload = json.dumps({'error': {'message': 'synthetic'}}).encode()
                    self.send_response(value)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(value.encode())

        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        registry = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
        registry['models'][MODEL].update(
            terms_verified=True,
            terms_review={'reviewed_by': 't', 'date': '2026-10-21',
                          'source': 'https://opencode.ai/v2/docs/console/models/'},
            repo_code_approved=True,
            operational_status='healthy')
        cls.registry = registry

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()

    def setUp(self):
        type(self).behaviour = lambda body, seen_before: ('sse', text_reply())

    def start_proxy(self, log_path=None):
        bound = proxy.validate_bound_model(MODEL, self.registry, set())
        server, px = proxy.serve('REAL-KEY', f'http://127.0.0.1:{self.upstream.server_address[1]}', bound,
                                 log_path=log_path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server, px

    def command_and_env(self, tmp, config, cli_model=MODEL, extra_env=None):
        home, work = os.path.join(tmp, 'home'), os.path.join(tmp, 'work')
        os.makedirs(home)
        os.makedirs(work)
        Path(work, 'normal.txt').write_text('ok-normal')
        Path(work, '.env').write_text('SECRET=1')
        env = {'HOME': home, 'PATH': '/usr/bin:/bin', 'PWD': work, 'XDG_CONFIG_HOME': home + '/.config',
               'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
               'XDG_STATE_HOME': home + '/.local/state', 'OPENCODE_CONFIG_CONTENT': json.dumps(config),
               'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_CONFIG_PROJECT_DISABLE': '1',
               'OPENCODE_DISABLE_CLAUDE_CODE': '1', 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1',
               'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1', 'CI': 'true', **(extra_env or {})}
        command = ['env', '-i', *[f'{k}={v}' for k, v in env.items()], BIN, 'run', '--standalone', '--format', 'json',
                   '--model', cli_model, '--', 'hi']
        return command, work

    def run_agent(self, cli_model=MODEL, mutate=None, mode='implement', timeout=60):
        del self.seen[:]
        server, px = self.start_proxy()
        config = sandbox.agent_config(MODEL, server.server_address[1], mode)
        if mutate:
            mutate(config)
        with tempfile.TemporaryDirectory() as tmp:
            command, work = self.command_and_env(tmp, config, cli_model)
            try:
                done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=timeout,
                                      stdin=subprocess.DEVNULL)
                out = (done.stdout, done.returncode, sorted(set(os.listdir(work)) - {'normal.txt', '.env'}))
            except subprocess.TimeoutExpired as expired:
                stdout = expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or '')
                out = (stdout, 124, sorted(set(os.listdir(work)) - {'normal.txt', '.env'}))
        return out, px, list(self.seen)

    def verdict(self, out):
        return guard.evaluate(out[0].splitlines(), out[1], False)

    def test_binary_is_the_pinned_version(self):
        self.assertEqual(self.version, f'opencode v{PINNED_VERSION}')

    def test_normal_run_reaches_upstream_only_with_the_bound_model_and_the_real_key(self):
        out, px, seen = self.run_agent()
        self.assertEqual(self.verdict(out), ('success', ''))
        self.assertTrue(seen)
        self.assertEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertEqual({s['auth'] for s in seen}, {'Bearer REAL-KEY'})   # placeholder never reaches upstream
        self.assertEqual({s['path'] for s in seen}, {'/inference/openai/v1/chat/completions'})
        self.assertEqual(px.rejections, [])

    def test_streaming_reply_is_a_text_only_lifecycle_the_guard_accepts(self):
        out, _px, _seen = self.run_agent()
        types = [json.loads(line)['type'] for line in out[0].splitlines()]
        self.assertEqual(types, ['text'])
        self.assertTrue(guard.probe_acknowledged([json.dumps({'type': 'text', 'part': {'text': 'PROBE_OK'}})]))

    def test_proxy_relays_the_genuine_client_identity_headers_unchanged_and_adds_none(self):
        _out, _px, seen = self.run_agent()
        headers = seen[0]['headers']
        self.assertEqual(headers.get('x-opencode-client'), 'cli')
        self.assertRegex(headers.get('user-agent', ''), rf'^opencode/[^/]+/{PINNED_VERSION}/cli$')
        self.assertRegex(headers.get('x-opencode-session', ''), r'^ses_[A-Za-z0-9]{8,64}$')
        self.assertEqual(headers.get('x-opencode-session'), headers.get('x-opencode-session-id'))
        self.assertRegex(headers.get('x-opencode-project', ''), r'^[0-9a-f]{40}$')
        self.assertEqual(headers['authorization'], 'Bearer REAL-KEY')

    def test_cli_model_substitution_is_stopped_at_the_proxy(self):
        out, px, seen = self.run_agent(cli_model='opencode/deepseek-v4.1-flash-free', timeout=40)
        # The substituted model never reached the provider. (The session-title side request still
        # uses the bound model through small_model, which is allowed.)
        self.assertNotIn('deepseek-v4.1-flash-free', {s['model'] for s in seen})
        self.assertLessEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertTrue(any(r['rejected'] == 'model_mismatch' for r in px.rejections), px.rejections)
        self.assertEqual(self.verdict(out)[0], 'failed')

    def test_config_override_naming_another_model_is_stopped_at_the_proxy(self):
        def swap(config):
            config['model'] = 'opencode/mimo-v2.6-flash-free'
            config['small_model'] = 'opencode/mimo-v2.6-flash-free'
        out, px, seen = self.run_agent(cli_model='opencode/mimo-v2.6-flash-free', mutate=swap, timeout=40)
        self.assertEqual(seen, [])
        self.assertTrue(px.rejections)
        self.assertEqual(self.verdict(out)[0], 'failed')

    def test_side_requests_for_another_model_are_blocked_but_the_run_still_succeeds(self):
        # Without small_model OpenCode asks a different model, over a different API, to title the
        # session. That is model substitution; the proxy refuses it and the main run is unaffected.
        out, px, seen = self.run_agent(mutate=lambda config: config.pop('small_model'))
        self.assertEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertTrue(any(r['rejected'] == 'endpoint_not_allowed' for r in px.rejections), px.rejections)
        self.assertEqual(self.verdict(out), ('success', ''))

    def test_environment_cannot_redirect_the_provider_around_the_proxy(self):
        # The firewall in the workflow stops direct egress; here we show that pointing the provider at
        # a different port than the proxy simply fails, it cannot reach upstream with the real key.
        def redirect(config):
            config['provider']['opencode']['options']['baseURL'] = 'http://127.0.0.1:9/inference/openai/v1'
        out, _px, seen = self.run_agent(mutate=redirect, timeout=25)
        self.assertEqual(seen, [])
        self.assertEqual(self.verdict(out)[0], 'failed')

    # --- sandbox permissions, enforced by the real v2 permission engine ---------------------------------------

    def tool_outcome(self, mode, name, arguments):
        type(self).behaviour = lambda body, n: ('sse', tool_reply(name, arguments) if body.get('tools') and 'tool' not in
                                                 [m.get('role') for m in body['messages']] else text_reply('done'))
        out, _px, seen = self.run_agent(mode=mode)
        results = [json.loads(line)['part'] for line in out[0].splitlines() if json.loads(line)['type'] == 'tool_use']
        offered = seen[0]['tools'] if seen else False
        return results, out[2], offered

    def test_implement_mode_can_edit_inside_the_work_tree_only(self):
        results, made, _ = self.tool_outcome('implement', 'write', {'path': 'new.txt', 'content': 'x'})
        self.assertEqual([r['state']['status'] for r in results], ['completed'])
        self.assertEqual(made, ['new.txt'])      # landed in the sandbox work tree, not a parent directory

    def test_review_mode_cannot_edit(self):
        results, made, _ = self.tool_outcome('review', 'write', {'path': 'new.txt', 'content': 'x'})
        self.assertEqual([r['state']['status'] for r in results], ['error'])
        self.assertEqual(made, [])

    def test_probe_mode_is_offered_no_tools_at_all(self):
        results, made, offered = self.tool_outcome('probe', 'write', {'path': 'new.txt', 'content': 'x'})
        self.assertFalse(offered)
        self.assertEqual((results, made), ([], []))

    def test_env_files_and_paths_outside_the_work_tree_cannot_be_read(self):
        env_result, _, _ = self.tool_outcome('implement', 'read', {'path': '.env'})
        self.assertEqual([r['state']['status'] for r in env_result], ['error'])
        outside, _, _ = self.tool_outcome('implement', 'read', {'path': '/etc/hostname'})
        self.assertEqual([r['state']['status'] for r in outside], ['error'])
        normal, _, _ = self.tool_outcome('implement', 'read', {'path': 'normal.txt'})
        self.assertEqual([r['state']['status'] for r in normal], ['completed'])   # control: reads are not all denied

    def test_v2_only_tools_that_run_code_or_reach_out_are_not_offered_in_any_editing_mode(self):
        arguments = {'execute': {'code': 'return 1'}, 'subagent': {'agent': 'general', 'description': 'd', 'prompt': 'p'},
                     'skill': {'id': 'x'}, 'question': {'questions': []}, 'webfetch': {'url': 'http://127.0.0.1:1/'},
                     'websearch': {'query': 'q'}}
        for mode in ('implement', 'review'):
            for name, args in arguments.items():
                results, made, _ = self.tool_outcome(mode, name, args)
                self.assertEqual([r['state']['status'] for r in results], ['error'], (mode, name))
                self.assertEqual(made, [], (mode, name))

    # --- provider rejection: prompt, bounded, sanitized ------------------------------------------------------

    def run_with_guard(self, status, limit_seconds=30):
        type(self).behaviour = lambda body, n: ('status', status)
        del self.seen[:]
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'proxy.log')
            server, _px = self.start_proxy(log)
            config = sandbox.agent_config(MODEL, server.server_address[1], 'probe')
            command, work = self.command_and_env(tmp, config)
            started = time.monotonic()
            stdout, _stderr, code, rejection = guard.run_agent(command, work, limit_seconds, log,
                                                               stop=lambda process: process.kill())
            return stdout, code, rejection, time.monotonic() - started, Path(log).read_text()

    def test_http_429_is_reported_promptly_instead_of_waiting_for_the_timeout(self):
        _out, _code, rejection, elapsed, log = self.run_with_guard(429)
        self.assertEqual(rejection, 'provider_http_429')
        self.assertLess(elapsed, 20)            # v2 alone retries a 429 until the 120 s probe timeout
        self.assertNotIn('synthetic', log)      # response content is never logged
        self.assertNotIn('REAL-KEY', log)

    def test_http_401_is_reported_as_auth_rejected(self):
        self.assertEqual(self.run_with_guard(401)[2], 'provider_auth_rejected')

    def test_http_403_is_reported_without_guessing_why(self):
        self.assertEqual(self.run_with_guard(403)[2], 'provider_http_403')

    def test_repeated_server_errors_are_reported_as_provider_unavailable(self):
        self.assertEqual(self.run_with_guard(503, limit_seconds=40)[2], 'provider_unavailable')

    def test_a_rejection_overrides_whatever_the_transcript_says(self):
        out, code, rejection, _elapsed, _log = self.run_with_guard(429)
        self.assertEqual(rejection, 'provider_http_429')
        self.assertNotEqual(guard.evaluate(out.splitlines(), code, False), ('success', ''))


if __name__ == '__main__':
    unittest.main()
