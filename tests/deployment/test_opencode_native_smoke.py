"""OpenCode native CLI smoke tests (smoke tests v2.0, OpenCode v2 runtime).

Validates the native smoke diagnostic end to end against transcripts captured
from the real @opencode/cli 2.1.4 linux-x64 binary (V2_TRANSCRIPTS below), the
workflow's permission model, and secret isolation:

  - fixed model and fixed synthetic prompt (no parameter can change either);
  - exact child environment allowlist (nothing inherited, key isolated, $PWD
    pinned to the workdir because v2 has no --dir);
  - fail-closed permissions in both v2 config dialects (legacy `permission`
    map and native `permissions` rule list with a leading catch-all deny);
  - standard provider integration only: OPENCODE_API_KEY is the only
    credential channel and never appears outside the protected step;
  - sanitized artifact: closed field list; prompt, key, URLs, headers,
    response bodies and stderr text can never enter the record;
  - failure reporting: provider HTTP status classes, tool-use rejection,
    timeouts, non-stop terminations and nonzero exits all fail the run; v2's
    bare-text reply (no step events) is the success shape;
  - bounding: the hard timeout kills and reaps the CLI's whole process group
    (v2 retries 429/5xx forever, so the timeout is the only bound);
  - workflow YAML: permissions, fixed-scope dispatch, fail-closed main guard,
    preserved artifact, v2 binary path and version agreement with the script.

The v1 (1.19.3) fixtures under tests/deployment/fixtures/opencode_1_19_3 are no
longer used here: v2 prints a different success shape (bare `text` events) and
a different error shape (`error.status` instead of `error.data.statusCode`).

main() runs end to end against a scripted stub binary (no network, no real
key); the stubs are POSIX-only, matching the production Ubuntu runner. The
real-binary checks live in test_opencode_headless_read_safety.py.
"""
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'opencode_native_smoke',
    ROOT / '.github/scripts/opencode_native_smoke.py',
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

WORKFLOW_PATH = ROOT / '.github/workflows/opencode-native-smoke.yml'
BINARY_PATH = '.github/opencode/runtime/node_modules/@opencode/cli-linux-x64/bin/opencode'

MODEL = 'opencode/glm-5.3-flash-free'
PROMPT = 'Respond with exactly OK'
PINNED_VERSION = '2.1.4'

# Captured from the real @opencode/cli 2.1.4 linux-x64 binary running the
# SHIPPED smoke config (smoke.smoke_config(), smoke.build_command(),
# smoke.build_child_env(), smoke._run_cli()) against a loopback scripted
# OpenAI-compatible stub with a synthetic key; only the provider baseURL was
# redirected. Lines are verbatim. Per case: (stdout lines, exit code,
# timed_out). Notable v2 facts these encode:
#   - success without tool calls is ONLY bare `text` events, exit 0;
#   - a read under `ask` is rejected headlessly (tool_use status=error) and
#     the CLI still exits 0; under `allow` it completes;
#   - 401 and 403 both arrive as error.type `provider.auth`; only
#     error.status tells them apart; exit 1;
#   - a `length` truncation is retried twice, then an `output-limit` error
#     without a status, exit 1;
#   - HTTP 500 is retried forever (only step_start events) until killed.
V2_TRANSCRIPTS = {
    'success_text': {
        'exit_code': 0, 'timed_out': False,
        'lines': [
            '{"type":"text","timestamp":1793161924303,"sessionID":"ses_e7f387659ffeEzMAMdbpo4XGP1","part":{"id":"prt_180c78ead001lOuICTcpkbjfue_text-0","sessionID":"ses_e7f387659ffeEzMAMdbpo4XGP1","messageID":"msg_180c78ead001lOuICTcpkbjfue","type":"text","text":"OK","time":{"start":1793161924279,"end":1793161924303}}}',
        ],
    },
    'tool_read_ask_rejected': {
        'exit_code': 0, 'timed_out': False,
        'lines': [
            '{"type":"step_start","timestamp":1793161926233,"sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","part":{"id":"prt_180c796590014LXWmtX1LV4QQu","sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","messageID":"msg_180c796410012dHCHq572Btmpl","type":"step-start"}}',
            '{"type":"tool_use","timestamp":1793161926256,"sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","part":{"partID":"prt_180c7965c001XrKasBZK6Ss6VS","sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","messageID":"msg_180c796410012dHCHq572Btmpl","type":"tool","id":"call_1","tool":"read","state":{"status":"error","input":{"path":"notes.txt"},"error":"This non-interactive run cannot ask the user for permission, so the request was rejected. Continue without this action.","metadata":{"providerCall":{"executed":false},"providerResult":{"executed":false},"rawInput":"{\\"path\\": \\"notes.txt\\"}"},"time":{"start":1793161926236,"end":1793161926256}}}}',
            '{"type":"step_finish","timestamp":1793161926259,"sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","part":{"id":"prt_180c79672001xd1bL8QcVP2hei","sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","messageID":"msg_180c796410012dHCHq572Btmpl","type":"step-finish","reason":"tool-calls","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}}',
            '{"type":"text","timestamp":1793161926305,"sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","part":{"id":"prt_180c7967b001ckYNGoBSIxa271_text-0","sessionID":"ses_e7f386dc3ffeldGvs5IUU4MWpU","messageID":"msg_180c7967b001ckYNGoBSIxa271","type":"text","text":"OK","time":{"start":1793161926290,"end":1793161926305}}}',
        ],
    },
    'tool_read_allow_completed': {
        'exit_code': 0, 'timed_out': False,
        'lines': [
            '{"type":"step_start","timestamp":1793161928599,"sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","part":{"id":"prt_180c79f970012MRjJD9iIXLXSs","sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","messageID":"msg_180c79f470010GBUpO1iL92nPr","type":"step-start"}}',
            '{"type":"tool_use","timestamp":1793161928661,"sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","part":{"partID":"prt_180c79f9f001k96Oj7cbmrYB6o","sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","messageID":"msg_180c79f470010GBUpO1iL92nPr","type":"tool","id":"call_1","tool":"read","state":{"status":"completed","input":{"path":"notes.txt"},"output":"Read file notes.txt, lines 1-1\\n1: synthetic-notes","title":"read","metadata":{"metadata":{"truncated":false},"content":[{"type":"text","text":"Read file notes.txt, lines 1-1\\n1: synthetic-notes"}],"providerCall":{"executed":false},"providerResult":{"executed":false},"rawInput":"{\\"path\\": \\"notes.txt\\"}"},"time":{"start":1793161928607,"end":1793161928661}}}}',
            '{"type":"step_finish","timestamp":1793161928666,"sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","part":{"id":"prt_180c79fda001WKLyUSbyXsK7Iz","sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","messageID":"msg_180c79f470010GBUpO1iL92nPr","type":"step-finish","reason":"tool-calls","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}}',
            '{"type":"text","timestamp":1793161928756,"sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","part":{"id":"prt_180c79ff30013d5pOoXjN7uMTz_text-0","sessionID":"ses_e7f386560ffeAduMPaxwK17u4G","messageID":"msg_180c79ff30013d5pOoXjN7uMTz","type":"text","text":"OK","time":{"start":1793161928731,"end":1793161928756}}}',
        ],
    },
    'auth_401': {
        'exit_code': 1, 'timed_out': False,
        'lines': [
            '{"type":"error","timestamp":1793161930731,"sessionID":"ses_e7f385c43ffe1RV8a6trak7UXF","error":{"type":"provider.auth","message":"Reconnect OpenCode Console to continue.","status":401,"response":{"body":"{\\"error\\":{\\"type\\":\\"authentication_error\\",\\"message\\":\\"Reconnect OpenCode Console to continue.\\"}}"}}}',
        ],
    },
    'forbidden_403': {
        'exit_code': 1, 'timed_out': False,
        'lines': [
            '{"type":"error","timestamp":1793161932833,"sessionID":"ses_e7f3854caffe30ahWKdx5xFpV5","error":{"type":"provider.auth","message":"Reconnect OpenCode Console to continue.","status":403,"response":{"body":"{\\"error\\":{\\"type\\":\\"authentication_error\\",\\"message\\":\\"Reconnect OpenCode Console to continue.\\"}}"}}}',
        ],
    },
    'truncated_length': {
        'exit_code': 1, 'timed_out': False,
        'lines': [
            '{"type":"step_start","timestamp":1793161935050,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b8ca001wIBVCM305UchBk","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8b3001n5zKYSfQJm7fQM","type":"step-start"}}',
            '{"type":"text","timestamp":1793161935056,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b8cc001OXlRNJ64ayTN9g","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8b3001n5zKYSfQJm7fQM","type":"text","text":"O","time":{"start":1793161935052,"end":1793161935056}}}',
            '{"type":"step_finish","timestamp":1793161935060,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b8d4001CioM3i0bnVKawg","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8b3001n5zKYSfQJm7fQM","type":"step-finish","reason":"length","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}}',
            '{"type":"step_start","timestamp":1793161935097,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b8f9001pVshOVKa203Y9g","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8d7001gTyQs7YyZ5LHGt","type":"step-start"}}',
            '{"type":"text","timestamp":1793161935102,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b8fb001HzDZjMc7dUf3fv","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8d7001gTyQs7YyZ5LHGt","type":"text","text":"O","time":{"start":1793161935099,"end":1793161935102}}}',
            '{"type":"step_finish","timestamp":1793161935105,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b901001cdk4bnEBRZVi0o","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b8d7001gTyQs7YyZ5LHGt","type":"step-finish","reason":"length","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}}',
            '{"type":"error","timestamp":1793161935134,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","error":{"type":"output-limit","message":"Response still truncated after two output token limit continuations"}}',
            '{"type":"text","timestamp":1793161935132,"sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","part":{"id":"prt_180c7b905001zlmB2hX0rc7CAc_text-0","sessionID":"ses_e7f384c9bffe8N1czpAjdK3Wy4","messageID":"msg_180c7b905001zlmB2hX0rc7CAc","type":"text","text":"O","time":{"start":1793161935121,"end":1793161935132}}}',
        ],
    },
    'retry_500_until_killed': {
        'exit_code': -9, 'timed_out': True,
        'lines': [
            '{"type":"step_start","timestamp":1793161937352,"sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","part":{"id":"prt_180c7c1c8001Whi5cd9XSxv2Qx","sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","messageID":"msg_180c7c1aa001ktS5qtKqCriT24","type":"step-start"}}',
            '{"type":"step_start","timestamp":1793161939648,"sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","part":{"id":"prt_180c7cac0001m6KsilplEaJQFH","sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","messageID":"msg_180c7c1aa001ktS5qtKqCriT24","type":"step-start"}}',
            '{"type":"step_start","timestamp":1793161943580,"sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","part":{"id":"prt_180c7da1c001iHy7v49d7QE04d","sessionID":"ses_e7f3843a7ffeeU0osMJRB1eOru","messageID":"msg_180c7c1aa001ktS5qtKqCriT24","type":"step-start"}}',
        ],
    },
}


