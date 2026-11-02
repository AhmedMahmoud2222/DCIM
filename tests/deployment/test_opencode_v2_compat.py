"""OpenCode v2 compatibility: guard lifecycle, provider-rejection fail-fast, header relay, permission rules.

Binary-free. The transcripts in fixtures/opencode_2_1_4 were produced by the genuine 2.1.4 binary against a
loopback scripted provider (see capture.py there); the real-binary behaviour is covered by test_opencode_e2e.
"""
import http.client
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
guard = importlib.import_module('opencode_guard')
proxy = importlib.import_module('opencode_proxy')
sandbox = importlib.import_module('opencode_sandbox')
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'opencode_2_1_4'
MODEL = 'opencode/glm-5.3-flash-free'
SESSION = 'ses_abcdefgh12345678'


def load(name):
    meta = json.loads((FIXTURES / f'{name}.meta.json').read_text())
    return (FIXTURES / f'{name}.jsonl').read_text().splitlines(), meta


def verdict(name):
    lines, meta = load(name)
    return guard.evaluate(lines, meta['exit_code'], meta['timed_out'])


def event(kind, stamp, message, **part):
    base = {'step_start': 'step-start', 'step_finish': 'step-finish', 'text': 'text', 'tool_use': 'tool'}[kind]
    return json.dumps({'type': kind, 'timestamp': stamp, 'sessionID': SESSION,
                       'part': {'type': base, 'sessionID': SESSION, 'messageID': message, **part}})


STEP_A, STEP_B = 'msg_aaaaaaaa1111', 'msg_bbbbbbbb2222'


def tool_step(stamp=1, message=STEP_A, reason='tool-calls'):
    return [event('step_start', stamp, message), event('tool_use', stamp + 1, message, state={'status': 'completed'}),
            event('step_finish', stamp + 2, message, reason=reason)]


class RealV2TranscriptTests(unittest.TestCase):
    def test_fixtures_are_from_opencode_2_1_4(self):
        metas = list(FIXTURES.glob('*.meta.json'))
        self.assertEqual(len(metas), 7)
        for meta_file in metas:
            self.assertEqual(json.loads(meta_file.read_text())['opencode_version'], '2.1.4', meta_file.name)

    def test_a_reply_without_tool_calls_is_bare_text_and_is_accepted(self):
        lines, _ = load('success_text')
        self.assertEqual([json.loads(line)['type'] for line in lines], ['text'])
        self.assertEqual(verdict('success_text'), ('success', ''))

    def test_a_reply_after_tool_calls_is_bare_text_after_the_last_tool_step_and_is_accepted(self):
        lines, _ = load('success_edit')
        kinds = [json.loads(line)['type'] for line in lines]
        reasons = [json.loads(line)['part'].get('reason') for line in lines if json.loads(line)['type'] == 'step_finish']
        self.assertEqual((kinds, reasons), (['step_start', 'tool_use', 'step_finish', 'text'], ['tool-calls']))
        self.assertEqual(verdict('success_edit'), ('success', ''))

    def test_real_failures_are_rejected_with_the_expected_reason(self):
        expected = {
            'fail_unauthorized_401': 'nonzero_exit:1',
            'fail_truncated_length': 'nonzero_exit:1',       # step_finish(length) then an error event
            'fail_rate_limited_429': 'timeout',               # endless internal retry, killed by the timeout
            'fail_provider_503': 'timeout',
            'fail_stream_cut': 'timeout',
        }
        for name, reason in expected.items():
            self.assertEqual(verdict(name), ('failed', reason), name)

    def test_real_failures_are_rejected_even_if_the_exit_status_were_ignored(self):
        for name in ('fail_unauthorized_401', 'fail_truncated_length', 'fail_rate_limited_429', 'fail_provider_503',
                     'fail_stream_cut'):
            status, why = guard.evaluate(load(name)[0], 0, False)
            self.assertEqual(status, 'failed', name)
            self.assertNotEqual(why, '', name)

    def test_the_real_429_transcript_has_no_error_event_so_the_transcript_alone_cannot_fail_fast(self):
        kinds = {json.loads(line)['type'] for line in load('fail_rate_limited_429')[0]}
        self.assertEqual(kinds, {'step_start'})

    def test_only_a_non_empty_closing_reply_counts(self):
        for text in ('', '   '):
            lines = [event('text', 1, STEP_A, text=text)]
            self.assertEqual(guard.evaluate(lines, 0, False), ('failed', 'no_supported_termination'))
        self.assertEqual(guard.evaluate([event('text', 1, STEP_A, text='done')], 0, False), ('success', ''))

    def test_bare_text_is_accepted_only_alone_or_after_a_tool_calls_step_and_only_last(self):
        reply = event('text', 9, STEP_B, text='done')
        self.assertEqual(guard.evaluate(tool_step() + [reply], 0, False), ('success', ''))
        # text before a step, between steps, or after a stop step is not a lifecycle v2 produces
        self.assertEqual(guard.evaluate([reply] + tool_step(10), 0, False), ('failed', 'event_ordering'))
        self.assertEqual(guard.evaluate(tool_step() + [reply] + tool_step(20, STEP_B), 0, False)[0], 'failed')
        stop = [event('step_start', 1, STEP_A), event('text', 2, STEP_A, text='x'),
                event('step_finish', 3, STEP_A, reason='stop')]
        self.assertEqual(guard.evaluate(stop, 0, False), ('success', ''))
        self.assertEqual(guard.evaluate(stop + [event('text', 4, STEP_B, text='more')], 0, False),
                         ('failed', 'event_ordering'))
        # a tool_use outside any step is never bare reply text
        self.assertEqual(guard.evaluate([event('tool_use', 1, STEP_A, state={'status': 'completed'})], 0, False),
                         ('failed', 'event_ordering'))

    def test_a_run_that_ends_after_its_last_tool_step_without_a_reply_is_not_a_success(self):
        self.assertEqual(guard.evaluate(tool_step(), 0, False), ('failed', 'no_supported_termination'))

    def test_bare_text_of_another_session_or_out_of_order_is_rejected(self):
        other = json.loads(event('text', 5, STEP_A, text='x'))
        other['sessionID'] = 'ses_zzzzzzzz99999999'
        self.assertEqual(guard.evaluate([event('text', 1, STEP_A, text='x'), json.dumps(other)], 0, False),
                         ('failed', 'session_mismatch'))
        self.assertEqual(guard.evaluate([event('text', 5, STEP_A, text='x'), event('text', 4, STEP_A, text='y')], 0, False),
                         ('failed', 'timestamp_order'))


class ProxyLogRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, 'proxy.log')

    def append(self, *records, raw=None):
        with open(self.log, 'a', encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps(record) + '\n')
            if raw:
                handle.write(raw)

    def test_a_terminal_provider_rejection_is_returned_with_its_category(self):
        self.append({'event': 'accepted'}, {'event': 'provider_rejection', 'category': 'provider_http_429', 'terminal': True})
        category, offset = guard.read_new_rejection(self.log, 0)
        self.assertEqual(category, 'provider_http_429')
        self.assertEqual(offset, os.path.getsize(self.log))

    def test_non_terminal_records_and_other_events_are_ignored(self):
        self.append({'event': 'provider_rejection', 'category': 'provider_http_429', 'terminal': False},
                    {'event': 'http_response', 'category': 'provider_http_429', 'terminal': True})
        self.assertEqual(guard.read_new_rejection(self.log, 0)[0], '')

    def test_only_a_well_formed_category_token_is_ever_taken_from_a_record(self):
        for category in ('Provider_X', 'provider_', 'x provider_http_429', 'provider_' + 'a' * 60, 'oops', 7, None):
            self.append({'event': 'provider_rejection', 'category': category, 'terminal': True})
        self.assertEqual(guard.read_new_rejection(self.log, 0)[0], '')

    def test_a_half_written_line_is_left_for_the_next_poll(self):
        record = json.dumps({'event': 'provider_rejection', 'category': 'provider_http_429', 'terminal': True})
        self.append(raw=record[:20])
        category, offset = guard.read_new_rejection(self.log, 0)
        self.assertEqual((category, offset), ('', 0))
        self.append(raw=record[20:] + '\n')
        self.assertEqual(guard.read_new_rejection(self.log, offset)[0], 'provider_http_429')

    def test_records_written_before_the_run_started_are_not_attributed_to_it(self):
        self.append({'event': 'provider_rejection', 'category': 'provider_http_429', 'terminal': True})
        child = [sys.executable, '-c', 'pass']
        _out, _err, code, rejection = guard.run_agent(child, self.tmp.name, 30, self.log)
        self.assertEqual((code, rejection), (0, ''))

    def test_a_missing_log_is_not_an_error(self):
        self.assertEqual(guard.read_new_rejection(self.log, 0), ('', 0))


