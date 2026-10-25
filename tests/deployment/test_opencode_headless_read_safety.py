#!/usr/bin/env python3
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
     the CLI exits 0;
  7. with an *open but idle* stdin the runtime neither auto-approves a read
     nor contacts the provider (it blocks before any model interaction); the
     diagnostic's hard timeout terminates the whole process group and reaps
     every child, so a hung approval can never be satisfied or leak.

This is P1 pre-dispatch gate evidence for the native smoke diagnostic: it
drives the genuine runtime permission engine rather than feeding fabricated
events to the classifier.

Each sensitive path is exercised in its OWN CLI run. Under `read: 'ask'` the
runtime refuses the first read and stops, so a single run that queues several
targets would never attempt the later ones; every assertion below therefore
first proves that the transcript contains a read attempt for the *specific*
target under test, and only then asserts non-disclosure.

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
import signal
import subprocess
import tempfile
import threading
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

# The three sensitive paths under test. Each is exercised in an independent
# CLI run; see the module docstring for why one run cannot cover all three.
TARGET_INSIDE = 'inside'
TARGET_OUTSIDE = 'outside'
TARGET_ENVIRON = 'environ'
TARGETS = (TARGET_INSIDE, TARGET_OUTSIDE, TARGET_ENVIRON)


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
    """A model turn that asks the runtime to read exactly `path`."""
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

    steps: ClassVar[list] = []
    requests: ClassVar[list] = []

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