def transcript(name: str) -> tuple[list[str], int, bool]:
    case = V2_TRANSCRIPTS[name]
    return list(case['lines']), case['exit_code'], case['timed_out']


def assert_outcome(case, lines, exit_code, timed_out, status, reason,
                   error_class=None, error_status_code=None):
    """Compare a complete evaluate() outcome tuple."""
    case.assertEqual(
        smoke.evaluate(lines, exit_code, timed_out),
        (status, reason, error_class, error_status_code),
        (lines[:2], exit_code, timed_out),
    )


def text_event(text='OK', session='ses_e7f3df85fffeJWRHFxspCNW2ha', stamp=1):
    return json.dumps({'type': 'text', 'timestamp': stamp, 'sessionID': session,
                       'part': {'sessionID': session, 'type': 'text',
                                'messageID': 'msg_180c20c9a001VWkxcAcOKycUjL',
                                'text': text}})


# ---------------------------------------------------------------------------
# Fixed scope: one model, one prompt, nothing parameterizable
# ---------------------------------------------------------------------------

class FixedScopeTests(unittest.TestCase):
    def test_model_is_the_one_allowed_selector(self):
        self.assertEqual(smoke.MODEL, MODEL)
        self.assertRegex(smoke.MODEL, r'^opencode/[a-z0-9.-]+-free$')

    def test_prompt_is_the_exact_mandated_string(self):
        self.assertEqual(smoke.PROMPT, PROMPT)

    def test_no_parameter_can_change_model_or_prompt(self):
        sig = inspect.signature(smoke.build_command)
        self.assertEqual(list(sig.parameters), ['binary'])
        # evaluate() and main() take fixed tuples only; no model/prompt flags.
        self.assertEqual(list(inspect.signature(smoke.evaluate).parameters),
                         ['lines', 'exit_code', 'timed_out'])

    def test_command_is_the_v2_headless_invocation_without_credential(self):
        command = smoke.build_command('/opt/opencode/bin/opencode')
        self.assertEqual(command, [
            '/opt/opencode/bin/opencode', 'run', '--standalone', '--format',
            'json', '--model', MODEL, '--', PROMPT,
        ])
        # Flags removed in v2 must not come back: --pure and --dir are gone
        # (the workdir is the process cwd plus $PWD).
        self.assertNotIn('--pure', command)
        self.assertNotIn('--dir', command)
        self.assertNotIn('OPENCODE_API_KEY', command)

    def test_workflow_yaml_does_not_carry_the_prompt(self):
        # The prompt lives only in the script; dispatchers cannot change it.
        self.assertNotIn('Respond with exactly', WORKFLOW_PATH.read_text())


# ---------------------------------------------------------------------------
# Provider error classification (shared with the direct diagnostic)
# ---------------------------------------------------------------------------

