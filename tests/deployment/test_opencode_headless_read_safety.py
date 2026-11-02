#!/usr/bin/env python3
"""Headless read-permission safety for the native OpenCode CLI smoke (v2).

Exercises the REAL pinned OpenCode v2 (@opencode/cli 2.1.4) permission engine
in an unattended (headless, no TTY) run against a deterministic local stub
model and proves, on the exact linux-x64 binary the CI runner installs from
the committed lockfile, with the smoke's own shipped config, command line,
child environment and runner (`smoke._run_cli`):

  1. `read: 'ask'` is NOT auto-approved headlessly: v2 rejects the call
     ("This non-interactive run cannot ask the user for permission");
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
     the CLI exits 0;
  7. with an *open but idle* stdin the runtime neither executes a read nor
     contacts the provider (v2, like v1, blocks before any model
     interaction); a hard timeout terminates the blocked run and reaps it;
  8. (new in v2) the shipped config offers the model exactly one tool,
     `read`; v2's new actions (`execute` Code Mode, `subagent`, `skill`,
     `question`, ...) are absent, and a model call to one of them fails
     without disclosing anything. Control: the legacy v1 `permission` map
     alone DOES expose them, because v2 auto-approves unmatched actions —
     which is why the native `permissions` list is load-bearing;
  9. (new in v2) a provider that fails with HTTP 500 forever is retried by v2
     forever; the smoke runner's hard timeout is what ends that run, and it
     kills and reaps the whole process group.

v1 -> v2 differences these tests encode (all measured on 2.1.4, not carried
over): the `ask` rejection message changed (v1: "rejected permission"); the
out-of-workdir refusal is "Permission denied: external_directory" (v1: "rule
which prevents"); read takes `path` (v1: `filePath`); the CLI has no --pure
or --dir (cwd + $PWD select the workdir); open idle stdin still blocks before
any provider request, with and without `read: ask`.

This is pre-dispatch gate evidence for the native smoke diagnostic: it drives
the genuine runtime permission engine rather than feeding fabricated events
to the classifier.

Each sensitive path is exercised in its OWN CLI run, and every assertion
below first proves that the transcript contains a read attempt for the
*specific* target under test, and only then asserts non-disclosure.

Safety properties of this test itself:

  - synthetic credentials only. The model is a loopback HTTP stub; the child
    process environment is the smoke's closed allowlist plus one synthetic
    marker and inherits nothing.
  - the real gateway is never contacted: the provider base URL is pinned to
    the loopback stub for every run.
  - nothing sensitive is written to an artifact. Assertions are boolean and
    the failure messages never contain canary content, environment values or
    raw transcripts.
  - the configuration under test is taken from the diagnostic script
    (`smoke.smoke_config()`), so the test cannot drift from the shipped
    configuration; only the provider endpoint is redirected.
  - fail closed: if the runtime cannot be exercised, the test fails rather
    than passing vacuously.

The test runs only where the pinned linux-x64 binary is installed (CI installs
it before the suite) or OPENCODE_BIN points at it. Elsewhere it is skipped,
and a skip is not a pass.
"""
import http.server
import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    'opencode_native_smoke',
    ROOT / '.github/scripts/opencode_native_smoke.py',
)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

DEFAULT_BIN = (ROOT / '.github/opencode/runtime/node_modules'
               '/@opencode/cli-linux-x64/bin/opencode')
BIN = os.environ.get('OPENCODE_BIN') or (
    str(DEFAULT_BIN) if DEFAULT_BIN.exists() else '')

MODEL = smoke.MODEL

# Synthetic markers. They are test fixtures, not secrets; assertions never
# embed them in failure messages.
CANARY_INSIDE = 'CANARY-INSIDE-HEADLESS-READ-SAFETY'
CANARY_OUTSIDE = 'CANARY-OUTSIDE-HEADLESS-READ-SAFETY'
ENV_MARKER = 'SYNTHETIC_ENV_MARKER_HEADLESS_READ_SAFETY'
ENV_MARKER_VALUE = 'SYNTHETIC-ENV-VALUE-HEADLESS-READ-SAFETY'
SYNTHETIC_KEY = 'synthetic-not-a-credential-headless-read-safety'