class RunAgentTests(unittest.TestCase):
    SLEEPER = [sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(120)']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, 'proxy.log')
        Path(self.log).write_text('')

    def test_a_terminal_rejection_stops_the_agent_promptly(self):
        def reject_soon():
            time.sleep(1)
            with open(self.log, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'event': 'provider_rejection', 'category': 'provider_http_429', 'terminal': True}) + '\n')
        threading.Thread(target=reject_soon, daemon=True).start()
        stopped = []
        started = time.monotonic()
        _out, _err, code, rejection = guard.run_agent(self.SLEEPER, self.tmp.name, 60, self.log,
                                                      stop=lambda process: (stopped.append(True), process.kill()))
        self.assertEqual(rejection, 'provider_http_429')
        self.assertEqual(stopped, [True])
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 15)

    def test_a_rejection_logged_just_before_the_agent_exits_is_still_reported(self):
        # v2 exits by itself on a 401, possibly between two polls
        script = ('import json, sys, time; time.sleep(0.2); '
                  'open(sys.argv[1], "a").write(json.dumps({"event": "provider_rejection", "category": '
                  '"provider_auth_rejected", "terminal": True}) + chr(10)); sys.exit(1)')
        _out, _err, code, rejection = guard.run_agent([sys.executable, '-c', script, self.log], self.tmp.name, 30, self.log)
        self.assertEqual((code, rejection), (1, 'provider_auth_rejected'))

    def test_the_wall_clock_limit_still_applies_without_a_rejection(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            guard.run_agent(self.SLEEPER, self.tmp.name, 2, self.log, stop=lambda process: process.kill())

    def test_without_a_proxy_log_the_agent_just_runs_to_completion(self):
        out, _err, code, rejection = guard.run_agent([sys.executable, '-c', 'print("hi")'], self.tmp.name, 30, '')
        self.assertEqual((out.strip(), code, rejection), ('hi', 0, ''))

    def test_main_names_the_provider_failure_instead_of_its_symptom_but_never_overturns_a_success(self):
        for rejection, transcript_ok, expected in (('provider_http_429', False, 'provider_http_429'),
                                                    ('provider_http_429', True, '')):
            lines = load('success_text')[0] if transcript_ok else load('fail_rate_limited_429')[0]
            with tempfile.TemporaryDirectory() as tmp:
                config, result = Path(tmp, 'c.json'), Path(tmp, 'r.json')
                config.write_text('{}')
                fake = mock.Mock(return_value=('\n'.join(lines), '', 0 if transcript_ok else 124, rejection))
                with mock.patch.object(guard, 'run_agent', fake), \
                        mock.patch.object(guard, 'reap_agent_processes', return_value=''), \
                        mock.patch.dict(os.environ, {'OPENCODE_PROXY_LOG': self.log}, clear=False):
                    guard.main(['g', str(result), MODEL, str(config), '/h', '/w', '25'])
                self.assertEqual(json.loads(result.read_text())['reason'], expected)
                self.assertEqual(fake.call_args.args[3], self.log)       # the guard hands the proxy log to the runner


class ProviderRejectionClassificationTests(unittest.TestCase):
    def test_categories_come_from_the_http_status_only(self):
        expected = {401: 'provider_auth_rejected', 402: 'provider_quota_exhausted', 403: 'provider_http_403',
                    429: 'provider_http_429', 400: 'provider_http_400', 404: 'provider_http_404',
                    500: 'provider_unavailable', 502: 'provider_unavailable', 503: 'provider_unavailable',
                    None: 'provider_unavailable', 200: ''}
        for status, category in expected.items():
            self.assertEqual(proxy.classify_provider_status(status), category, status)

    def test_a_rate_limit_is_not_guessed_to_be_a_quota_and_a_403_is_not_guessed_to_be_an_unsupported_client(self):
        # The status alone cannot tell these apart, so no finer category is claimed.
        self.assertNotIn(proxy.classify_provider_status(429), ('provider_rate_limited', 'provider_quota_exhausted'))
        self.assertNotEqual(proxy.classify_provider_status(403), 'provider_client_unsupported')

    def test_limits_before_a_rejection_stops_the_run(self):
        self.assertEqual([proxy.rejection_limit(s) for s in (401, 402, 403, 429, 500, 503, None, 418)],
                         [1, 1, 1, 2, 3, 3, 3, 1])

    def test_every_category_is_a_token_the_guard_accepts(self):
        for status in (400, 401, 402, 403, 404, 429, 500, 503, None):
            self.assertRegex(proxy.classify_provider_status(status), guard.REJECTION_CATEGORY_RE.pattern)


class UpstreamStub:
    """Loopback provider that records the headers it receives and answers with a scripted status."""

    def __init__(self, status=200):
        self.status, self.requests = status, []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                owner.requests.append({k.lower(): v for k, v in self.headers.items()})
                body = b'{"error": "secret upstream detail"}' if owner.status >= 400 else \
                    b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'
                self.send_response(owner.status)
                self.send_header('Content-Type', 'application/json' if owner.status >= 400 else 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyRelayTests(unittest.TestCase):
    BODY = json.dumps({'model': 'glm-5.3-flash-free', 'messages': [{'role': 'user', 'content': 'x'}], 'stream': True})

    def start(self, status=200, upstream_url=None):
        upstream = UpstreamStub(status)
        self.addCleanup(upstream.close)
        self.log = tempfile.NamedTemporaryFile(delete=False)
        self.log.close()
        self.addCleanup(os.unlink, self.log.name)
        server, self.px = proxy.serve('REAL-KEY', upstream_url or upstream.url, 'glm-5.3-flash-free', log_path=self.log.name)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return upstream

    def post(self, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('POST', '/inference/openai/v1/chat/completions', self.BODY,
                           {'Content-Type': 'application/json', 'Authorization': 'Bearer sandbox-placeholder', **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def log_records(self):
        return [json.loads(line) for line in Path(self.log.name).read_text().splitlines()]

    GENUINE = {'User-Agent': 'opencode/latest/2.1.4/cli', 'x-opencode-client': 'cli',
               'x-opencode-project': '1fb53acb19d48359fc49ad364366045f6ab716c7', 'x-opencode-session': SESSION,
               'x-opencode-session-id': SESSION, 'x-session-id': SESSION, 'x-session-affinity': SESSION}

    def test_genuine_client_identity_headers_are_relayed_unchanged_and_the_credential_is_replaced(self):
        upstream = self.start()
        self.assertEqual(self.post(self.GENUINE)[0], 200)
        sent = upstream.requests[0]
        for name, value in self.GENUINE.items():
            self.assertEqual(sent[name.lower()], value, name)
        self.assertEqual(sent['authorization'], 'Bearer REAL-KEY')

    def test_malformed_or_non_cli_identity_values_are_dropped_not_forwarded(self):
        upstream = self.start()
        self.post({**self.GENUINE, 'x-opencode-client': 'desktop', 'x-opencode-project': 'not-a-sha',
                   'x-opencode-session': 'ses_short', 'x-session-id': 'a b'})
        sent = upstream.requests[0]
        for name in ('x-opencode-client', 'x-opencode-project', 'x-opencode-session', 'x-session-id'):
            self.assertNotIn(name, sent, name)
        self.assertEqual(sent['x-session-affinity'], SESSION)     # the well-formed ones still pass

    def test_no_identity_header_is_ever_invented(self):
        upstream = self.start()
        self.post()
        sent = upstream.requests[0]
        for name in proxy.CLIENT_HEADER_SHAPES:
            self.assertNotIn(name, sent, name)

    def test_upstream_status_and_body_reach_the_client_unchanged(self):
        self.start(status=429)
        status, data = self.post(self.GENUINE)
        self.assertEqual(status, 429)
        self.assertIn(b'secret upstream detail', data)

    def test_a_429_is_terminal_on_the_second_response_and_the_log_holds_no_response_content(self):
        self.start(status=429)
        self.post(self.GENUINE)
        self.assertFalse([r for r in self.log_records() if r.get('terminal')])
        self.post(self.GENUINE)
        terminal = [r for r in self.log_records() if r.get('event') == 'provider_rejection' and r['terminal']]
        self.assertEqual([(r['category'], r['http_status'], r['count']) for r in terminal], [('provider_http_429', 429, 2)])
        text = Path(self.log.name).read_text()
        for secret in ('secret upstream detail', 'REAL-KEY', 'sandbox-placeholder'):
            self.assertNotIn(secret, text)

    def test_a_401_is_terminal_at_once_and_the_policy_rejection_counter_is_not_inflated(self):
        self.start(status=401)
        self.post(self.GENUINE)
        record = [r for r in self.log_records() if r.get('event') == 'provider_rejection'][0]
        self.assertEqual((record['category'], record['terminal']), ('provider_auth_rejected', True))
        self.assertEqual(self.px.rejections, [])                   # a provider refusal is not a proxy policy rejection

    def test_successful_responses_log_no_rejection(self):
        self.start(status=200)
        self.post(self.GENUINE)
        self.assertFalse([r for r in self.log_records() if r.get('event') == 'provider_rejection'])

    def test_an_unreachable_upstream_is_counted_as_unavailable(self):
        self.start(upstream_url='http://127.0.0.1:9')          # nothing listens there
        for _ in range(3):
            self.assertEqual(self.post()[0], 502)
        categories = [(r['category'], r['terminal']) for r in self.log_records() if r.get('event') == 'provider_rejection']
        self.assertEqual(categories, [('provider_unavailable', False), ('provider_unavailable', False),
                                      ('provider_unavailable', True)])


class V2PermissionRuleTests(unittest.TestCase):
    def rules(self, mode):
        return sandbox.agent_config(MODEL, 4000, mode)['permissions']

    def decide(self, mode, action, resource='x.py'):
        """Last matching rule wins (v2 semantics); '*' matches any run of characters including '/'."""
        import fnmatch
        effect = None
        for rule in self.rules(mode):
            if rule['action'] in ('*', action) and fnmatch.fnmatchcase(resource, rule['resource']):
                effect = rule['effect']
        return effect

    def test_the_list_opens_with_a_catch_all_deny_so_unknown_future_actions_are_denied(self):
        for mode in ('implement', 'review', 'probe'):
            self.assertEqual(self.rules(mode)[0], {'action': '*', 'resource': '*', 'effect': 'deny'})
            self.assertEqual(self.decide(mode, 'some_future_action'), 'deny')

    def test_actions_that_run_code_delegate_or_reach_out_are_denied_in_every_mode(self):
        for mode in ('implement', 'review', 'probe'):
            for action in ('execute', 'subagent', 'skill', 'question', 'webfetch', 'websearch', 'external_directory'):
                self.assertEqual(self.decide(mode, action, '/etc/passwd'), 'deny', (mode, action))

    def test_implement_mode_allows_what_it_needs_and_keeps_env_files_out(self):
        for action in ('read', 'glob', 'grep', 'shell', 'edit'):
            self.assertEqual(self.decide('implement', action), 'allow', action)
        self.assertEqual(self.decide('implement', 'read', '.env'), 'deny')
        self.assertEqual(self.decide('implement', 'read', 'config/.env.production'), 'deny')
        self.assertEqual(self.decide('implement', 'read', '.env.example'), 'allow')

    def test_review_mode_cannot_edit_and_probe_mode_allows_nothing(self):
        self.assertEqual(self.decide('review', 'edit'), 'deny')
        self.assertEqual(self.decide('review', 'read'), 'allow')
        for rule in self.rules('probe'):
            self.assertEqual(rule['effect'], 'deny', rule)

    def test_the_legacy_keys_agree_with_the_native_rules_for_what_both_express(self):
        for mode in ('implement', 'review', 'probe'):
            legacy = sandbox.agent_config(MODEL, 4000, mode)['permission']
            for old, new in (('edit', 'edit'), ('bash', 'shell'), ('glob', 'glob'), ('grep', 'grep')):
                self.assertEqual(legacy[old] == 'allow', self.decide(mode, new) == 'allow', (mode, old))
            for old in ('task', 'webfetch', 'websearch', 'external_directory'):
                self.assertEqual(legacy[old], 'deny')

    def test_updates_and_sharing_are_disabled(self):
        config = sandbox.agent_config(MODEL, 4000)
        self.assertEqual((config['update'], config['share'], config['autoupdate']), ('disable', 'disabled', False))


if __name__ == '__main__':
    unittest.main()