class ProviderErrorClassificationTests(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(smoke.classify_provider_error(401), 'auth_error')
        self.assertEqual(smoke.classify_provider_error(403), 'forbidden')
        self.assertEqual(smoke.classify_provider_error(429), 'rate_limit')
        self.assertEqual(smoke.classify_provider_error(404), 'model_not_available')
        for code in (500, 502, 503):
            self.assertEqual(smoke.classify_provider_error(code),
                             'upstream_error')

    def test_other_statuses_are_classified_by_number(self):
        self.assertEqual(smoke.classify_provider_error(418), 'http_418')

    def test_missing_status_is_unknown_provider_error(self):
        self.assertEqual(smoke.classify_provider_error(None),
                         'unknown_provider_error')
        # Booleans are ints in Python; they are not valid status codes.
        self.assertEqual(smoke.classify_provider_error(True),
                         'unknown_provider_error')


# ---------------------------------------------------------------------------
# Child environment: closed allowlist, key isolated, tools denied
# ---------------------------------------------------------------------------

class ChildEnvironmentTests(unittest.TestCase):
    def env(self, key='k'):
        return smoke.build_child_env('/tmp/agent-home', '/tmp/smoke-work', key)

    def config(self):
        return json.loads(self.env()['OPENCODE_CONFIG_CONTENT'])

    def test_env_is_an_exact_closed_allowlist(self):
        self.assertEqual(set(self.env('secret-key')), smoke.CHILD_ENV_KEYS)

    def test_key_present_only_in_its_designated_slot(self):
        env = self.env('secret-key-value')
        self.assertEqual(env['OPENCODE_API_KEY'], 'secret-key-value')
        self.assertEqual([k for k, v in env.items() if 'secret-key-value' in v],
                         ['OPENCODE_API_KEY'])

    def test_version_probe_env_omits_the_key_entirely(self):
        self.assertNotIn('OPENCODE_API_KEY', self.env(''))

    def test_pwd_is_pinned_to_the_workdir(self):
        # v2 has no --dir and resolves relative paths from $PWD when it is
        # set: a stale inherited PWD would point the run at another tree.
        self.assertEqual(self.env()['PWD'], '/tmp/smoke-work')
        with self.assertRaises(ValueError):
            smoke.build_child_env('/tmp/agent-home', '', 'k')

    def test_v1_pure_mode_is_replaced_by_the_v2_discovery_switches(self):
        env = self.env()
        self.assertNotIn('OPENCODE_PURE', env)
        for flag in ('OPENCODE_DISABLE_PROJECT_CONFIG',
                     'OPENCODE_CONFIG_PROJECT_DISABLE',
                     'OPENCODE_DISABLE_CLAUDE_CODE',
                     'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS',
                     'OPENCODE_DISABLE_MODELS_FETCH',
                     'OPENCODE_DISABLE_AUTOUPDATE'):
            self.assertEqual(env[flag], '1', flag)

    def test_home_and_xdg_paths_live_under_agent_home_only(self):
        for key, value in self.env().items():
            if key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                       'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
                self.assertTrue(value.startswith('/tmp/agent-home'), key)

    def test_legacy_permission_map_denies_everything_but_read_ask(self):
        config = self.config()
        self.assertEqual(config['enabled_providers'], ['opencode'])
        self.assertFalse(config['autoupdate'])
        self.assertEqual(config['share'], 'disabled')
        legacy = config['permission']
        self.assertEqual(legacy.pop('read'), 'ask')
        for tool in ('edit', 'bash', 'glob', 'grep', 'list', 'webfetch',
                     'websearch', 'external_directory', 'task'):
            self.assertEqual(legacy[tool], 'deny', tool)
        self.assertEqual(set(legacy.values()), {'deny'})

    def test_native_rule_list_is_fail_closed(self):
        # v2 auto-approves any action that no rule matches in a headless run,
        # and its new actions (execute, subagent, skill, question) are not
        # covered by the legacy keys: the native list must open with a
        # catch-all deny and may re-open nothing but `read` as `ask`.
        rules = self.config()['permissions']
        for rule in rules:
            self.assertEqual(set(rule), {'action', 'resource', 'effect'})
            self.assertIn(rule['effect'], ('deny', 'ask'))
        self.assertEqual(rules[0], {'action': '*', 'resource': '*',
                                    'effect': 'deny'})
        non_deny = [r for r in rules if r['effect'] != 'deny']
        self.assertEqual(non_deny, [{'action': 'read', 'resource': '*',
                                     'effect': 'ask'}])
        # Every other known v2 action is re-denied AFTER the read rule (last
        # matching rule wins), and external_directory is the final word.
        read_at = rules.index(non_deny[0])
        later = {r['action'] for r in rules[read_at + 1:]
                 if r['effect'] == 'deny' and r['resource'] == '*'}
        for action in ('edit', 'shell', 'glob', 'grep', 'execute', 'subagent',
                       'skill', 'question', 'webfetch', 'websearch',
                       'external_directory'):
            self.assertIn(action, later, action)
        self.assertEqual(rules[-1], {'action': 'external_directory',
                                     'resource': '*', 'effect': 'deny'})
        self.assertNotIn('allow', json.dumps(self.config()))

    def test_no_other_model_is_selectable_for_any_request(self):
        # The primary model, the CLI --model flag and the auxiliary/
        # title-generation model must all be the single pinned MODEL.
        config = self.config()
        self.assertEqual(config['model'], MODEL)
        self.assertEqual(config['small_model'], MODEL)
        self.assertEqual(config['enabled_providers'], ['opencode'])
        command = smoke.build_command('/opt/opencode/bin/opencode')
        self.assertEqual(command[command.index('--model') + 1], MODEL)

    def test_config_content_never_contains_credential_or_endpoints(self):
        content = self.env('secret-key')['OPENCODE_CONFIG_CONTENT']
        self.assertNotIn('apiKey', content)
        self.assertNotIn('baseURL', content)
        # No provider options block (no baseURL/apiKey overrides): the JSON key
        # "provider" itself must not appear — "enabled_providers" is distinct.
        self.assertNotIn('"provider"', content)
        self.assertNotIn('secret-key', content)

    def test_empty_agent_home_is_rejected(self):
        with self.assertRaises(ValueError):
            smoke.build_child_env('', '/tmp/smoke-work', 'k')


# ---------------------------------------------------------------------------
# Transcript evaluation against real OpenCode 2.1.4 transcripts
# ---------------------------------------------------------------------------

class V2TranscriptEvaluationTests(unittest.TestCase):
    def test_captured_set_is_complete_and_from_the_pinned_runtime_shape(self):
        self.assertEqual(set(V2_TRANSCRIPTS), {
            'success_text', 'tool_read_ask_rejected',
            'tool_read_allow_completed', 'auth_401', 'forbidden_403',
            'truncated_length', 'retry_500_until_killed'})
        for name, case in V2_TRANSCRIPTS.items():
            for line in case['lines']:
                self.assertIn(json.loads(line)['type'],
                              ('text', 'step_start', 'step_finish', 'tool_use',
                               'error'), name)

    def test_bare_text_reply_is_ok(self):
        lines, code, timed_out = transcript('success_text')
        self.assertEqual([json.loads(line)['type'] for line in lines], ['text'])
        assert_outcome(self, lines, code, timed_out, 'ok', '')

    def test_read_rejected_under_ask_is_still_unexpected_tool_use(self):
        # The CLI exits 0 although the permission layer refused the call:
        # the transcript-level rejection is what fails the run.
        lines, code, timed_out = transcript('tool_read_ask_rejected')
        self.assertEqual(code, 0)
        assert_outcome(self, lines, code, timed_out,
                       'failed', 'unexpected_tool_use')

    def test_completed_tool_use_is_rejected_even_when_exit_is_zero(self):
        lines, code, timed_out = transcript('tool_read_allow_completed')
        self.assertEqual(code, 0)
        assert_outcome(self, lines, code, timed_out,
                       'failed', 'unexpected_tool_use')

    def test_wrong_bare_text_answer_is_output_mismatch(self):
        # A clean exit with some other reply must never count as a successful
        # probe: the mandated answer is exactly OK, in either v2 shape.
        assert_outcome(self, [text_event('Hello')], 0, False,
                       'failed', 'output_mismatch')

    def test_wrong_answer_on_a_clean_stepped_stop_is_output_mismatch(self):
        session = 'ses_e7f3df85fffeJWRHFxspCNW2ha'
        lines = [
            json.dumps({'type': 'step_start', 'sessionID': session,
                        'part': {'type': 'step-start'}}),
            text_event('NOT OK', session),
            json.dumps({'type': 'step_finish', 'sessionID': session,
                        'part': {'type': 'step-finish', 'reason': 'stop'}}),
        ]
        assert_outcome(self, lines, 0, False, 'failed', 'output_mismatch')

    def test_exact_ok_on_a_clean_stepped_stop_is_ok(self):
        session = 'ses_e7f3df85fffeJWRHFxspCNW2ha'
        lines = [
            json.dumps({'type': 'step_start', 'sessionID': session,
                        'part': {'type': 'step-start'}}),
            text_event('OK', session),
            json.dumps({'type': 'step_finish', 'sessionID': session,
                        'part': {'type': 'step-finish', 'reason': 'stop'}}),
        ]
        assert_outcome(self, lines, 0, False, 'ok', '')

    SESSION = 'ses_e7f3df85fffeJWRHFxspCNW2ha'

    def _shape(self, shape, *events):
        """Wrap text-bearing events in the bare-text or the stepped shape."""
        if shape == 'bare':
            return list(events)
        return ([json.dumps({'type': 'step_start', 'sessionID': self.SESSION,
                             'part': {'type': 'step-start'}})]
                + list(events)
                + [json.dumps({'type': 'step_finish',
                               'sessionID': self.SESSION,
                               'part': {'type': 'step-finish',
                                        'reason': 'stop'}})])

    def _status(self, lines, exit_code=0):
        return smoke.evaluate(lines, exit_code, False)[0]

    def test_only_the_exact_ok_text_is_accepted_in_both_shapes(self):
        for shape in ('bare', 'stepped'):
            with self.subTest(shape=shape):
                assert_outcome(self, self._shape(shape, text_event('OK')),
                               0, False, 'ok', '')

    def test_surrounding_whitespace_is_output_mismatch_in_both_shapes(self):
        # The contract is exactly OK: no trimming of the model's reply.
        texts = (' OK', 'OK ', ' OK ', '\tOK', 'OK\t', '\nOK', 'OK\n',
                 'OK\r\n', '\r\nOK', ' \t\n OK \n\t ', '\u00a0OK',
                 'OK\u2003', 'OK\u200b', '\u2028OK', 'OK\x0b', '\x0cOK')
        for shape in ('bare', 'stepped'):
            for text in texts:
                with self.subTest(shape=shape, text=text):
                    assert_outcome(self, self._shape(shape, text_event(text)),
                                   0, False, 'failed', 'output_mismatch')

    def test_additional_or_altered_text_is_output_mismatch(self):
        texts = ('OK.', 'OK!', 'OK OK', 'OKAY', 'ok', 'Ok', 'oK', 'O K',
                 'OK: done', 'Sure. OK', 'OK\nOK', '0K', 'O', 'K', '"OK"',
                 '`OK`', 'OKay')
        for shape in ('bare', 'stepped'):
            for text in texts:
                with self.subTest(shape=shape, text=text):
                    assert_outcome(self, self._shape(shape, text_event(text)),
                                   0, False, 'failed', 'output_mismatch')

    def test_empty_or_blank_text_never_passes(self):
        for shape in ('bare', 'stepped'):
            for text in ('', ' ', '\n', '\t', ' \t\r\n '):
                with self.subTest(shape=shape, text=text):
                    self.assertEqual(
                        self._status(self._shape(shape, text_event(text))),
                        'failed')

    def test_split_or_repeated_text_parts_never_pass(self):
        # "OK" delivered as several parts, or OK plus any extra part, is not
        # a single exact reply.
        for shape in ('bare', 'stepped'):
            for parts in (('O', 'K'), ('OK', 'OK'), ('OK', ''), ('', 'OK'),
                          ('OK', ' '), (' ', 'OK'), ('OK', 'extra')):
                with self.subTest(shape=shape, parts=parts):
                    events = [text_event(part, stamp=i)
                              for i, part in enumerate(parts)]
                    self.assertEqual(
                        self._status(self._shape(shape, *events)), 'failed')

    def test_malformed_text_structures_never_pass(self):
        def raw(part):
            body = {'type': 'text', 'sessionID': self.SESSION}
            if part is not ...:
                body['part'] = part
            return json.dumps(body)
        malformed = {
            'part_missing': raw(...),
            'part_null': raw(None),
            'part_string': raw('OK'),
            'part_list': raw(['OK']),
            'text_missing': raw({'type': 'text'}),
            'text_null': raw({'type': 'text', 'text': None}),
            'text_number': raw({'type': 'text', 'text': 0}),
            'text_bool': raw({'type': 'text', 'text': True}),
            'text_list': raw({'type': 'text', 'text': ['OK']}),
            'text_object': raw({'type': 'text', 'text': {'text': 'OK'}}),
            'text_codepoints': raw({'type': 'text', 'text': [79, 75]}),
            'wrong_key': raw({'type': 'text', 'content': 'OK'}),
        }
        for shape in ('bare', 'stepped'):
            for name, event in malformed.items():
                with self.subTest(shape=shape, case=name):
                    self.assertEqual(
                        self._status(self._shape(shape, event)), 'failed')

    def test_a_valid_ok_does_not_rescue_a_malformed_sibling(self):
        malformed = json.dumps({'type': 'text', 'sessionID': self.SESSION,
                                'part': {'type': 'text', 'text': 7}})
        for shape in ('bare', 'stepped'):
            with self.subTest(shape=shape):
                self.assertEqual(self._status(self._shape(
                    shape, text_event('OK'), malformed)), 'failed')

    def test_ok_in_a_non_text_event_does_not_count(self):
        reasoning = json.dumps({'type': 'reasoning', 'sessionID': self.SESSION,
                                'part': {'type': 'reasoning', 'text': 'OK'}})
        for shape in ('bare', 'stepped'):
            with self.subTest(shape=shape):
                self.assertEqual(
                    self._status(self._shape(shape, reasoning)), 'failed')

    def test_exact_ok_with_a_nonzero_exit_still_fails(self):
        for shape in ('bare', 'stepped'):
            with self.subTest(shape=shape):
                self.assertEqual(self._status(
                    self._shape(shape, text_event('OK')), exit_code=1),
                    'failed')

    def test_non_object_json_lines_fail_without_crashing(self):
        for line in ('[]', 'null', '"OK"', '7', 'true', '{"type": 5}', '{'):
            with self.subTest(line=line):
                self.assertEqual(self._status([line]), 'failed')

    def test_unauthorized_401_reports_auth_error(self):
        lines, code, timed_out = transcript('auth_401')
        assert_outcome(self, lines, code, timed_out,
                       'failed', 'provider_error', 'auth_error', 401)

    def test_forbidden_403_is_told_apart_from_401_by_status_only(self):
        # v2 labels both `provider.auth`; the numeric status must decide.
        lines, code, timed_out = transcript('forbidden_403')
        self.assertEqual(json.loads(lines[0])['error']['type'], 'provider.auth')
        assert_outcome(self, lines, code, timed_out,
                       'failed', 'provider_error', 'forbidden', 403)

    def test_truncated_length_fails(self):
        lines, code, timed_out = transcript('truncated_length')
        assert_outcome(self, lines, code, timed_out, 'failed', 'provider_error',
                       'unknown_provider_error', None)
        # Cut right after the first step_finish(length) (no error event yet):
        # still not a stop termination.
        assert_outcome(self, lines[:3], 0, False,
                       'failed', 'incomplete_generation:length')

    def test_endless_retry_is_a_timeout(self):
        lines, code, timed_out = transcript('retry_500_until_killed')
        self.assertTrue(timed_out)
        assert_outcome(self, lines, code, timed_out, 'failed', 'timeout')
        # Without the timeout flag, step_start-only output has no termination.
        assert_outcome(self, lines, 0, False,
                       'failed', 'no_supported_termination')


class GatewayStatusPrecedenceTests(unittest.TestCase):
    def test_provider_error_class_wins_over_nonzero_exit(self):
        # A production-relevant gateway 403 must be recorded as forbidden,
        # not dwarfed into nonzero_exit:1.
        line = ('{"type":"error","timestamp":1,"sessionID":"ses_e7f3af832ffe86F11f",'
                '"error":{"type":"provider.auth","message":"x","status":403}}')
        self.assertEqual(smoke.evaluate([line], 1, False),
                         ('failed', 'provider_error', 'forbidden', 403))

    def test_gateway_403_is_classified_forbidden_directly(self):
        self.assertEqual(smoke.classify_provider_error(403), 'forbidden')


class TranscriptEdgeCaseTests(unittest.TestCase):
    def test_unparseable_output_fails(self):
        self.assertEqual(smoke.evaluate(['{not json'], 0, False)[1],
                         'unparseable_output')

    def test_unknown_event_type_fails(self):
        self.assertEqual(smoke.evaluate(['{"type":"mystery"}'], 0, False)[1],
                         'unknown_event_type')

    def test_no_events_fails(self):
        self.assertEqual(smoke.evaluate([], 0, False), ('failed', 'no_events',
                                                        None, None))
        self.assertEqual(smoke.evaluate(['', '  '], 0, False)[1], 'no_events')

    def test_bare_text_reply_needs_non_empty_text(self):
        self.assertEqual(smoke.evaluate([text_event('  ')], 0, False)[1],
                         'no_supported_termination')

    def test_bare_text_reply_needs_a_clean_exit(self):
        self.assertEqual(smoke.evaluate([text_event()], 1, False)[1],
                         'nonzero_exit:1')

    def test_events_from_more_than_one_session_fail(self):
        lines = [text_event(session='ses_aaaaaaaa1'),
                 text_event(session='ses_bbbbbbbb2')]
        self.assertEqual(smoke.evaluate(lines, 0, False)[1], 'session_mismatch')
        missing = json.dumps({'type': 'text', 'part': {'text': 'OK'}})
        self.assertEqual(smoke.evaluate([missing], 0, False)[1],
                         'session_mismatch')

    def test_missing_termination_fails(self):
        line = ('{"type":"step_start","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-start"}}')
        self.assertEqual(smoke.evaluate([line], 0, False)[1],
                         'no_supported_termination')

    def test_malformed_step_finish_fails(self):
        line = ('{"type":"step_finish","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-finish"}}')
        self.assertEqual(smoke.evaluate([line], 0, False)[1], 'malformed_event')

    def test_error_without_status_is_unknown_provider_error(self):
        line = ('{"type":"error","sessionID":"ses_ea5c01","error":'
                '{"type":"output-limit","message":"x"}}')
        self.assertEqual(smoke.evaluate([line], 1, False),
                         ('failed', 'provider_error', 'unknown_provider_error',
                          None))

    def test_stepped_stop_with_nonzero_exit_fails(self):
        line = ('{"type":"step_finish","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-finish","reason":"stop"}}')
        self.assertEqual(smoke.evaluate([line], 3, False)[1], 'nonzero_exit:3')

    def test_any_tool_use_is_rejected(self):
        for tool in ('read', 'shell', 'execute', 'write', 'subagent'):
            line = json.dumps({'type': 'tool_use', 'sessionID': 'ses_ea5c01',
                               'part': {'type': 'tool', 'tool': tool,
                                        'state': {'status': 'error'}}})
            self.assertEqual(smoke.evaluate([line, text_event()], 0, False),
                             ('failed', 'unexpected_tool_use', None, None), tool)

    def test_reason_escaper_drops_everything_outside_the_vocabulary(self):
        self.assertEqual(smoke._esc_reason('Length cap'), 'length-cap')
        self.assertEqual(smoke._esc_reason('stop; exit\nbad'), 'stop-exit-bad')
        self.assertEqual(smoke._esc_reason(''), '')


# Measured on the real @opencode/cli 2.1.4 linux-x64 binary: `opencode
# --version` prints exactly this (v1.19.3 printed a bare `1.19.3`).
REAL_PINNED_VERSION_OUTPUT = 'opencode v2.1.4\n'


class VersionParsingTests(unittest.TestCase):
    def test_accepts_real_pinned_binary_output(self):
        self.assertEqual(smoke.parse_version(REAL_PINNED_VERSION_OUTPUT),
                         PINNED_VERSION)

    def test_accepts_bare_prefixed_and_banner_formats(self):
        self.assertEqual(smoke.parse_version('2.1.4'), '2.1.4')
        self.assertEqual(smoke.parse_version('v2.1.4'), '2.1.4')
        self.assertEqual(smoke.parse_version('opencode 2.1.4'), '2.1.4')
        self.assertEqual(smoke.parse_version('  opencode v2.1.4  \n'), '2.1.4')

    def test_ignores_blank_lines_around_the_version(self):
        self.assertEqual(smoke.parse_version('\nopencode v2.1.4\n\n'), '2.1.4')

    def test_rejects_unmatchable_or_ambiguous_output(self):
        self.assertIsNone(smoke.parse_version('nothing here'))
        self.assertIsNone(smoke.parse_version('v2.1'))             # 2 parts
        self.assertIsNone(smoke.parse_version('opencode v2.1.4 extra'))
        self.assertIsNone(smoke.parse_version('build-2.1.4'))
        self.assertIsNone(smoke.parse_version('2.1.4\n2.1.9'))     # conflicting
        self.assertIsNone(smoke.parse_version(''))                 # empty
        self.assertIsNone(smoke.parse_version('2.1.4.0'))          # 4 parts

    def test_expected_version_matches_every_repo_pin(self):
        self.assertEqual(smoke.EXPECTED_OPENCODE_VERSION, PINNED_VERSION)
        package = json.loads(
            (ROOT / '.github/opencode/runtime/package.json').read_text())
        self.assertEqual(smoke.EXPECTED_OPENCODE_VERSION,
                         package['dependencies']['@opencode/cli'])
        self.assertNotIn('opencode-ai', package['dependencies'])
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
        self.assertEqual(smoke.EXPECTED_OPENCODE_VERSION,
                         workflow['env']['OPENCODE_VERSION'])


# ---------------------------------------------------------------------------
# Sanitized artifact: closed field list, nothing sensitive can enter
# ---------------------------------------------------------------------------

class ArtifactSanitizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def write_record(self):
        return smoke._write_result(
            os.path.join(self.tmp, 'result.json'),
            opencode_version=PINNED_VERSION, status='failed',
            reason='provider_error', error_class='forbidden',
            error_status_code=403, exit_code=1, timed_out=False,
            event_count=1, stderr_lines=1, setup_ms=None, auth_ms=None,
            inference_ms=None, total_ms=123, execution_count=1,
            output_tokens=None,
        )

    def test_record_field_list_is_exactly_the_closed_set(self):
        record = self.write_record()
        self.assertEqual(set(record), smoke.RESULT_KEYS)

    def test_persisted_record_matches_the_returned_record(self):
        record = self.write_record()
        with open(os.path.join(self.tmp, 'result.json'), encoding='utf-8') as h:
            self.assertEqual(json.load(h), record)

    def test_gateway_403_evidence_carries_only_safe_classes(self):
        # Reproduce the production-relevant failure in the v2 error shape:
        # record everything the sanitizer allows around a gateway 403, then
        # prove the sensitive channel data never made it into the record.
        line = json.dumps({'type': 'error', 'timestamp': 1,
                           'sessionID': 'ses_e7f3af832ffe86F11f',
                           'error': {'type': 'provider.auth',
                                     'message': 'Upstream request failed: denied',
                                     'status': 403,
                                     'response': {'body': '{"message": "denied"}',
                                                  'headers': {'cf-ray': 'abc'},
                                                  'url': 'https://opencode.ai/x'}}})
        status, reason, error_class, error_status_code = smoke.evaluate(
            [line], 1, False)
        record = smoke._write_result(
            os.path.join(self.tmp, 'r.json'),
            opencode_version=PINNED_VERSION, status=status, reason=reason,
            error_class=error_class, error_status_code=error_status_code,
            exit_code=1, timed_out=False, event_count=1, stderr_lines=0,
            setup_ms=None, auth_ms=None, inference_ms=None, total_ms=7,
            execution_count=1, output_tokens=None)
        text = json.dumps(record)
        self.assertEqual((record['error_class'], record['error_status_code']),
                         ('forbidden', 403))
        for sensitive in ('denied', 'response', 'body', 'cf-ray', 'opencode.ai',
                          'provider.auth', 'Upstream request'):
            self.assertNotIn(sensitive, text)

    def test_real_401_transcript_body_never_enters_the_record(self):
        lines, code, timed_out = transcript('auth_401')
        record = smoke._write_result(
            os.path.join(self.tmp, 'r.json'), opencode_version=PINNED_VERSION,
            status='failed', reason='provider_error',
            error_class=smoke.evaluate(lines, code, timed_out)[2],
            error_status_code=401, exit_code=code, timed_out=timed_out,
            event_count=len(lines), stderr_lines=0, setup_ms=None,
            auth_ms=None, inference_ms=None, total_ms=1, execution_count=1,
            output_tokens=None)
        error = json.loads(lines[0])['error']
        text = json.dumps(record)
        self.assertNotIn(error['message'], text)
        self.assertNotIn('authentication_error', text)

    def test_missing_key_record_is_sanitized_and_nonzero(self):
        record_path = os.path.join(self.tmp, 'r.json')
        with tempfile.TemporaryDirectory() as root:
            code = smoke.main([
                'opencode_native_smoke.py', record_path,
                os.path.join(root, 'opencode'),
                os.path.join(root, 'home'), os.path.join(root, 'work'), '10',
            ])
        with open(record_path, encoding='utf-8') as h:
            record = json.load(h)
        self.assertEqual(code, 1)
        self.assertEqual(record['reason'], 'missing_api_key')
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['opencode_version'])
        self.assertIsNone(record['error_class'])

    def test_usage_error_returns_two(self):
        self.assertEqual(smoke.main(['only-one']), 2)