TIMEOUT_S = 120
IDLE_TIMEOUT_S = 30      # the idle-stdin run is expected to block this long
RETRY_TIMEOUT_S = 8      # bound for the endless-retry run
ENV_SENSITIVE_PATH = '/proc/self/environ'

# The three sensitive paths under test. Each is exercised in an independent
# CLI run.
TARGET_INSIDE = 'inside'
TARGET_OUTSIDE = 'outside'
TARGET_ENVIRON = 'environ'

# v2 actions that the smoke must never offer to the model.
V2_FORBIDDEN_TOOLS = ('execute', 'shell', 'bash', 'write', 'edit', 'glob',
                      'grep', 'subagent', 'task', 'skill', 'question',
                      'webfetch', 'websearch')


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


def _tool_reply(name, arguments):
    """A model turn that calls tool `name` with `arguments`."""
    return _sse([
        _chunk({'role': 'assistant', 'content': None, 'tool_calls': [
            {'index': 0, 'id': 'call_1', 'type': 'function',
             'function': {'name': name, 'arguments': ''}}]}),
        _chunk({'tool_calls': [{'index': 0, 'function': {
            'arguments': json.dumps(arguments)}}]}),
        _chunk({}, 'tool_calls'),
    ])


def _read_reply(path):
    """A model turn that asks the runtime to read exactly `path` (v2: `path`)."""
    return _tool_reply('read', {'path': path})


