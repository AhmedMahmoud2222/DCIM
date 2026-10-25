"""End-to-end test with the REAL OpenCode 1.19.3 binary, the model-bound proxy and a stub upstream.

Runs only when OPENCODE_BIN points at the pinned binary (CI installs it from the committed
lockfile; locally: `cd .github/opencode/runtime && npm ci --ignore-scripts`). Otherwise every
test is SKIPPED, and a skip is not a pass.
"""
import http.server
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
proxy = importlib.import_module('opencode_proxy')
sandbox = importlib.import_module('opencode_sandbox')
guard = importlib.import_module('opencode_guard')

DEFAULT_BIN = ROOT / '.github/opencode/runtime/node_modules/opencode-linux-x64/bin/opencode'
BIN = os.environ.get('OPENCODE_BIN') or (str(DEFAULT_BIN) if DEFAULT_BIN.exists() else '')
MODEL = 'opencode/glm-5.3-flash-free'


@unittest.skipUnless(BIN and os.path.exists(BIN), 'real OpenCode 1.19.3 binary not installed (set OPENCODE_BIN)')
class RealOpenCodeThroughProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version = subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip()
        cls.seen = []

        class Stub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                cls.seen.append({'path': self.path, 'model': body.get('model'), 'auth': self.headers.get('Authorization')})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                piece = lambda d, f=None: 'data: ' + json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm', 'choices': [{'index': 0, 'delta': d, 'finish_reason': f}]}) + '\n\n'
                self.wfile.write((piece({'role': 'assistant', 'content': ''}) + piece({'content': 'ok'}) + piece({}, 'stop') + 'data: [DONE]\n\n').encode())

        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        registry = json.loads((ROOT / '.github/opencode/model-registry.json').read_text())
        registry['models'][MODEL].update(
            terms_verified=True,
            terms_review={'reviewed_by': 't', 'date': '2026-10-21',
                          'source': 'https://opencode.ai/docs/zen'},
            repo_code_approved=True,
            operational_status='healthy')
        cls.registry = registry

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()

    def run_agent(self, cli_model=MODEL, mutate=None, extra_env=None):
        del self.seen[:]
        bound = proxy.validate_bound_model(MODEL, self.registry, set())
        server, px = proxy.serve('REAL-KEY', f'http://127.0.0.1:{self.upstream.server_address[1]}', bound)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        config = sandbox.agent_config(MODEL, server.server_address[1])
        if mutate:
            mutate(config)
        with tempfile.TemporaryDirectory() as tmp:
            home, work = os.path.join(tmp, 'home'), os.path.join(tmp, 'work')
            os.makedirs(home)
            os.makedirs(work)
            env = {'HOME': home, 'PATH': '/usr/bin:/bin', 'XDG_CONFIG_HOME': home + '/.config',
                   'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
                   'XDG_STATE_HOME': home + '/.local/state', 'OPENCODE_CONFIG_CONTENT': json.dumps(config),
                   'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_PURE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
                   'OPENCODE_DISABLE_AUTOUPDATE': '1', 'CI': 'true', **(extra_env or {})}
            done = subprocess.run(['timeout', '150', BIN, 'run', '--pure', '--format', 'json', '--model', cli_model,
                                   '--dir', work, '--', 'hi'], capture_output=True, text=True, env=env)
        return done, px, list(self.seen)

    def test_binary_is_the_pinned_version(self):
        self.assertEqual(self.version, '1.19.3')

    def test_normal_run_reaches_upstream_only_with_the_bound_model_and_the_real_key(self):
        done, px, seen = self.run_agent()
        self.assertEqual(guard.evaluate(done.stdout.splitlines(), done.returncode, False), ('success', ''))
        self.assertTrue(seen)
        self.assertEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertEqual({s['auth'] for s in seen}, {'Bearer REAL-KEY'})   # placeholder never reaches upstream
        self.assertEqual({s['path'] for s in seen}, {'/inference/openai/v1/chat/completions'})
        self.assertEqual(px.rejections, [])

    def test_cli_model_substitution_is_stopped_at_the_proxy(self):
        done, px, seen = self.run_agent(cli_model='opencode/deepseek-v4.1-flash-free')
        # The substituted model never reached the provider. (The session-title side request still
        # uses the bound model through small_model, which is allowed.)
        self.assertNotIn('deepseek-v4.1-flash-free', {s['model'] for s in seen})
        self.assertLessEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertTrue(any(r['rejected'] == 'model_mismatch' for r in px.rejections), px.rejections)
        self.assertEqual(guard.evaluate(done.stdout.splitlines(), done.returncode, False)[0], 'failed')

    def test_config_override_naming_another_model_is_stopped_at_the_proxy(self):
        def swap(config):
            config['model'] = 'opencode/mimo-v2.6-flash-free'
            config['small_model'] = 'opencode/mimo-v2.6-flash-free'
        done, px, seen = self.run_agent(cli_model='opencode/mimo-v2.6-flash-free', mutate=swap)
        self.assertEqual(seen, [])
        self.assertTrue(px.rejections)
        self.assertEqual(guard.evaluate(done.stdout.splitlines(), done.returncode, False)[0], 'failed')

    def test_side_requests_for_another_model_are_blocked_but_the_run_still_succeeds(self):
        # Without small_model OpenCode asks a different model, over a different API, to title the
        # session. That is model substitution; the proxy refuses it and the main run is unaffected.
        done, px, seen = self.run_agent(mutate=lambda config: config.pop('small_model'))
        self.assertEqual({s['model'] for s in seen}, {'glm-5.3-flash-free'})
        self.assertTrue(any(r['rejected'] == 'endpoint_not_allowed' for r in px.rejections), px.rejections)
        self.assertEqual(guard.evaluate(done.stdout.splitlines(), done.returncode, False), ('success', ''))

    def test_environment_cannot_redirect_the_provider_around_the_proxy(self):
        # The firewall in the workflow stops direct egress; here we show that pointing the provider at
        # a different port than the proxy simply fails, it cannot reach upstream with the real key.
        def redirect(config):
            config['provider']['opencode']['options']['baseURL'] = 'http://127.0.0.1:9/inference/openai/v1'
        done, _px, seen = self.run_agent(mutate=redirect)
        self.assertEqual(seen, [])
        self.assertEqual(guard.evaluate(done.stdout.splitlines(), done.returncode, False)[0], 'failed')


if __name__ == '__main__':
    unittest.main()