class SummaryLineTests(unittest.TestCase):
    def test_summary_contains_only_sanitized_classes(self):
        record = {
            'model': MODEL, 'status': 'failed', 'reason': 'provider_error',
            'error_class': 'forbidden', 'error_status_code': 403,
            'total_ms': 1000,
        }
        line = smoke._summary(record)
        self.assertIn('forbidden', line)
        self.assertIn('total_ms=1000', line)
        for sensitive in ('scripted', 'message', 'http://', 'Bearer'):
            self.assertNotIn(sensitive, line)


# ---------------------------------------------------------------------------
# Credential-free compatibility preflight
# ---------------------------------------------------------------------------

class PreflightTests(unittest.TestCase):
    """A synthetic key must reach auth (401) — not be policy-rejected (403).

    This is the regression guard for the v1 free-tier `read: deny`
    incompatibility that made the merged diagnostic a false negative: the
    envelope was rejected before auth, so a valid-key run could never succeed.
    Whether the v2-era gateway still does this is unverified; the guard stays.
    """

    def test_preflight_never_reads_the_environment_api_key(self):
        key = smoke.resolve_key('preflight', {'OPENCODE_API_KEY': 'real-key'})
        self.assertEqual(key, smoke.SYNTHETIC_KEY)
        self.assertNotIn('real-key', key)

    def test_live_mode_reads_only_the_documented_variable(self):
        self.assertEqual(
            smoke.resolve_key('live', {'OPENCODE_API_KEY': 'k'}), 'k')
        self.assertEqual(smoke.resolve_key('live', {}), '')
        self.assertEqual(
            smoke.resolve_key('live', {'OPENCODE_KEY': 'other'}), '')

    def test_auth_rejection_means_the_envelope_is_compatible(self):
        self.assertEqual(
            smoke.probe_verdict('failed', 'provider_error', 'auth_error', 401),
            ('ok', 'credential_rejected_as_expected'))

    def test_real_v2_401_and_403_transcripts_map_to_the_two_verdicts(self):
        for name, verdict in (('auth_401', ('ok', 'credential_rejected_as_expected')),
                              ('forbidden_403', ('failed', 'config_envelope_rejected'))):
            self.assertEqual(smoke.probe_verdict(*smoke.evaluate(*transcript(name))),
                             verdict, name)

    def test_policy_rejection_means_the_envelope_is_incompatible(self):
        self.assertEqual(
            smoke.probe_verdict('failed', 'provider_error', 'forbidden', 403),
            ('failed', 'config_envelope_rejected'))

    def test_any_other_probe_outcome_fails_closed(self):
        for outcome in (('ok', '', None, None),
                        ('failed', 'provider_error', 'server_error', 500),
                        ('failed', 'timeout', None, None),
                        ('failed', 'no_events', None, None),
                        ('failed', 'missing_api_key', None, None),
                        ('failed', 'provider_error', None, None)):
            self.assertEqual(smoke.probe_verdict(*outcome),
                             ('failed', 'unexpected_probe_outcome'), outcome)

    def test_preflight_record_carries_the_mode_and_no_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = smoke._write_result(
                os.path.join(tmp, 'preflight.json'), mode='preflight',
                opencode_version=PINNED_VERSION, status='ok', reason='',
                error_class='auth_error', error_status_code=401,
                exit_code=1, timed_out=False, event_count=1, stderr_lines=0,
                setup_ms=None, auth_ms=None, inference_ms=None, total_ms=42,
                execution_count=1, output_tokens=None)
        self.assertEqual(record['mode'], 'preflight')
        self.assertEqual(set(record), smoke.RESULT_KEYS)
        self.assertNotIn('synthetic-compatibility-probe-not-a-credential',
                         json.dumps(record))

    def test_default_record_mode_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = smoke._write_result(
                os.path.join(tmp, 'result.json'), opencode_version=PINNED_VERSION,
                status='ok', reason='', error_class=None,
                error_status_code=None, exit_code=0, timed_out=False,
                event_count=3, stderr_lines=0, setup_ms=None, auth_ms=None,
                inference_ms=None, total_ms=1, execution_count=1,
                output_tokens=None)
        self.assertEqual(record['mode'], 'live')

    def test_cli_runs_with_stdin_closed_in_its_own_process_group(self):
        # An open idle stdin blocks a v2 run before any provider contact; the
        # child must always get /dev/null, and its own session so the whole
        # group can be killed on timeout. main() must go through _run_cli().
        source = inspect.getsource(smoke._run_cli)
        self.assertIn('stdin=subprocess.DEVNULL', source)
        self.assertIn("start_new_session=(os.name == 'posix')", source)
        self.assertIn('_run_cli(', inspect.getsource(smoke.main))
        self.assertNotIn('subprocess.run(command', inspect.getsource(smoke.main))

    @unittest.skipIf(os.name != 'posix',
                     '_phase_timings seal logic is POSIX-only')
    def test_non_dict_json_output_is_rejected_with_unparseable_output(self):
        # Non-dict JSON (array, null, number, string) parses as valid JSON but
        # has no .get() method. _phase_timings must skip it without crashing,
        # and evaluate() must already have classified it as unparseable_output.
        with tempfile.TemporaryDirectory() as tmp:
            stub_dir = Path(tmp) / 'stubdir'
            stub_dir.mkdir()
            stub = stub_dir / 'opencode'
            stub.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "--version" ]; then\n'
                '  echo "2.1.4"\n'
                '  exit 0\n'
                'fi\n'
                'printf "[]\\n"\n'
                'exit 1\n',
                encoding='utf-8')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                       | stat.S_IXOTH)
            result = os.path.join(tmp, 'result.json')
            agent_home = os.path.join(tmp, 'home')
            workdir = os.path.join(tmp, 'work')
            os.makedirs(agent_home)
            os.makedirs(workdir)
            environment = dict(os.environ)
            environment.pop('OPENCODE_API_KEY', None)
            environment['OPENCODE_API_KEY'] = 'synthetic-test-key'
            environment['OPENCODE_SMOKE_ACCOUNT_HOME'] = agent_home
            completed = subprocess.run(
                [sys.executable,
                 str(ROOT / '.github/scripts/opencode_native_smoke.py'),
                 result, str(stub), agent_home, workdir, '2'],
                env=environment, capture_output=True, text=True,
                timeout=300)
            record = None
            if os.path.exists(result):
                with open(result, encoding='utf-8') as handle:
                    record = json.load(handle)
            self.assertEqual(completed.returncode, 1, completed.stdout)
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['reason'], 'unparseable_output')
            self.assertEqual(record['error_class'], None)
            self.assertEqual(record['error_status_code'], None)
            self.assertEqual(set(record), smoke.RESULT_KEYS)