class _Stub(http.server.BaseHTTPRequestHandler):
    """Deterministic local model stub. Only the model turns are scripted."""

    protocol_version = 'HTTP/1.0'
    steps: ClassVar[list] = []
    requests: ClassVar[list] = []
    status: ClassVar[int] = 200

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
        if _Stub.status != 200:
            body = b'{"error":{"message":"synthetic"}}'
            self.send_response(_Stub.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        roles = [m.get('role') for m in request.get('messages', [])]
        if not request.get('tools'):
            # Auxiliary title/small_model request: no tool envelope.
            payload = _text_reply('Task')
        elif 'tool' not in roles and _Stub.steps:
            payload = _Stub.steps.pop(0)
        else:
            payload = _text_reply('OK')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(payload.encode())
        self.wfile.flush()


def _tool_use_events(transcript):
    """Parse the REAL transcript into tool-use descriptors.

    Each descriptor carries the path the runtime was actually asked to read
    (`input.path` in v2), the terminal status, and a coarse rejection kind so
    that the refusal mechanisms can be told apart (v2 2.1.4 wording):

      'permission'  - `ask` could not be answered (non-interactive run)
      'rule'        - a configured rule forbids the call (external_directory)
      'unavailable' - the tool is not offered at all (denied action)
      'completed'   - the tool ran
      'other'       - anything else
    """
    events = []
    for line in transcript:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        state = part.get('state') or {}
        status = state.get('status')
        requested = (state.get('input') or {}).get('path')
        error = state.get('error')
        if status == 'completed':
            kind = 'completed'
        elif isinstance(error, str) and 'cannot ask the user for permission' in error:
            kind = 'permission'
        elif isinstance(error, str) and error.startswith('Permission denied: external_directory'):
            kind = 'rule'
        elif isinstance(error, str) and error.startswith('No tool named'):
            kind = 'unavailable'
        else:
            kind = 'other'
        events.append({'tool': part.get('tool'), 'path': requested,
                       'status': status, 'kind': kind})
    return events


def _alive(pid: int) -> bool:
    """True while pid exists and is not a zombie."""
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as handle:
            return handle.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


@unittest.skipUnless(BIN and os.path.exists(BIN),
                     'real OpenCode v2 binary not installed '
                     '(set OPENCODE_BIN)')
class RealHeadlessReadSafetyTests(unittest.TestCase):
    """The real permission engine, headless, with synthetic credentials."""

    @classmethod
    def setUpClass(cls):
        cls.version = subprocess.run(
            [BIN, '--version'], check=False, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60).stdout.strip()
        # One CLI run per configuration; memoised so the assertions below
        # share a single execution per configuration.
        cls._cache: dict = {}

    # -- harness ----------------------------------------------------------

    @classmethod
    def _cached(cls, key, factory):
        if key not in cls._cache:
            cls._cache[key] = factory()
        return cls._cache[key]

    def _run(self, read_permission, target, stdin_mode='DEVNULL', *,
             step=None, legacy_only=False, http_status=200,
             timeout_s=TIMEOUT_S):
        """Run the real CLI headless against exactly one scripted model turn.

        By default the model is scripted to request ONE read of `target`, so
        the transcript is guaranteed to contain a read attempt for this
        specific target -- which is what makes the non-disclosure assertions
        below non-vacuous. `step` replaces that turn (e.g. a call to a tool
        that must not exist). DEVNULL runs go through the smoke's own runner
        (`smoke._run_cli`); the IDLE_PIPE control needs its own spawner since
        the runner never hands the CLI an open stdin.
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

            requested_path = {
                TARGET_INSIDE: inside_path,
                TARGET_OUTSIDE: outside_path,
                TARGET_ENVIRON: ENV_SENSITIVE_PATH,
            }[target]

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Stub)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]

            # The configuration under test is the diagnostic's own config;
            # only the provider endpoint is redirected to the loopback stub
            # so the real gateway is never contacted, and only the `read`
            # effect is varied between variants (in both dialects).
            config = smoke.smoke_config()
            config['permission']['read'] = read_permission
            config['permissions'] = [
                dict(rule, effect=read_permission) if rule['action'] == 'read'
                else rule for rule in config['permissions']]
            if legacy_only:
                del config['permissions']
            config['provider'] = {'opencode': {'options': {
                'baseURL': f'http://127.0.0.1:{port}/v1',
                'apiKey': SYNTHETIC_KEY,
            }}}

            env = smoke.build_child_env(home, work, SYNTHETIC_KEY)
            env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
            # Synthetic environment marker: a stand-in for the shape of the
            # real key's location (/proc/self/environ) without ever holding
            # a real credential.
            env[ENV_MARKER] = ENV_MARKER_VALUE

            _Stub.steps = [step or _read_reply(requested_path)]
            _Stub.requests = []
            _Stub.status = http_status

            command = smoke.build_command(BIN)
            try:
                if stdin_mode == 'IDLE_PIPE':
                    outcome = self._spawn_idle(command, env, work, timeout_s)
                else:
                    started = time.monotonic()
                    stdout, _stderr, exit_code, timed_out = smoke._run_cli(
                        command, env, work, timeout_s)
                    outcome = {'stdout': stdout,
                               'exit_code': exit_code, 'timed_out': timed_out,
                               'elapsed': time.monotonic() - started}
            finally:
                server.shutdown()
                server.server_close()
                _Stub.status = 200

            transcript = [line for line in outcome['stdout'].splitlines()
                          if line.strip()]
            recorded = json.dumps(_Stub.requests)
            events = _tool_use_events(transcript)
            attempted = [event['path'] for event in events]
            expected_real = os.path.realpath(requested_path)
            offered = [sorted(t['function']['name'] for t in request['tools'])
                       for request in _Stub.requests if request.get('tools')]
            return {
                'exit_code': outcome['exit_code'],
                'timed_out': outcome['timed_out'],
                'elapsed': outcome.get('elapsed'),
                'reaped': outcome.get('reaped', True),
                'transcript': transcript,
                'events': events,
                # The read attempt for THIS target is present in the real
                # transcript (compared on the resolved path so that a
                # normalised path still matches).
                'attempted_this_target': any(
                    isinstance(path, str)
                    and (path == requested_path
                         or os.path.realpath(path) == expected_real)
                    for path in attempted),
                'rejection_kinds': [event['kind'] for event in events],
                'statuses': [event['status'] for event in events],
                'tools_called': [event['tool'] for event in events],
                'leaked': any(marker in recorded for marker in
                              (CANARY_INSIDE, CANARY_OUTSIDE, ENV_MARKER_VALUE)),
                'request_count': len(_Stub.requests),
                'offered_tools': offered,
                'child_env_keys': sorted(env),
            }

    def _spawn_idle(self, command, env, cwd, timeout_s):
        """Start the CLI with an OPEN, IDLE stdin and reap it on every path.

        stdin is the read end of a pipe whose write end is held OPEN for the
        whole child execution and never written to, so the child observes an
        open, idle stream rather than EOF. `communicate(timeout=...)` bounds
        the run, the child is SIGKILLed on timeout, and
        every file descriptor is closed on every path.
        """
        read_fd, write_fd = os.pipe()
        process = None
        timed_out = False
        stdout = ''
        exit_code = None
        try:
            process = subprocess.Popen(
                command, stdin=read_fd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=cwd,
                text=True, start_new_session=True)
            # The child holds its own duplicate; the parent does not need the
            # read end. The WRITE end stays open on purpose.
            os.close(read_fd)
            read_fd = None
            try:
                stdout, _stderr = process.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, _stderr = process.communicate()
            exit_code = process.returncode
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            reaped = process is not None and process.poll() is not None
            if read_fd is not None:
                os.close(read_fd)
            # Closed only here, after the child has exited: the write end was
            # open for the child's entire lifetime.
            os.close(write_fd)
        return {'stdout': stdout or '', 'exit_code': exit_code,
                'timed_out': timed_out, 'reaped': reaped}

    def ask(self, target):
        return self._cached(('ask', target), lambda: self._run('ask', target))

    def allow(self, target):
        return self._cached(('allow', target),
                            lambda: self._run('allow', target))

    # -- environment fidelity --------------------------------------------

    def test_binary_is_the_pinned_version(self):
        self.assertEqual(self.version,
                         f'opencode v{smoke.EXPECTED_OPENCODE_VERSION}')
        self.assertEqual(smoke.parse_version(self.version),
                         smoke.EXPECTED_OPENCODE_VERSION)

    def test_permission_block_under_test_is_the_diagnostic_configuration(self):
        # The probe must exercise exactly what the diagnostic ships.
        config = smoke.smoke_config()
        self.assertEqual(config['permission']['read'], 'ask')
        self.assertEqual(config['permission']['external_directory'], 'deny')
        native = config['permissions']
        self.assertEqual(native[0], {'action': '*', 'resource': '*',
                                     'effect': 'deny'})
        self.assertEqual([r for r in native if r['effect'] != 'deny'],
                         [{'action': 'read', 'resource': '*', 'effect': 'ask'}])

    # -- 1. each sensitive path is attempted, then refused ----------------

    def test_inside_canary_read_is_attempted_and_refused_under_ask(self):
        result = self.ask(TARGET_INSIDE)
        self.assertFalse(result['timed_out'], 'the run did not terminate')
        # The attempt is really there: the runtime was asked for this path.
        self.assertTrue(result['attempted_this_target'],
                        'the transcript contains no read attempt for the '
                        'in-workdir canary')
        self.assertEqual(result['statuses'], ['error'])
        # Refused because no approver can answer `ask` headlessly.
        self.assertEqual(result['rejection_kinds'], ['permission'])
        self.assertFalse(result['leaked'],
                         'the in-workdir canary reached the provider')

    def test_outside_canary_read_is_attempted_and_refused_under_ask(self):
        result = self.ask(TARGET_OUTSIDE)
        self.assertFalse(result['timed_out'], 'the run did not terminate')
        self.assertTrue(result['attempted_this_target'],
                        'the transcript contains no read attempt for the '
                        'out-of-workdir canary')
        self.assertEqual(result['statuses'], ['error'])
        # Refused by the external_directory rule, not by the read setting.
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'the out-of-workdir canary reached the provider')

    def test_environ_read_is_attempted_and_refused_under_ask(self):
        result = self.ask(TARGET_ENVIRON)
        self.assertFalse(result['timed_out'], 'the run did not terminate')
        self.assertTrue(result['attempted_this_target'],
                        'the transcript contains no read attempt for the '
                        'environment-sensitive path')
        self.assertEqual(result['statuses'], ['error'])
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'an environment value reached the provider')

    # -- 2. controls: the detector is neither vacuous nor over-broad -----

    def test_positive_control_read_allow_leaks_the_inside_canary(self):
        # Proves the leak detector fires: with `read: allow` the runtime
        # really does read the in-workdir canary and hand it to the model.
        result = self.allow(TARGET_INSIDE)
        self.assertFalse(result['timed_out'], 'the run did not terminate')
        self.assertTrue(result['attempted_this_target'])
        self.assertEqual(result['statuses'], ['completed'])
        self.assertEqual(result['rejection_kinds'], ['completed'])
        self.assertTrue(result['leaked'],
                        'read: allow did not read the in-workdir canary; '
                        'the leak detector cannot detect a leak')

    def test_negative_control_read_allow_still_blocks_the_outside_canary(self):
        # The out-of-workdir canary is refused even with `read: allow`, so
        # its non-disclosure is caused by external_directory:'deny' and not
        # by a detector that never fires.
        result = self.allow(TARGET_OUTSIDE)
        self.assertTrue(result['attempted_this_target'])
        self.assertEqual(result['statuses'], ['error'])
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'the out-of-workdir canary reached the provider')

    def test_negative_control_read_allow_still_blocks_the_environ_path(self):
        result = self.allow(TARGET_ENVIRON)
        self.assertTrue(result['attempted_this_target'])
        self.assertEqual(result['statuses'], ['error'])
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'an environment value reached the provider')

    # -- 3. the transcript verifier rejects the real transcript ----------

    def test_transcript_verifier_rejects_the_real_tool_use_transcript(self):
        # The genuine runtime output (not a fabricated event) must be
        # rejected by the shipped classifier even though the CLI exits 0.
        result = self.ask(TARGET_INSIDE)
        self.assertEqual(result['exit_code'], 0,
                         'expected the CLI to exit 0 despite the refusal')
        status, reason, _error_class, _code = smoke.evaluate(
            result['transcript'], result['exit_code'], result['timed_out'])
        self.assertEqual(status, 'failed')
        self.assertEqual(reason, 'unexpected_tool_use')

    # -- 4. an open but idle stdin does not change the outcome -----------

    def test_idle_pipe_is_open_and_not_eof(self):
        # Harness self-check, both sides of the claim:
        #   (a) a reader whose pipe write end is OPEN and idle BLOCKS; it
        #       does not observe EOF;
        #   (b) a reader whose pipe write end is CLOSED sees EOF and ends.
        # Without this, an "idle pipe" assertion could silently degenerate
        # into the closed-stdin case and prove nothing.
        reader_script = ('import sys; data = sys.stdin.buffer.read(); '
                         'sys.stdout.write(str(len(data)))')

        def reap(child):
            """Kill, wait for and fully drain/close a helper child."""
            child.kill()
            child.wait()
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        # (a) write end open, nothing written -> the reader must block.
        read_fd, write_fd = os.pipe()
        blocked = False
        child = None
        try:
            child = subprocess.Popen(
                ['python3', '-c', reader_script], stdin=read_fd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.close(read_fd)
            read_fd = None
            try:
                child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                blocked = True
        finally:
            if child is not None:
                reap(child)
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        self.assertTrue(blocked,
                        'a reader did not block on an open, idle pipe')

        # (b) write end closed -> the reader sees EOF and finishes.
        read_fd, write_fd = os.pipe()
        child = None
        out = ''
        try:
            child = subprocess.Popen(
                ['python3', '-c', reader_script], stdin=read_fd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.close(read_fd)
            read_fd = None
            os.close(write_fd)
            write_fd = None
            out, _err = child.communicate(timeout=30)
        finally:
            if child is not None and child.poll() is None:
                reap(child)
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        self.assertEqual(out.strip(), '0',
                         'a reader on a closed pipe did not observe EOF')

    def idle(self):
        return self._cached(('ask', TARGET_INSIDE, 'IDLE_PIPE'),
                            lambda: self._run('ask', TARGET_INSIDE, 'IDLE_PIPE',
                                              timeout_s=IDLE_TIMEOUT_S))

    def test_read_ask_with_open_idle_stdin_executes_no_read_and_leaks_nothing(
            self):
        """The open-idle-stdin control: no approval is possible.

        Measured on 2.1.4 (as on 1.19.3): a headless run whose stdin is open
        and idle blocks before any model interaction, so it neither executes
        the read nor forwards anything: nothing is auto-approved and nothing
        is disclosed. Termination and cleanup are asserted below.
        """
        result = self.idle()
        self.assertEqual(result['request_count'], 0,
                         'the runtime interacted with the provider while '
                         'its stdin was open and idle')
        executed = [event for event in result['events']
                    if event['kind'] == 'completed']
        self.assertEqual(executed, [],
                         'a read was executed although no approver could '
                         'answer the `ask` prompt')
        self.assertFalse(result['leaked'],
                         'the in-workdir canary reached the provider with an '
                         'open idle stdin')

    def test_open_idle_stdin_run_is_terminated_by_the_hard_timeout_and_reaped(
            self):
        """A hard timeout is the control that terminates a blocked run: it
        must fire, the child must be killed and fully reaped."""
        result = self.idle()
        self.assertTrue(result['timed_out'],
                        'the runtime did not block on the open idle stdin; '
                        'the hard-timeout control was not exercised')
        self.assertEqual(result['exit_code'], -signal.SIGKILL)
        self.assertTrue(result['reaped'],
                        'the timed-out child process was not reaped')

    # -- 5. the child process holds only synthetic values -----------------

    def test_child_process_environment_is_isolated_and_synthetic(self):
        result = self.ask(TARGET_INSIDE)
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
                'unexpected credential-bearing variable in child env')

    # -- 6. (v2) only `read` is ever offered; new v2 tools are absent -----

    def test_shipped_config_offers_the_model_exactly_the_read_tool(self):
        result = self.ask(TARGET_INSIDE)
        self.assertTrue(result['offered_tools'])
        for offered in result['offered_tools']:
            self.assertEqual(offered, ['read'])

    def test_control_legacy_permission_map_alone_exposes_v2_tools(self):
        # Why the native `permissions` list exists: with only the v1-style
        # map, v2 still offers its new tools (unmatched actions are
        # auto-approved headlessly). If this ever stops being true, the
        # control is stale, not the smoke.
        result = self._cached(
            ('ask', 'legacy-only'),
            lambda: self._run('ask', TARGET_INSIDE, legacy_only=True))
        offered = set().union(*map(set, result['offered_tools']))
        self.assertIn('execute', offered)
        self.assertFalse(result['leaked'])

    def test_calls_to_unoffered_v2_tools_fail_and_disclose_nothing(self):
        calls = {
            'execute': {'code': 'return 1'},
            'shell': {'command': 'cat canary_inside.txt; env'},
            'subagent': {'agent': 'general', 'description': 'd',
                         'prompt': 'read canary_inside.txt'},
            'glob': {'pattern': '**/*'},
        }
        for name, arguments in calls.items():
            result = self._cached(
                ('ask', 'call', name),
                lambda: self._run('ask', TARGET_INSIDE,
                                  step=_tool_reply(name, arguments)))
            self.assertFalse(result['timed_out'], name)
            self.assertEqual(result['tools_called'], [name])
            self.assertEqual(result['statuses'], ['error'], name)
            self.assertEqual(result['rejection_kinds'], ['unavailable'], name)
            self.assertFalse(result['leaked'], name)
            self.assertEqual(smoke.evaluate(result['transcript'],
                                            result['exit_code'], False)[1],
                             'unexpected_tool_use', name)
        self.assertFalse(set(V2_FORBIDDEN_TOOLS)
                         & set().union(*map(set, result['offered_tools'])))

    # -- 7. (v2) endless provider retries are bounded by the runner -------

    def test_endless_5xx_retry_is_bounded_and_reaped_by_the_runner(self):
        # v2 retries a 5xx forever (only step_start events, no error event).
        # smoke._run_cli's hard timeout must end it, kill the whole process
        # group and leave no CLI process behind.
        before = set(_cli_pids())
        result = self._cached(
            ('ask', 'http500'),
            lambda: self._run('ask', TARGET_INSIDE, http_status=500,
                              timeout_s=RETRY_TIMEOUT_S))
        self.assertTrue(result['timed_out'])
        # A runner-killed run reports no exit status; the timeout is the fact.
        self.assertIsNone(result['exit_code'])
        self.assertLess(result['elapsed'], RETRY_TIMEOUT_S + 15)
        self.assertGreaterEqual(result['request_count'], 2,
                                'the runtime did not retry; the scenario '
                                'did not exercise the endless retry')
        self.assertEqual(smoke.evaluate(result['transcript'],
                                        result['exit_code'], True)[1],
                         'timeout')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and set(_cli_pids()) - before:
            time.sleep(0.2)
        self.assertEqual(set(_cli_pids()) - before, set(),
                         'a CLI process outlived the hard timeout')


def _cli_pids():
    """PIDs of live (non-zombie) processes running the pinned CLI binary."""
    target = os.path.realpath(BIN)
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            if os.path.realpath(f'/proc/{entry}/exe') == target \
                    and _alive(int(entry)):
                pids.append(int(entry))
        except OSError:
            continue
    return pids


if __name__ == '__main__':
    unittest.main()
