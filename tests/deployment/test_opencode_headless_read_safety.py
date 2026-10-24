"""Headless read-permission safety for the native OpenCode CLI smoke.

Exercises the REAL pinned OpenCode 1.19.3 permission engine in an unattended
(headless, no TTY) run against a deterministic local stub model and proves, on
the exact linux-x64 binary the CI runner installs from the committed lockfile:

  1. `read: 'ask'` is NOT auto-approved when stdin is disabled;
  2. a read of a canary inside the working directory never reaches the
     provider;
  3. a read of a canary outside the working directory never reaches the
     provider;
  4. an environment-sensitive path (/proc/self/environ) never reaches the
     provider, in a child process whose environment holds only synthetic
     values;
  5. `external_directory: 'deny'` blocks out-of-scope access even when
     `read` is 'allow';
  6. the transcript verifier rejects the REAL tool-use transcript even though
     the CLI exits 0.

This is P1 pre-dispatch gate evidence for the native smoke diagnostic: it
drives the genuine runtime permission engine rather than feeding fabricated
events to the classifier.

Safety properties of this test itself:

  - synthetic credentials only. The model is a loopback HTTP stub; the child
    process environment contains no real secret and inherits nothing.
  - the real gateway is never contacted: the provider base URL is pinned to
    the loopback stub for every run.
  - nothing sensitive is written to an artifact. Assertions are boolean and
    the failure messages never contain canary content, environment values or
    raw transcripts.
  - the permission block under test is taken verbatim from the diagnostic
    script (`smoke.PERMISSIONS`), so the test cannot drift from the shipped
    configuration.
  - fail closed: if the runtime cannot be exercised, the test fails rather
    than passing vacuously.

The test runs only where the pinned linux-x64 binary is installed (CI installs
it before the suite). Elsewhere it is skipped, and a skip is not a pass.
"""
import http.server
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    'opencode_native_smoke',
    ROOT / '.github/scripts/opencode_native_smoke.py',
)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

DEFAULT_BIN = (ROOT / '.github/opencode/runtime/node_modules'
               '/opencode-linux-x64/bin/opencode')
BIN = os.environ.get('OPENCODE_BIN') or (
    str(DEFAULT_BIN) if DEFAULT_BIN.exists() else '')

MODEL = smoke.MODEL
PROMPT = smoke.PROMPT

# Synthetic markers. They are test fixtures, not secrets; assertions never
# embed them in failure messages.
CANARY_INSIDE = 'CANARY-INSIDE-HEADLESS-READ-SAFETY'
CANARY_OUTSIDE = 'CANARY-OUTSIDE-HEADLESS-READ-SAFETY'
ENV_MARKER = 'SYNTHETIC-ENV-MARKER-HEADLESS-READ-SAFETY'
SYNTHETIC_KEY = 'synthetic-not-a-credential-headless-read-safety'

TIMEOUT_S = 120
ENV_SENSITIVE_PATH = '/proc/self/environ'


def _sse(chunks):
    return (''.join(f'data: {json.dumps(c)}\n\n' for c in chunks)
            + 'data: [DONE]\n\n')


def _chunk(delta, finish=None):
    return {'id': 'chatcmpl-1', 'object': 'chat.completion.chunk',
            'created': 1, 'model': 'glm-5.3-flash-free',
            'choices': [{'index': 0, 'delta': delta,
                         'finish_reason': finish}]}


def _text_reply(text):
    return _sse([_chunk({'role': 'assistant', 'content': ''}),
                 _chunk({'content': text}), _chunk({}, 'stop')])


def _read_reply(path):
    """A model turn that asks the runtime to read `path`."""
    arguments = json.dumps({'filePath': path})
    return _sse([
        _chunk({'role': 'assistant', 'content': None, 'tool_calls': [
            {'index': 0, 'id': 'call_1', 'type': 'function',
             'function': {'name': 'read', 'arguments': ''}}]}),
        _chunk({'tool_calls': [{'index': 0,
                                'function': {'arguments': arguments}}]}),
        _chunk({}, 'tool_calls'),
    ])


class _Stub(http.server.BaseHTTPRequestHandler):
    """Deterministic local model stub. Only the model turns are scripted."""

    steps: list = []
    requests: list = []

    def log_message(self, *args):  # silence stderr noise
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw or b'{}')
        except ValueError:
            request = {}
        _Stub.requests.append(request)
        if 'tools' not in request:
            # Auxiliary title/small_model request: no tool envelope.
            payload = _text_reply('Task')
        elif _Stub.steps:
            payload = _Stub.steps.pop(0)
        else:
            payload = _text_reply('OK')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(payload.encode())
        self.wfile.flush()


@unittest.skipUnless(BIN and os.path.exists(BIN),
                     'real OpenCode 1.19.3 binary not installed '
                     '(set OPENCODE_BIN)')