# ---------------------------------------------------------------------------
# Workflow YAML policy tests
# ---------------------------------------------------------------------------

def _find_posix_shell_available() -> str | None:
    """Find a POSIX shell that shares the parent process environment.

    Deterministic candidates only: the CI runner's /bin/bash (or /bin/sh), or
    Git-for-Windows bash which shares the Windows env. Windows' WSL bash.exe
    is deliberately not used: it does not inherit arbitrary parent env vars,
    so GITHUB_* variables would silently vanish.
    """
    candidates = ('/bin/bash', '/bin/sh',
                  r'C:\Program Files\Git\bin\bash.exe',
                  r'C:\Program Files\Git\usr\bin\bash.exe')
    for candidate in candidates:
        if os.path.isfile(candidate) and (os.name == 'posix' or 'git' in
                                          candidate.lower()):
            return candidate
    return None


class WorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding='utf-8')
        cls.workflow = yaml.safe_load(cls.text)
        # YAML 1.1 parses the previously reserved JSON/YAML on-key as a bool.
        cls.on = cls.workflow.get('on', cls.workflow.get(True))
        cls.job = cls.workflow['jobs']['smoke']

    def test_trigger_is_dispatch_only_without_inputs(self):
        self.assertEqual(set(self.on), {'workflow_dispatch'})
        dispatch = self.on['workflow_dispatch']
        if isinstance(dispatch, dict):
            self.assertFalse(dispatch.get('inputs'))
        self.assertNotIn('workflow_call', self.on)
        self.assertNotIn('schedule', self.on)

    def test_top_level_permissions_are_empty(self):
        self.assertEqual(self.workflow['permissions'], {})

    def test_top_level_env_carries_only_the_version_pin(self):
        self.assertEqual(self.workflow.get('env', {}),
                         {'OPENCODE_VERSION': PINNED_VERSION})

    def test_job_is_protected_and_read_only(self):
        self.assertEqual(self.job['environment'], 'opencode-agent')
        self.assertEqual(self.job['permissions'], {'contents': 'read'})
        self.assertLessEqual(self.job['timeout-minutes'], 15)
        self.assertEqual(self.job['runs-on'], 'ubuntu-latest')
        self.assertEqual(self.workflow['jobs'].keys(), {'smoke'})

    def test_job_condition_pins_dispatch_to_main(self):
        condition = self.job['if']
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)

    def test_fail_closed_guard_is_the_first_step_and_is_secret_free(self):
        guard = self.job['steps'][0]
        self.assertEqual(guard.get('name'),
                         'Enforce trusted main dispatch (no secrets at this point)')
        run = guard.get('run', '')
        # Only documented default runner variables, no invented context keys.
        for required in ('workflow_dispatch', 'refs/heads/main'):
            self.assertIn(required, run)
        self.assertNotIn('GITHUB_ENVIRONMENT', run)
        self.assertFalse(guard.get('uses'))
        # The guard runs before every step that could receive a secret.
        secret_steps = [s for s in self.job['steps']
                        if 'secrets.' in json.dumps(s)]
        self.assertEqual(len(secret_steps), 1)
        self.assertGreater(self.job['steps'].index(secret_steps[0]), 0)

    def test_opencode_agent_environment_is_still_bound(self):
        # The environment binding is enforced by the job-level key, not by
        # an undocumented runner variable inside the guard script.
        self.assertEqual(self.job['environment'], 'opencode-agent')

    def test_secret_is_exposed_to_exactly_one_run_step(self):
        expected = {'OPENCODE_API_KEY': '${{ secrets.OPENCODE_API_KEY }}'}
        holders = [s for s in self.job['steps'] if s.get('env') == expected]
        self.assertEqual(len(holders), 1)
        self.assertTrue(holders[0]['name'].startswith('Run native CLI smoke test'))
        # The key must never be interpolated into any run block.
        for step in self.job['steps']:
            self.assertNotIn('${{ secrets', step.get('run', ''))

    def install_step(self):
        installs = [s for s in self.job['steps'] if 'npm ci' in s.get('run', '')]
        self.assertEqual(len(installs), 1)
        return installs[0]

    def test_install_step_runs_without_the_key(self):
        self.assertNotIn('OPENCODE_API_KEY',
                         json.dumps(self.install_step().get('env', '')))

    def test_install_step_checks_the_v2_binary_and_its_exact_version_string(self):
        run = self.install_step()['run']
        self.assertIn('npm ci --ignore-scripts', run)
        self.assertIn(f'BIN={BINARY_PATH}', run)
        self.assertIn('test -f "${BIN}"', run)
        # v2 prints `opencode v<semver>`; the comparison must be exact
        # equality against that string, not a substring match.
        self.assertIn('test "$("${BIN}" --version </dev/null)" = '
                      '"opencode v${OPENCODE_VERSION}"', run)
        self.assertNotIn('opencode-linux-x64', self.text)

    def test_all_external_actions_are_pinned_to_full_shas(self):
        def walk(node):
            if isinstance(node, dict):
                if 'uses' in node:
                    ref = node['uses']
                    self.assertRegex(ref, r'[^@\s]+@[0-9a-f]{40}$')
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(self.workflow)

    def test_artifact_is_delivered_regardless_of_outcome(self):
        uploads = [s for s in self.job['steps']
                   if s.get('uses', '').startswith('actions/upload-artifact')]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['retention-days'], 1)
        paths = {line.strip() for line in
                 upload['with']['path'].splitlines() if line.strip()}
        self.assertEqual(paths, {'${{ runner.temp }}/smoke-preflight.json',
                                 '${{ runner.temp }}/smoke-result.json'})
        self.assertEqual(upload['with']['name'], 'opencode-native-smoke')

    # -- credential-free preflight (free-tier envelope regression guard) -----

    def preflight_step(self):
        matches = [s for s in self.job['steps']
                   if s.get('name') == 'Synthetic compatibility preflight '
                                      '(no credential)']
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_credential_free_preflight_precedes_the_secret_step(self):
        steps = self.job['steps']
        preflight = self.preflight_step()
        holders = [s for s in steps if 'secrets.' in json.dumps(s)]
        self.assertEqual(len(holders), 1)
        self.assertLess(steps.index(preflight), steps.index(holders[0]))
        # The preflight step must not receive or interpolate any secret.
        self.assertNotIn('secrets.', json.dumps(preflight))
        self.assertFalse(preflight.get('env'))
        self.assertNotIn('${{ ', preflight['run'])
        self.assertIn('--preflight', preflight['run'])
        self.assertIn('set -euo pipefail', preflight['run'])

    def test_preflight_uses_isolated_paths_and_the_pinned_binary(self):
        run = self.preflight_step()['run']
        for path in ('${RUNNER_TEMP}/smoke-preflight.json',
                     '${RUNNER_TEMP}/preflight-home',
                     '${RUNNER_TEMP}/preflight-work'):
            self.assertIn(path, run)
        self.assertIn(BINARY_PATH, run)
        self.assertIn('opencode_native_smoke.py --preflight', run)

    def test_preflight_never_reads_the_api_key_in_the_runner_script(self):
        # Static guarantee: the preflight branch resolves the fixed synthetic
        # placeholder, and the child env for the preflight run cannot carry a
        # real key even if one leaked into the step environment.
        source = (ROOT / '.github/scripts/opencode_native_smoke.py').read_text(
            encoding='utf-8')
        self.assertIn("SYNTHETIC_KEY = 'synthetic-compatibility-probe-"
                      "not-a-credential'", source)
        key = smoke.resolve_key('preflight', {'OPENCODE_API_KEY': 'real-key'})
        self.assertEqual(key, smoke.SYNTHETIC_KEY)

    def test_workflow_uses_only_the_standard_provider_integration(self):
        for banned in ('opencode_proxy.py', 'opencode_catalog.py',
                       'model-registry.json', 'proxy.port',
                       'OPENCODE_CONFIG_CONTENT', 'inference/openai',
                       'urllib', 'Bearer'):
            self.assertNotIn(banned, self.text)

    def test_model_is_fixed_in_the_job_and_step_identity(self):
        self.assertIn(MODEL, self.job['name'])
        run_steps = [s['name'] for s in self.job['steps']
                     if s['name'].startswith('Run native CLI smoke test')]
        self.assertEqual(len(run_steps), 1)
        # No input of any kind may retarget the tested model.
        self.assertNotIn('with:', json.dumps(self.on))

    def test_runner_script_reference_exists(self):
        script = ROOT / '.github/scripts/opencode_native_smoke.py'
        self.assertTrue(script.is_file())
        self.assertIn('.github/scripts/opencode_native_smoke.py', self.text)

    def test_checkout_is_sparse_and_credentials_are_not_persisted(self):
        checkouts = [s for s in self.job['steps']
                     if 'actions/checkout' in s.get('uses', '')]
        self.assertEqual(len(checkouts), 1)
        checkout = checkouts[0]
        patterns = set(checkout['with']['sparse-checkout'].split())
        self.assertEqual(patterns,
                         {'.github/scripts', '.github/opencode/runtime'})
        self.assertFalse(checkout['with']['persist-credentials'])

    def runner_step(self):
        return [s for s in self.job['steps']
                if s['name'].startswith('Run native CLI smoke test')][0]

    def test_isolation_directories_live_under_runner_temp(self):
        run = self.runner_step()['run']
        for path in ('${RUNNER_TEMP}/smoke-result.json',
                     '${RUNNER_TEMP}/smoke-home',
                     '${RUNNER_TEMP}/smoke-work'):
            self.assertIn(path, run)
        self.assertIn(BINARY_PATH, run)
        self.assertNotIn('${{ ', run)

    def test_exit_nonzero_contract_is_explicit(self):
        self.assertIn('set -euo pipefail', self.runner_step()['run'])