def _tool_use_events(transcript):
    """Parse the REAL transcript into tool-use descriptors.

    Each descriptor carries the path the runtime was actually asked to read
    (`input.filePath`), the terminal status, and a coarse rejection kind so
    that the two refusal mechanisms can be told apart:

      'permission' - the `ask` prompt was refused (no approver present)
      'rule'       - a configured rule forbids the call (external_directory)
      'completed'  - the tool ran
      'other'      - anything else
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
        requested = (state.get('input') or {}).get('filePath')
        error = state.get('error')
        if status == 'completed':
            kind = 'completed'
        elif isinstance(error, str) and 'rejected permission' in error:
            kind = 'permission'
        elif isinstance(error, str) and 'rule which prevents' in error:
            kind = 'rule'
        else:
            kind = 'other'
        events.append({'tool': part.get('tool'), 'path': requested,
                       'status': status, 'kind': kind})
    return events


@unittest.skipUnless(BIN and os.path.exists(BIN),
                     'real OpenCode 1.19.3 binary not installed '
                     '(set OPENCODE_BIN)')
class RealHeadlessReadSafetyTests(unittest.TestCase):
    """The real permission engine, headless, with synthetic credentials."""

    @classmethod
    def setUpClass(cls):
        cls.version = subprocess.run(
            [BIN, '--version'], check=False, capture_output=True, text=True,
            timeout=60).stdout.strip()
        # One CLI run per (read permission, target, stdin mode); memoised so
        # the assertions below share a single execution per configuration.
        cls._cache: dict = {}

    # -- harness ----------------------------------------------------------

    @classmethod
    def _cached(cls, key, factory):
        if key not in cls._cache:
            cls._cache[key] = factory()
        return cls._cache[key]

    def _run(self, read_permission, target, stdin_mode='DEVNULL'):
        """Run the real CLI headless against exactly one read target.

        The model is scripted to request ONE path. Under `read: 'ask'` the
        runtime refuses that read and stops, so the transcript is guaranteed
        to contain a read attempt for this specific target -- which is what
        makes the non-disclosure assertions below non-vacuous.
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
            sensitive_marker = {
                TARGET_INSIDE: CANARY_INSIDE,
                TARGET_OUTSIDE: CANARY_OUTSIDE,
                TARGET_ENVIRON: ENV_MARKER,
            }[target]

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

            _Stub.steps = [_read_reply(requested_path), _text_reply('OK')]
            _Stub.requests = []

            command = [BIN, 'run', '--pure', '--format', 'json',
                       '--model', MODEL, '--dir', work, '--', PROMPT]
            outcome = self._spawn(command, env, work, stdin_mode)
            server.shutdown()
            server.server_close()

            transcript = [line for line in outcome['stdout'].splitlines()
                          if line.strip()]
            recorded = json.dumps(_Stub.requests)
            events = _tool_use_events(transcript)
            attempted = [event['path'] for event in events]
            expected_real = os.path.realpath(requested_path)
            return {
                'exit_code': outcome['exit_code'],
                'timed_out': outcome['timed_out'],
                'reaped': outcome.get('reaped', False),
                'transcript': transcript,
                'events': events,
                'attempted_paths': attempted,
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
                'leaked': sensitive_marker in recorded,
                'request_count': len(_Stub.requests),
                'child_env_keys': sorted(env),
                'requested_path': requested_path,
            }

    def _spawn(self, command, env, cwd, stdin_mode):
        """Start the CLI with a controlled stdin and reap it on every path.

        `DEVNULL`  - stdin is /dev/null (EOF immediately).
        `IDLE_PIPE`- stdin is the read end of a pipe whose write end is held
                     OPEN for the whole child execution and never written to,
                     so the child observes an open, idle stream rather than
                     EOF. On 1.19.3 a headless run in this state blocks before
                     any provider interaction; the hard timeout is what
                     guarantees termination, exactly the diagnostic's own
                     guard against an uninterruptible permission wait.

        The child runs in its own session so a timeout can kill the whole
        process group, and every file descriptor is closed on every path.
        """
        read_fd = write_fd = None
        process = None
        timed_out = False
        try:
            if stdin_mode == 'IDLE_PIPE':
                read_fd, write_fd = os.pipe()
                stdin = read_fd
            else:
                stdin = subprocess.DEVNULL
            process = subprocess.Popen(
                command, stdin=stdin, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=cwd,
                text=True, start_new_session=True)
            if read_fd is not None:
                # The child holds its own duplicate; the parent does not
                # need the read end. The WRITE end stays open on purpose.
                os.close(read_fd)
                read_fd = None
            try:
                stdout, _stderr = process.communicate(timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
                stdout, _stderr = process.communicate()
            exit_code = process.returncode
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
                process.wait()
            reaped = process is not None and process.poll() is not None
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                # Closed only here, after the child has exited: the write end
                # was open for the child's entire lifetime.
                os.close(write_fd)
        return {'stdout': stdout or '', 'exit_code': exit_code,
                'timed_out': timed_out, 'reaped': reaped}

    # -- environment fidelity --------------------------------------------

    def test_binary_is_the_pinned_version(self):
        self.assertEqual(self.version, '1.19.3')

    def test_permission_block_under_test_is_the_diagnostic_configuration(self):
        # The probe must exercise exactly what the diagnostic ships.
        self.assertEqual(smoke.PERMISSIONS['read'], 'ask')
        self.assertEqual(smoke.PERMISSIONS['external_directory'], 'deny')
        for tool in ('edit', 'bash', 'webfetch', 'task', 'todowrite'):
            self.assertEqual(smoke.PERMISSIONS[tool], 'deny', tool)

    # -- 1. each sensitive path is attempted, then refused ----------------

    def test_inside_canary_read_is_attempted_and_refused_under_ask(self):
        result = self._cached(('ask', TARGET_INSIDE),
                              lambda: self._run('ask', TARGET_INSIDE))
        self.assertFalse(result['timed_out'], 'the run did not terminate')
        # The attempt is really there: the runtime was asked for this path.
        self.assertTrue(result['attempted_this_target'],
                        'the transcript contains no read attempt for the '
                        'in-workdir canary')
        self.assertEqual(result['statuses'], ['error'])
        # Refused by the `ask` prompt having no approver.
        self.assertEqual(result['rejection_kinds'], ['permission'])
        self.assertFalse(result['leaked'],
                         'the in-workdir canary reached the provider')

    def test_outside_canary_read_is_attempted_and_refused_under_ask(self):
        result = self._cached(('ask', TARGET_OUTSIDE),
                              lambda: self._run('ask', TARGET_OUTSIDE))
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
        result = self._cached(('ask', TARGET_ENVIRON),
                              lambda: self._run('ask', TARGET_ENVIRON))
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
        result = self._cached(('allow', TARGET_INSIDE),
                              lambda: self._run('allow', TARGET_INSIDE))
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
        result = self._cached(('allow', TARGET_OUTSIDE),
                              lambda: self._run('allow', TARGET_OUTSIDE))
        self.assertTrue(result['attempted_this_target'])
        self.assertEqual(result['statuses'], ['error'])
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'the out-of-workdir canary reached the provider')

    def test_negative_control_read_allow_still_blocks_the_environ_path(self):
        result = self._cached(('allow', TARGET_ENVIRON),
                              lambda: self._run('allow', TARGET_ENVIRON))
        self.assertTrue(result['attempted_this_target'])
        self.assertEqual(result['statuses'], ['error'])
        self.assertEqual(result['rejection_kinds'], ['rule'])
        self.assertFalse(result['leaked'],
                         'an environment value reached the provider')

    # -- 3. the transcript verifier rejects the real transcript ----------

    def test_transcript_verifier_rejects_the_real_tool_use_transcript(self):
        # The genuine runtime output (not a fabricated event) must be
        # rejected by the shipped classifier even though the CLI exits 0.
        result = self._cached(('ask', TARGET_INSIDE),
                              lambda: self._run('ask', TARGET_INSIDE))
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

    def test_read_ask_with_open_idle_stdin_executes_no_read_and_leaks_nothing(
            self):
        """The open-idle-stdin control: no approval is possible.

        On 1.19.3 a headless run whose stdin is open and idle blocks before
        any model interaction, so it neither executes the read nor forwards
        its arguments anywhere: nothing is auto-approved and nothing is
        disclosed. Termination and cleanup are asserted by the companion
        test below.
        """
        result = self._cached(('ask', TARGET_INSIDE, 'IDLE_PIPE'),
                              lambda: self._run('ask', TARGET_INSIDE,
                                                'IDLE_PIPE'))
        # The runtime never reached the provider: zero model requests.
        self.assertEqual(result['request_count'], 0,
                         'the runtime interacted with the provider while '
                         'its stdin was open and idle')
        # Nothing was executed on the runtime side: no read, no approval.
        executed = [event for event in result['events']
                    if event['kind'] == 'completed']
        self.assertEqual(executed, [],
                         'a read was executed although no approver could '
                         'answer the `ask` prompt')
        # Nothing sensitive could have been disclosed.
        self.assertFalse(result['leaked'],
                         'the in-workdir canary reached the provider with an '
                         'open idle stdin')

    def test_open_idle_stdin_run_is_terminated_by_the_hard_timeout_and_reaped(
            self):
        """The diagnostic's hard timeout is the control that terminates a
        blocked run: it must fire, the process group must be SIGKILLed and
        the child fully reaped on this path (fd and process cleanup)."""
        result = self._cached(('ask', TARGET_INSIDE, 'IDLE_PIPE'),
                              lambda: self._run('ask', TARGET_INSIDE,
                                                'IDLE_PIPE'))
        # The runtime blocked for the whole window: the control fired.
        self.assertTrue(result['timed_out'],
                        'the runtime did not block on the open idle stdin; '
                        'the hard-timeout control was not exercised')
        # ...and it was force-terminated by SIGKILL to the process group.
        self.assertEqual(result['exit_code'], -signal.SIGKILL)
        # ...and fully reaped: no child outlives the test.
        self.assertTrue(result['reaped'],
                        'the timed-out child process was not reaped')

    # -- 5. the child process holds only synthetic values -----------------

    def test_child_process_environment_is_isolated_and_synthetic(self):
        result = self._cached(('ask', TARGET_INSIDE),
                              lambda: self._run('ask', TARGET_INSIDE))
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


if __name__ == '__main__':
    unittest.main()