class RealHeadlessReadSafetyTests(unittest.TestCase):
    """The real permission engine, headless, with synthetic credentials."""

    @classmethod
    def setUpClass(cls):
        cls.version = subprocess.run(
            [BIN, '--version'], capture_output=True, text=True,
            timeout=60).stdout.strip()

    # -- harness ----------------------------------------------------------

    def _run_variant(self, read_permission, stdin_mode='DEVNULL'):
        """Run the real CLI headless with `read: <read_permission>`.

        Returns sanitized booleans and the REAL transcript lines so the
        production classifier can be exercised on genuine runtime output.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, 'home')
            work = os.path.join(tmp, 'work')
            outside = os.path.join(tmp, 'outside')
            for directory in (home, work, outside):
                os.makedirs(directory)
            inside_path = os.path.join(work, 'canary_inside.txt')
            outside_path = os.path.join(outside, 'canary_outside.txt')
            with open(inside_path, 'w', encoding='utf-8') as handle:
                handle.write(CANARY_INSIDE + '\n')
            with open(outside_path, 'w', encoding='utf-8') as handle:
                handle.write(CANARY_OUTSIDE + '\n')

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Stub)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]

            # The configuration under test is the diagnostic's own config;
            # only the provider endpoint is redirected to the loopback stub
            # so the real gateway is never contacted, and only the `read`
            # key is varied between variants.
            config = smoke.smoke_config()
            config['permission'] = dict(smoke.PERMISSIONS)
            config['permission']['read'] = read_permission
            config['provider'] = {'opencode': {'options': {
                'baseURL': f'http://127.0.0.1:{port}/v1',
                'apiKey': SYNTHETIC_KEY,
            }}}

            env = {
                'HOME': home,
                'XDG_CONFIG_HOME': os.path.join(home, '.config'),
                'XDG_DATA_HOME': os.path.join(home, '.local', 'share'),
                'XDG_CACHE_HOME': os.path.join(home, '.cache'),
                'XDG_STATE_HOME': os.path.join(home, '.local', 'state'),
                'PATH': '/usr/local/bin:/usr/bin:/bin',
                'LANG': 'C.UTF-8',
                'CI': 'true',
                'OPENCODE_API_KEY': SYNTHETIC_KEY,
                'OPENCODE_CONFIG_CONTENT': json.dumps(config),
                'OPENCODE_DISABLE_AUTOUPDATE': '1',
                'OPENCODE_DISABLE_MODELS_FETCH': '1',
                'OPENCODE_PURE': '1',
                # Synthetic environment marker: a stand-in for the shape of
                # the real key's location (/proc/self/environ) without ever
                # holding a real credential.
                ENV_MARKER: 'synthetic-value',
            }

            _Stub.steps = [
                _read_reply(inside_path),
                _read_reply(outside_path),
                _read_reply(ENV_SENSITIVE_PATH),
                _text_reply('OK'),
            ]
            _Stub.requests = []

            command = [BIN, 'run', '--pure', '--format', 'json',
                       '--model', MODEL, '--dir', work, '--', PROMPT]
            stdin = subprocess.DEVNULL if stdin_mode == 'DEVNULL' else None
            timed_out = False
            exit_code = None
            stdout = ''
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, env=env,
                    cwd=work, timeout=TIMEOUT_S,
                    stdin=subprocess.DEVNULL if stdin_mode == 'DEVNULL'
                    else subprocess.PIPE)
                exit_code = completed.returncode
                stdout = completed.stdout
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                server.shutdown()
                server.server_close()

            transcript = [line for line in stdout.splitlines() if line.strip()]
            recorded = json.dumps(_Stub.requests)
            return {
                'exit_code': exit_code,
                'timed_out': timed_out,
                'transcript': transcript,
                'request_count': len(_Stub.requests),
                'leaked_inside': CANARY_INSIDE in recorded,
                'leaked_outside': CANARY_OUTSIDE in recorded,
                'leaked_env': ENV_MARKER in recorded,
                'child_env_keys': sorted(env),
            }

    @staticmethod
    def _tool_use_events(transcript):
        """Event kinds and tool-use statuses parsed from the real transcript."""
        kinds = []
        for line in transcript:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get('type')
            if kind == 'tool_use':
                part = event.get('part') or {}
                state = (part.get('state') or {}).get('status')
                kinds.append(f'tool_use({state})')
            elif kind == 'error':
                kinds.append('error')
            else:
                kinds.append(kind)
        return kinds

    # -- environment fidelity --------------------------------------------

    def test_binary_is_the_pinned_version(self):
        self.assertEqual(self.version, '1.19.3')

    def test_permission_block_under_test_is_the_diagnostic_configuration(self):
        # The probe must exercise exactly what the diagnostic ships.
        self.assertEqual(smoke.PERMISSIONS['read'], 'ask')
        self.assertEqual(smoke.PERMISSIONS['external_directory'], 'deny')
        for tool in ('edit', 'bash', 'webfetch', 'task', 'todowrite'):
            self.assertEqual(smoke.PERMISSIONS[tool], 'deny', tool)

    # -- 1. read: ask is not auto-approved headless -----------------------

    def test_read_ask_is_not_auto_approved_with_stdin_disabled(self):
        result = self._run_variant('ask')
        self.assertFalse(result['timed_out'],
                         'read: ask headless run did not terminate')
        kinds = self._tool_use_events(result['transcript'])
        self.assertIn('tool_use(error)', kinds)
        # The runtime attempted the read and the permission engine refused
        # it: exactly one tool-use attempt, rejected.
        self.assertEqual(sum(1 for k in kinds if k.startswith('tool_use(')), 1)

    def test_read_ask_does_not_leak_the_in_workdir_canary(self):
        result = self._run_variant('ask')
        self.assertFalse(result['leaked_inside'],
                         'read: ask leaked the in-workdir canary to the model')

    def test_read_ask_does_not_leak_the_out_of_workdir_canary(self):
        result = self._run_variant('ask')
        self.assertFalse(result['leaked_outside'],
                         'read: ask leaked the out-of-workdir canary')

    def test_read_ask_does_not_leak_environment_values(self):
        result = self._run_variant('ask')
        self.assertFalse(result['leaked_env'],
                         'read: ask leaked an environment value to the model')

    def test_read_ask_does_not_hang_with_an_idle_stdin_pipe(self):
        result = self._run_variant('ask', stdin_mode='PIPE')
        self.assertFalse(result['timed_out'],
                         'read: ask hung with an idle stdin pipe')
        self.assertFalse(result['leaked_inside'],
                         'read: ask leaked the canary with an idle stdin pipe')

    # -- 2. the transcript verifier rejects the real transcript ----------

    def test_transcript_verifier_rejects_the_real_tool_use_transcript(self):
        # The genuine runtime output (not a fabricated event) must be
        # rejected by the shipped classifier even though the CLI exits 0.
        result = self._run_variant('ask')
        self.assertEqual(result['exit_code'], 0,
                         'expected the CLI to exit 0 despite the refusal')
        status, reason, _error_class, _code = smoke.evaluate(
            result['transcript'], result['exit_code'], result['timed_out'])
        self.assertEqual(status, 'failed')
        self.assertEqual(reason, 'unexpected_tool_use')

    def test_control_read_allow_is_detected_so_the_probe_is_not_vacuous(self):
        # With `read: allow` the runtime really does read the canary, which
        # proves the detection above is meaningful rather than always green.
        result = self._run_variant('allow')
        self.assertFalse(result['timed_out'],
                         'read: allow headless run did not terminate')
        self.assertTrue(result['leaked_inside'],
                        'read: allow did not read the in-workdir canary; '
                        'the probe cannot detect a leak')

    # -- 3. external_directory: deny bounds the reachable scope ----------

    def test_external_directory_deny_blocks_out_of_scope_reads(self):
        # Even with `read: allow`, the out-of-workdir canary and the
        # environment-sensitive path must not reach the model.
        result = self._run_variant('allow')
        self.assertFalse(result['leaked_outside'],
                         'external_directory: deny did not block the '
                         'out-of-workdir canary')
        self.assertFalse(result['leaked_env'],
                         'external_directory: deny did not block the '
                         'environment-sensitive path')

    def test_external_directory_deny_is_enforced_even_under_read_ask(self):
        result = self._run_variant('ask')
        self.assertFalse(result['leaked_outside'],
                         'out-of-workdir read reached the model under ask')
        self.assertFalse(result['leaked_env'],
                         'environment-sensitive read reached the model '
                         'under ask')

    # -- 4. the child process holds only synthetic values -----------------

    def test_child_process_environment_is_isolated_and_synthetic(self):
        result = self._run_variant('ask')
        # The child environment is the diagnostic's closed allowlist plus
        # the one synthetic marker this probe needs. Nothing else is
        # inherited: no runner token, no user configuration, no real secret.
        self.assertEqual(result['child_env_keys'],
                         sorted(set(smoke.CHILD_ENV_KEYS) | {ENV_MARKER}))
        for forbidden in ('GITHUB_TOKEN', 'GITHUB_ACTIONS', 'AWS_',
                          'ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
            self.assertFalse(
                [key for key in result['child_env_keys']
                 if key.startswith(forbidden)],
                f'unexpected credential-bearing variable in child env')


if __name__ == '__main__':
    unittest.main()