@unittest.skipUnless(_find_posix_shell_available() is not None,
                     'needs a POSIX shell that shares the parent env '
                     '(e.g. /bin/bash on the Linux CI runner); Windows WSL '
                     'bash does not inherit the parent environment')
class GuardBehaviorTests(unittest.TestCase):
    """Execute the actual guard script body from the workflow YAML under both
    valid-main and rejected conditions — behavior, not just text."""

    shell = _find_posix_shell_available()

    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding='utf-8'))
        cls.guard_script = workflow['jobs']['smoke']['steps'][0]['run']

    def run_guard(self, event, ref):
        # The guard body is piped via stdin: CI runners mount TMPDIR noexec,
        # so a chmod+exec of a temp file fails with "Permission denied".
        # Piping executes the exact same script text with the same env.
        shell = self.shell
        env = {k: v for k, v in os.environ.items() if k.startswith(('System',
                'PATH', 'PATHEXT', 'HOME'))}
        env.update({'GITHUB_EVENT_NAME': event, 'GITHUB_REF': ref})
        return subprocess.run(
            [shell], input=self.guard_script.replace('\r\n', '\n'),
            env=env, capture_output=True, text=True, timeout=60)

    def test_valid_main_dispatch_is_accepted(self):
        result = self.run_guard('workflow_dispatch', 'refs/heads/main')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_main_ref_is_rejected(self):
        result = self.run_guard('workflow_dispatch', 'refs/heads/feature-x')
        self.assertNotEqual(result.returncode, 0)

    def test_wrong_event_is_rejected(self):
        result = self.run_guard('issue_comment', 'refs/heads/main')
        self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# main() end to end with a scripted stub binary (POSIX only; no network)
# ---------------------------------------------------------------------------

def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipIf(os.name == 'nt', 'stub binaries are POSIX shell scripts')
class MainEndToEndTests(unittest.TestCase):
    """Run main() as a subprocess against a stub binary emitting real v2
    transcripts, then assert exit code, artifact fields and log hygiene.
    OPENCODE_API_KEY is a synthetic string; no gateway is contacted."""

    KEY = 'smoke-test-synthetic-key-not-real'
    BANNED_LOG_SUBSTRINGS = ('Upstream request', 'cf-ray', 'statusCode',
                             'response', 'Reconnect OpenCode', 'Bearer', KEY)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._account = os.path.join(self.tmp, 'account-home')
        os.makedirs(self._account)
        patcher = unittest.mock.patch.dict(
            os.environ, {'OPENCODE_SMOKE_ACCOUNT_HOME': self._account})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Registered last, so it runs first: sealed trees need their owner
        # write bits back before the temp dir can be removed.
        self.addCleanup(_restore_tree, self.tmp)

    def stub_binary(self, media: str, exit_code: int,
                    banner: str = 'opencode v2.1.4') -> Path:
        stub_dir = Path(self.tmp) / 'stubdir'
        stub_dir.mkdir(exist_ok=True)
        # The --version output uses the REAL pinned-binary format (see
        # REAL_PINNED_VERSION_OUTPUT). The transcript is emitted byte-for-byte
        # from a sibling file; a heredoc would append a trailing blank line.
        # Every inference invocation also records how it was launched:
        # argv, $PWD, the physical cwd and what its stdin is.
        (stub_dir / 'media.jsonl').write_text(media, encoding='utf-8')
        stub = stub_dir / 'opencode'
        stub.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "--version" ]; then\n'
            f'  echo "{banner}"\n'
            '  exit 0\n'
            'fi\n'
            'here="$(dirname "$0")"\n'
            'printf "%s\\n" "$@" > "$here/argv.txt"\n'
            'printf "%s\\n%s\\n%s\\n" "$PWD" "$(pwd -P)" '
            '"$(readlink /proc/self/fd/0)" > "$here/launch.txt"\n'
            'env | cut -d= -f1 | sort > "$here/envkeys.txt"\n'
            'cat "$here/media.jsonl"\n'
            f'exit {exit_code}\n',
            encoding='utf-8')
        _make_executable(stub)
        return stub

    def run_smoke(self, stub: Path, *, key: str | None = KEY,
                  binary: str | None = None, timeout_min: str = '10',
                  cwd: str | None = None, preflight: bool = False):
        environment = dict(os.environ)
        environment.pop('OPENCODE_API_KEY', None)  # never use a real key
        environment['PWD'] = '/'                   # a stale parent PWD
        if key is not None:
            environment['OPENCODE_API_KEY'] = key
        result = os.path.join(self.tmp, 'preflight.json' if preflight
                              else 'result.json')
        work_root = tempfile.mkdtemp(prefix='root', dir=self.tmp)   # fresh per run; nothing is removed
        self.agent_home = os.path.join(work_root, 'home')
        self.workdir = os.path.join(work_root, 'work')
        invocation_binary = binary or stub
        command = [sys.executable,
                   str(ROOT / '.github/scripts/opencode_native_smoke.py')]
        if preflight:
            command.append('--preflight')
        command.extend([result, str(invocation_binary), self.agent_home,
                        self.workdir, timeout_min])
        completed = subprocess.run(
            command, env=environment, capture_output=True, text=True,
            timeout=300, cwd=cwd)
        record = None
        if os.path.exists(result):
            with open(result, encoding='utf-8') as handle:
                record = json.load(handle)
        return completed, record

    def assert_log_hygiene(self, completed):
        for banned in self.BANNED_LOG_SUBSTRINGS:
            self.assertNotIn(banned, completed.stdout + completed.stderr)

    @staticmethod
    def media(name: str) -> str:
        return ''.join(line + '\n' for line in V2_TRANSCRIPTS[name]['lines'])

    def test_success_transcript_completes_with_zero_exit(self):
        stub = self.stub_binary(self.media('success_text'), 0)
        completed, record = self.run_smoke(stub)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['model'], MODEL)
        self.assertEqual(record['opencode_version'], PINNED_VERSION)
        self.assertGreater(record['total_ms'], 0)
        self.assertEqual(record['execution_count'], 1)
        # A bare-text reply has no step_finish event, hence no token count;
        # the field must still be present in the closed record.
        self.assertIsNone(record['output_tokens'])
        self.assertEqual(record['event_count'],
                         len(V2_TRANSCRIPTS['success_text']['lines']))

    def test_cli_is_launched_headless_in_the_workdir_with_the_exact_env(self):
        stub = self.stub_binary(self.media('success_text'), 0)
        completed, _record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        argv = (stub.parent / 'argv.txt').read_text().splitlines()
        self.assertEqual(argv, smoke.build_command('x')[1:])
        pwd, physical, stdin = (stub.parent / 'launch.txt').read_text().splitlines()
        # $PWD is the workdir (not the stale parent '/'), cwd matches it, and
        # stdin is /dev/null (never an open pipe that could block the run).
        self.assertEqual(pwd, self.workdir)
        self.assertEqual(physical, os.path.realpath(self.workdir))
        self.assertEqual(stdin, '/dev/null')
        keys = set((stub.parent / 'envkeys.txt').read_text().split())
        # Exactly the closed allowlist; a POSIX sh may add only its own
        # bookkeeping variables.
        self.assertLessEqual(smoke.CHILD_ENV_KEYS, keys)
        self.assertLessEqual(keys - smoke.CHILD_ENV_KEYS,
                             {'SHLVL', '_', 'OLDPWD'})

    def test_bare_version_banner_is_tolerated(self):
        stub = self.stub_binary(self.media('success_text'), 0, banner='2.1.4')
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['opencode_version'], PINNED_VERSION)

    def test_relative_binary_path_with_different_cwd(self):
        # Regression: the script must canonicalize the pinned binary to an
        # absolute path before changing cwd to the smoke workdir — a relative
        # binary path containing '/' would otherwise resolve from the wrong
        # directory (POSIX ENOENT).
        stub = self.stub_binary(self.media('success_text'), 0)
        relative_binary = os.path.relpath(stub, start=self.tmp)
        self.assertIn(os.sep, relative_binary)  # relative path with a slash
        completed, record = self.run_smoke(stub, binary=relative_binary,
                                           cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['opencode_version'], PINNED_VERSION)

    def test_tool_use_transcript_fails_although_the_cli_exits_zero(self):
        stub = self.stub_binary(self.media('tool_read_ask_rejected'), 0)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'unexpected_tool_use')
        self.assertEqual(record['exit_code'], 0)

    def test_gateway_401_fails_with_classified_artifact(self):
        stub = self.stub_binary(self.media('auth_401'), 1)
        completed, record = self.run_smoke(stub)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['reason'], 'provider_error')
        self.assertEqual(record['error_class'], 'auth_error')
        self.assertEqual(record['error_status_code'], 401)
        self.assertEqual(record['exit_code'], 1)

    def test_gateway_403_is_reported_as_forbidden(self):
        stub = self.stub_binary(self.media('forbidden_403'), 1)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['error_class'], 'forbidden')
        self.assertEqual(record['error_status_code'], 403)
        self.assertEqual(record['reason'], 'provider_error')
        self.assert_log_hygiene(completed)

    def test_version_mismatch_fails_before_any_inference(self):
        stub = Path(self.tmp) / 'stub-opencode-old'
        stub.write_text('#!/bin/sh\necho "opencode v2.1.9"\n', encoding='utf-8')
        _make_executable(stub)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'opencode_version_mismatch')
        self.assertEqual(record['opencode_version'], '2.1.9')
        self.assertIsNone(record['exit_code'])
        self.assertEqual(record['event_count'], 0)

    def test_v1_binary_is_rejected_as_a_version_mismatch(self):
        stub = Path(self.tmp) / 'stub-opencode-v1'
        stub.write_text('#!/bin/sh\necho "1.19.3"\n', encoding='utf-8')
        _make_executable(stub)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'opencode_version_mismatch')
        self.assertEqual(record['opencode_version'], '1.19.3')

    def test_missing_key_fails_before_any_execution(self):
        stub = self.stub_binary(self.media('success_text'), 0)
        completed, record = self.run_smoke(stub, key=None)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'missing_api_key')
        self.assertIsNone(record['opencode_version'])
        self.assertEqual(record['event_count'], 0)
        self.assertFalse((stub.parent / 'argv.txt').exists())
        self.assert_log_hygiene(completed)

    # -- credential-free preflight end to end --------------------------------

    def test_preflight_passes_when_auth_is_reached(self):
        stub = self.stub_binary(self.media('auth_401'), 1)
        completed, record = self.run_smoke(stub, preflight=True)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['mode'], 'preflight')
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['reason'], 'credential_rejected_as_expected')
        self.assertEqual(record['error_class'], 'auth_error')
        self.assertEqual(record['error_status_code'], 401)
        self.assertEqual(record['opencode_version'], PINNED_VERSION)

    def test_preflight_runs_without_any_key_in_the_environment(self):
        # The step must be genuinely credential-free: no OPENCODE_API_KEY is
        # needed for the synthetic compatibility result.
        stub = self.stub_binary(self.media('auth_401'), 1)
        completed, record = self.run_smoke(stub, key=None, preflight=True)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['mode'], 'preflight')

    def test_preflight_fails_on_policy_rejection_before_auth(self):
        stub = self.stub_binary(self.media('forbidden_403'), 1)
        completed, record = self.run_smoke(stub, preflight=True)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['mode'], 'preflight')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['reason'], 'config_envelope_rejected')
        self.assertEqual(record['error_class'], 'forbidden')
        self.assertEqual(record['error_status_code'], 403)

    def test_preflight_fails_closed_if_the_synthetic_key_was_accepted(self):
        # A synthetic non-credential key can never authenticate. If a run
        # looks successful the probe is meaningless and must not pass.
        stub = self.stub_binary(self.media('success_text'), 0)
        completed, record = self.run_smoke(stub, preflight=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'unexpected_probe_outcome')


@unittest.skipIf(os.name == 'nt', 'process groups and stub scripts are POSIX')
class HardTimeoutTests(unittest.TestCase):
    """v2 retries 429/5xx and broken streams forever, so the hard timeout is
    the only bound. _run_cli() must stop the run on time and leave nothing
    behind: the CLI and everything it spawned are killed and reaped."""

    def test_hung_run_and_its_descendants_are_killed_and_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, 'grandchild.pid')
            stub = Path(tmp) / 'opencode'
            stub.write_text(
                '#!/bin/sh\n'
                'echo \'{"type":"step_start"}\'\n'
                f'sleep 300 & echo $! > "{pid_file}"\n'
                'exec sleep 300\n', encoding='utf-8')
            _make_executable(stub)
            env = {'PATH': '/usr/bin:/bin', 'PWD': tmp}
            started = time.monotonic()
            stdout, _stderr, exit_code, timed_out = smoke._run_cli(
                [str(stub)], env, tmp, 2)
            lines = stdout.splitlines()
            elapsed = time.monotonic() - started
            with open(pid_file, encoding='utf-8') as handle:
                grandchild = int(handle.read())
        self.assertTrue(timed_out)
        self.assertIsNone(exit_code)   # killed: no exit status is reported
        self.assertLess(elapsed, 30)
        self.assertEqual(len(lines), 1)   # output before the kill is kept
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.1)
        self.assertFalse(_alive(grandchild),
                         'a process spawned by the CLI outlived the timeout')

    def test_normal_exit_is_not_a_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / 'opencode'
            stub.write_text('#!/bin/sh\necho line\necho err >&2\nexit 3\n',
                            encoding='utf-8')
            _make_executable(stub)
            outcome = smoke._run_cli([str(stub)], {'PATH': '/usr/bin:/bin'},
                                     tmp, 30)
        self.assertEqual(outcome, ('line\n', 'err\n', 3, False))

    def test_descendant_left_behind_by_a_clean_exit_is_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, 'grandchild.pid')
            stub = Path(tmp) / 'opencode'
            stub.write_text(
                '#!/bin/sh\n'
                f'sleep 300 </dev/null >/dev/null 2>&1 & echo $! > "{pid_file}"\n'
                'exit 0\n', encoding='utf-8')
            _make_executable(stub)
            outcome = smoke._run_cli([str(stub)], {'PATH': '/usr/bin:/bin'},
                                     tmp, 30)
            with open(pid_file, encoding='utf-8') as handle:
                grandchild = int(handle.read())
        self.assertEqual(outcome[2:], (0, False))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.1)
        self.assertFalse(_alive(grandchild),
                         'a process spawned by the CLI outlived a clean exit')


def _alive(pid: int) -> bool:
    """True while pid exists and is not a zombie."""
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as handle:
            return handle.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def _restore_tree(path: str) -> None:
    """Give owner write bits back so sealed test trees can be removed."""
    for current, _dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in files:
            os.chmod(os.path.join(current, name), 0o644)


class RuntimeHardeningTests(unittest.TestCase):
    def test_runner_has_no_assert_statements(self):
        # `python -O` strips assert; security checks must raise explicitly.
        import ast
        tree = ast.parse((ROOT / '.github/scripts/opencode_native_smoke.py')
                         .read_text(encoding='utf-8'))
        self.assertEqual([node.lineno for node in ast.walk(tree)
                          if isinstance(node, ast.Assert)], [])

    def test_child_env_forbids_package_installation(self):
        env = smoke.build_child_env('/tmp/agent-home', '/tmp/smoke-work', 'k')
        self.assertEqual(env['npm_config_offline'], 'true')
        self.assertIn('npm_config_offline', smoke.CHILD_ENV_KEYS)


@unittest.skipIf(os.name == 'nt', 'POSIX process groups and permission seal')
class PosixRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Runs after the temp-dir cleanup above is registered: restore owner
        # write bits first so sealed trees can be removed.
        self.addCleanup(_restore_tree, self._tmp.name)
        self.tmp = self._tmp.name
        self._account = os.path.join(self.tmp, 'account')
        os.makedirs(self._account)
        patcher = unittest.mock.patch.dict(
            os.environ, {'OPENCODE_SMOKE_ACCOUNT_HOME': self._account})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = os.path.join(self.tmp, 'agent')
        os.makedirs(self.agent)
        self.binary = os.path.join(self.tmp, 'opencode')
        with open(self.binary, 'w', encoding='utf-8') as handle:
            handle.write('#!/bin/sh\necho 2.1.4\n')
        os.chmod(self.binary, 0o755)
        self.manifest = os.path.join(self.tmp, 'result.json.runtime.json')

    def _alive(self, pid):
        """Return True only if pid is an actively executing process.

        A reaped process (no /proc entry) and a zombie (state 'Z', terminated
        but awaiting reaping) are both not alive: neither is executing.
        Any OSError reading /proc/{pid}/stat means the process is gone.
        """
        try:
            with open(f'/proc/{pid}/stat', encoding='utf-8') as handle:
                state = handle.read().rsplit(')', 1)[1].split()[0]
        except OSError:
            return False
        # Executable states per proc(5): R running, S interruptible sleep,
        # D uninterruptible sleep, T stopped, t traced/stopped. 'Z' is a
        # zombie: already terminated, not executing.
        return state in ('R', 'S', 'D', 't', 'T')

    def test_timeout_kills_the_whole_process_group(self):
        pidfile = os.path.join(self.tmp, 'grandchild.pid')
        script = os.path.join(self.tmp, 'hang.sh')
        with open(script, 'w', encoding='utf-8') as handle:
            handle.write('#!/bin/sh\nsleep 300 &\necho $! > %s\nsleep 300\n'
                         % pidfile)
        os.chmod(script, 0o755)
        started = time.monotonic()
        _stdout, _stderr, code, timed_out = smoke._run_cli(
            [script], {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
            self.tmp, 2)
        self.assertTrue(timed_out)
        self.assertIsNone(code)
        self.assertLess(time.monotonic() - started, 30)
        with open(pidfile, encoding='utf-8') as handle:
            grandchild = int(handle.read().strip())
        self.assertGreater(grandchild, 0,
                           'grandchild pid was not written by the script')
        # Bounded wait for process-state convergence after the group kill.
        deadline = time.monotonic() + 5
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self._alive(grandchild),
                         'a descendant survived the group kill '
                         '(grandchild pid %d was not terminated)' % grandchild)

    def test_prepare_then_validate_round_trip(self):
        smoke.prepare_runtime(self.agent, self.manifest, self.binary)
        smoke.validate_runtime(self.agent, self.manifest, self.binary)

    def test_sealed_roots_refuse_writes_by_the_run_identity(self):
        if os.geteuid() == 0:
            self.skipTest('root bypasses owner write bits')
        smoke.prepare_runtime(self.agent, self.manifest, self.binary)
        with self.assertRaises(PermissionError):
            open(os.path.join(self.agent, smoke.RUNTIME_DIR, 'planted.json'),
                 'w', encoding='utf-8')
        with self.assertRaises(PermissionError):
            open(os.path.join(self._account, '.opencode', 'planted.json'),
                 'w', encoding='utf-8')

    def test_planted_agent_config_blocks_the_seal(self):
        config_root = os.path.join(self.agent, smoke.RUNTIME_DIR)
        os.makedirs(config_root)
        with open(os.path.join(config_root, 'planted.json'), 'w',
                  encoding='utf-8') as handle:
            handle.write('{}')
        with self.assertRaises(ValueError) as caught:
            smoke.prepare_runtime(self.agent, self.manifest, self.binary)
        self.assertEqual(str(caught.exception), 'runtime_config_not_empty')

    def test_unsealed_root_is_rejected_by_validation(self):
        smoke.prepare_runtime(self.agent, self.manifest, self.binary)
        os.chmod(os.path.join(self.agent, smoke.RUNTIME_DIR), 0o755)
        with self.assertRaises(ValueError) as caught:
            smoke.validate_runtime(self.agent, self.manifest, self.binary)
        self.assertEqual(str(caught.exception), 'runtime_not_sealed')

    def test_tampered_binary_is_rejected_by_validation(self):
        smoke.prepare_runtime(self.agent, self.manifest, self.binary)
        with open(self.binary, 'a', encoding='utf-8') as handle:
            handle.write('# tampered\n')
        with self.assertRaises(ValueError) as caught:
            smoke.validate_runtime(self.agent, self.manifest, self.binary)
        self.assertEqual(str(caught.exception), 'runtime_integrity_mismatch')


if __name__ == '__main__':
    unittest.main()
