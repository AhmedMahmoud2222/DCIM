"""OpenCode native CLI smoke tests (smoke tests v1.0).

Validates the native smoke diagnostic end to end against the real 1.19.3
event fixtures (tests/deployment/fixtures/opencode_1_19_3), the workflow's
permission model, and secret isolation:

  - fixed model and fixed synthetic prompt (no parameter can change either);
  - exact child environment allowlist (nothing inherited, key isolated);
  - standard provider integration only: OPENCODE_API_KEY is the only
    credential channel and never appears outside the protected step;
  - sanitized artifact: closed field list; prompt, key, URLs, headers,
    response bodies and stderr text can never enter the record;
  - failure reporting: provider HTTP status classes, tool-use rejection,
    timeouts, non-stop terminations and nonzero exits all fail the run;
  - workflow YAML: permissions, fixed-scope dispatch, fail-closed main guard,
    preserved artifact and version agreement with the script.

main() runs end to end against a scripted stub binary (no network, no real
key); the stubs are POSIX-only, matching the production Ubuntu runner and the
opencode_guard.py environment budget.
"""
import importlib.util
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'opencode_native_smoke',
    ROOT / '.github/scripts/opencode_native_smoke.py',
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

FIXTURES = ROOT / 'tests/deployment/fixtures/opencode_1_19_3'
WORKFLOW_PATH = ROOT / '.github/workflows/opencode-native-smoke.yml'

MODEL = 'opencode/glm-5.3-flash-free'
PROMPT = 'Respond with exactly OK'


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / f'{name}.jsonl').read_text(encoding='utf-8').splitlines()


def fixture_meta(name: str) -> dict:
    return json.loads((FIXTURES / f'{name}.meta.json').read_text(encoding='utf-8'))


def assert_outcome(case, lines, exit_code, timed_out, status, reason,
                   error_class=None, error_status_code=None):
    """Compare a complete evaluate() outcome tuple."""
    case.assertEqual(
        smoke.evaluate(lines, exit_code, timed_out),
        (status, reason, error_class, error_status_code),
        (lines[:2], exit_code, timed_out),
    )


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
        self.assertEqual(list(sig.parameters), ['binary', 'workdir'])
        # evaluate() and main() take fixed tuples only; no model/prompt flags.
        self.assertEqual(list(inspect.signature(smoke.evaluate).parameters),
                         ['lines', 'exit_code', 'timed_out'])

    def test_command_carries_model_prompt_and_no_credential(self):
        command = smoke.build_command('/opt/opencode/bin/opencode', '/work')
        self.assertEqual(command, [
            '/opt/opencode/bin/opencode', 'run', '--pure', '--format', 'json',
            '--model', MODEL, '--dir', '/work', '--', PROMPT,
        ])
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
    def test_env_is_an_exact_closed_allowlist(self):
        env = smoke.build_child_env('/tmp/agent-home', 'secret-key')
        self.assertEqual(set(env), smoke.CHILD_ENV_KEYS)

    def test_key_present_only_in_its_designated_slot(self):
        env = smoke.build_child_env('/tmp/agent-home', 'secret-key-value')
        self.assertEqual(env['OPENCODE_API_KEY'], 'secret-key-value')

    def test_version_probe_env_omits_the_key_entirely(self):
        env = smoke.build_child_env('/tmp/agent-home', '')
        self.assertNotIn('OPENCODE_API_KEY', env)

    def test_home_and_xdg_paths_live_under_agent_home_only(self):
        env = smoke.build_child_env('/tmp/agent-home', 'k')
        for key, value in env.items():
            if key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                       'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
                self.assertTrue(value.startswith('/tmp/agent-home'), key)

    def test_config_content_denies_every_tool_of_the_pinned_runtime(self):
        env = smoke.build_child_env('/tmp/agent-home', 'k')
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['enabled_providers'], ['opencode'])
        self.assertFalse(config['autoupdate'])
        self.assertEqual(config['share'], 'disabled')
        # Validated permission kinds of opencode-ai 1.19.3 (binary evidence):
        # file/access tools plus the delegation kinds. Nothing may be 'allow'.
        for tool in ('edit', 'read', 'bash', 'webfetch',
                     'external_directory', 'task', 'todowrite'):
            self.assertEqual(config['permission'][tool], 'deny', tool)
        self.assertNotIn('allow', json.dumps(config['permission']))

    def test_no_other_model_is_selectable_for_any_request(self):
        # C1: the primary model, the CLI --model flag and the auxiliary/
        # title-generation model must all be the single pinned MODEL.
        env = smoke.build_child_env('/tmp/agent-home', 'k')
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['model'], MODEL)
        self.assertEqual(config['small_model'], MODEL)
        self.assertEqual(config['enabled_providers'], ['opencode'])
        command = smoke.build_command('/opt/opencode/bin/opencode', '/work')
        self.assertEqual(command[command.index('--model') + 1], MODEL)

    def test_config_content_never_contains_credential_or_endpoints(self):
        env = smoke.build_child_env('/tmp/agent-home', 'secret-key')
        self.assertNotIn('apiKey', env['OPENCODE_CONFIG_CONTENT'])
        self.assertNotIn('baseURL', env['OPENCODE_CONFIG_CONTENT'])
        # No provider options block (no baseURL/apiKey overrides): the JSON key
        # "provider" itself must not appear — "enabled_providers" is distinct.
        self.assertNotIn('"provider"', env['OPENCODE_CONFIG_CONTENT'])
        self.assertNotIn('secret-key', env['OPENCODE_CONFIG_CONTENT'])

    def test_empty_agent_home_is_rejected(self):
        with self.assertRaises(ValueError):
            smoke.build_child_env('', 'k')


# ---------------------------------------------------------------------------
# Transcript evaluation against real OpenCode 1.19.3 fixtures
# ---------------------------------------------------------------------------

class FixtureEvaluationTests(unittest.TestCase):
    def test_success_text_fixture_is_ok(self):
        meta = fixture_meta('success_text')
        assert_outcome(self, fixture_lines('success_text'), meta['exit_code'],
                       meta['timed_out'], 'ok', '')

    def test_unauthorized_401_reports_auth_error(self):
        meta = fixture_meta('fail_unauthorized_401')
        assert_outcome(self, fixture_lines('fail_unauthorized_401'),
                       meta['exit_code'], meta['timed_out'],
                       'failed', 'provider_error', 'auth_error', 401)

    def test_provider_500_reports_upstream_error(self):
        meta = fixture_meta('fail_provider_500')
        assert_outcome(self, fixture_lines('fail_provider_500'),
                       meta['exit_code'], meta['timed_out'],
                       'failed', 'provider_error', 'upstream_error', 500)

    def test_truncated_length_is_not_a_stop_termination(self):
        meta = fixture_meta('fail_truncated_length')
        assert_outcome(self, fixture_lines('fail_truncated_length'),
                       meta['exit_code'], meta['timed_out'],
                       'failed', 'incomplete_generation:length')

    def test_tool_use_is_rejected_even_when_exit_is_zero(self):
        meta = fixture_meta('success_edit')
        assert_outcome(self, fixture_lines('success_edit'),
                       meta['exit_code'], meta['timed_out'],
                       'failed', 'unexpected_tool_use')

    def test_tool_calls_termination_is_rejected_before_the_trailing_error(self):
        # The fixture contains a tool_use event before its trailing error;
        # the tool rejection wins deterministically.
        meta = fixture_meta('fail_ends_on_tool_calls')
        assert_outcome(self, fixture_lines('fail_ends_on_tool_calls'),
                       meta['exit_code'], meta['timed_out'],
                       'failed', 'unexpected_tool_use')

    def test_timeouts_beat_even_complete_transcripts(self):
        for name in ('fail_stream_cut', 'fail_timeout_hang'):
            meta = fixture_meta(name)
            assert_outcome(self, fixture_lines(name), meta['exit_code'],
                           meta['timed_out'], 'failed', 'timeout')


class GatewayStatusPrecedenceTests(unittest.TestCase):
    def test_provider_error_class_wins_over_nonzero_exit(self):
        # A production-relevant gateway 403 must be recorded as forbidden,
        # not dwarfed into nonzero_exit:1.
        line = ('{"type":"error","error":{"name":"APIError","data":'
                '{"message":"x","statusCode":403,"isRetryable":false}}}')
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

    def test_blank_lines_do_not_create_events(self):
        # Where the fixture has blank padding, only real events count.
        self.assertEqual(smoke.evaluate([], 0, False), ('failed', 'no_events',
                                                        None, None))

    def test_missing_termination_fails(self):
        line = ('{"type":"step_start","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-start"}}')
        self.assertEqual(smoke.evaluate([line], 0, False)[1],
                         'no_supported_termination')

    def test_malformed_step_finish_fails(self):
        line = ('{"type":"step_finish","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-finish"}}')
        self.assertEqual(smoke.evaluate([line], 0, False)[1], 'malformed_event')

    def test_error_without_status_code_is_unknown_provider_error(self):
        line = '{"type":"error","error":{"name":"APIError","data":{"message":"x"}}}'
        outcome = smoke.evaluate([line], 1, False)
        self.assertEqual(outcome[:3], ('failed', 'provider_error',
                                       'unknown_provider_error'))
        self.assertIsNone(outcome[3])

    def test_nonzero_exit_without_other_symptoms_fails(self):
        line = ('{"type":"step_finish","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"step-finish","reason":"stop"}}')
        self.assertEqual(smoke.evaluate([line], 3, False)[1], 'nonzero_exit:3')

    def test_malicious_read_tool_use_is_rejected(self):
        # Defense layer after config denial: even a synthetic transcript that
        # contains a completed filesystem-read tool call cannot count as ok.
        line = ('{"type":"tool_use","timestamp":1,"sessionID":"ses_ea5c01",'
                '"part":{"type":"tool","tool":"read","callID":"c1",'
                '"state":{"status":"completed"}}}')
        self.assertEqual(smoke.evaluate([line], 0, False),
                         ('failed', 'unexpected_tool_use', None, None))

    def test_reason_escaper_drops_everything_outside_the_vocabulary(self):
        self.assertEqual(smoke._esc_reason('Length cap'), 'length-cap')
        self.assertEqual(smoke._esc_reason('stop; exit\nbad'), 'stop-exit-bad')
        self.assertEqual(smoke._esc_reason(''), '')


# Captured from the real lockfile-pinned runtime: `opencode --version` on the
# registry-published windows-x64 tarball of opencode-ai 1.19.3 prints exactly
# this (bare semver, no banner, no leading 'v'). The linux-x64 build matches:
# the workflow install step asserts bare equality against '1.19.3' and passed
# on the CI runner.
REAL_PINNED_VERSION_OUTPUT = '1.19.3\n'


class VersionParsingTests(unittest.TestCase):
    def test_accepts_real_pinned_binary_output(self):
        self.assertEqual(smoke.parse_version(REAL_PINNED_VERSION_OUTPUT),
                         '1.19.3')

    def test_accepts_bare_prefixed_and_banner_formats(self):
        self.assertEqual(smoke.parse_version('opencode v1.19.3'), '1.19.3')
        self.assertEqual(smoke.parse_version('v1.19.3'), '1.19.3')
        self.assertEqual(smoke.parse_version('opencode 1.19.3'), '1.19.3')
        self.assertEqual(smoke.parse_version('  1.19.3  \n'), '1.19.3')

    def test_ignores_blank_lines_around_the_version(self):
        self.assertEqual(smoke.parse_version('\n1.19.3\n\n'), '1.19.3')

    def test_rejects_unmatchable_or_ambiguous_output(self):
        self.assertIsNone(smoke.parse_version('nothing here'))
        self.assertIsNone(smoke.parse_version('v1.19'))            # 2 parts
        self.assertIsNone(smoke.parse_version('1.19.3 extra text'))
        self.assertIsNone(smoke.parse_version('build-1.19.3'))
        self.assertIsNone(smoke.parse_version('1.19.3\n1.19.9'))   # conflicting
        self.assertIsNone(smoke.parse_version(''))                 # empty
        self.assertIsNone(smoke.parse_version('1.19.3.0'))         # 4 parts

    def test_expected_version_matches_every_repo_pin(self):
        package = json.loads(
            (ROOT / '.github/opencode/runtime/package.json').read_text())
        self.assertEqual(smoke.EXPECTED_OPENCODE_VERSION,
                         package['dependencies']['opencode-ai'])
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
            opencode_version='1.19.3', status='failed', reason='provider_error',
            error_class='forbidden', error_status_code=403, exit_code=1,
            timed_out=False, event_count=1, stderr_lines=1, elapsed_ms=123,
        )

    def test_record_field_list_is_exactly_the_closed_set(self):
        record = self.write_record()
        self.assertEqual(set(record), smoke.RESULT_KEYS)

    def test_persisted_record_matches_the_returned_record(self):
        record = self.write_record()
        with open(os.path.join(self.tmp, 'result.json'), encoding='utf-8') as h:
            self.assertEqual(json.load(h), record)

    def test_gateway_403_evidence_carries_only_safe_classes(self):
        # Reproduce the production-relevant failure: record everything the
        # sanitizer allows around a gateway 403, then prove the sensitive
        # channel data never made it into the record.
        line = ('{"type":"error","error":{"name":"APIError","data":'
                '{"message":"Upstream request failed: denied","statusCode":403,'
                '"isRetryable":false,"responseHeaders":{"cf-ray":"abc"},'
                '"responseBody":"{\\"message\\": \\"denied\\"}",'
                '"metadata":{"url":"https://opencode.ai/x"}}}}')
        status, reason, error_class, error_status_code = smoke.evaluate(
            [line], 1, False)
        record = smoke._write_result(
            os.path.join(self.tmp, 'r.json'),
            opencode_version='1.19.3', status=status, reason=reason,
            error_class=error_class, error_status_code=error_status_code,
            exit_code=1, timed_out=False, event_count=1, stderr_lines=0,
            elapsed_ms=7)
        text = json.dumps(record)
        self.assertEqual((record['error_class'], record['error_status_code']),
                         ('forbidden', 403))
        for sensitive in ('denied', 'responseBody', 'responseHeaders',
                          'cf-ray', 'opencode.ai', 'statusCode',
                          'Upstream request'):
            self.assertNotIn(sensitive, text)

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
            'elapsed_ms': 1000,
        }
        line = smoke._summary(record)
        self.assertIn('forbidden', line)
        self.assertIn('elapsed_ms=1000', line)
        for sensitive in ('scripted', 'message', 'http://', 'Bearer'):
            self.assertNotIn(sensitive, line)


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
        self.assertEqual(set(self.workflow.get('env', {})),
                         {'OPENCODE_VERSION'})

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
        # B1: the environment binding is enforced by the job-level key, not by
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

    def test_install_step_runs_without_the_key(self):
        installs = [s for s in self.job['steps'] if 'npm ci' in s.get('run', '')]
        self.assertEqual(len(installs), 1)
        self.assertNotIn('OPENCODE_API_KEY', json.dumps(installs[0].get('env', '')))

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
        self.assertEqual(upload['with']['path'],
                         '${{ runner.temp }}/smoke-result.json')
        self.assertEqual(upload['with']['name'], 'opencode-native-smoke')

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

    def test_isolation_directories_live_under_runner_temp(self):
        runner = [s for s in self.job['steps']
                  if s['name'].startswith('Run native CLI smoke test')][0]
        run = runner['run']
        for path in ('${RUNNER_TEMP}/smoke-result.json',
                     '${RUNNER_TEMP}/smoke-home',
                     '${RUNNER_TEMP}/smoke-work'):
            self.assertIn(path, run)
        self.assertNotIn('${{ ', run)

    def test_exit_nonzero_contract_is_explicit(self):
        runner = [s for s in self.job['steps']
                  if s['name'].startswith('Run native CLI smoke test')][0]
        self.assertIn('set -euo pipefail', runner['run'])


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

@unittest.skipIf(os.name == 'nt', 'stub binaries are POSIX shell scripts')
class MainEndToEndTests(unittest.TestCase):
    """Run main() as a subprocess against a stub binary emitting real fixture
    transcripts, then assert exit code, artifact fields and log hygiene.
    OPENCODE_API_KEY is a synthetic string; no gateway is contacted."""

    KEY = 'smoke-test-synthetic-key-not-real'
    BANNED_LOG_SUBSTRINGS = ('Upstream request', 'cf-ray', 'statusCode:',
                             'responseBody', 'Bearer', KEY)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def stub_binary(self, media: str, exit_code: int, banner: str = '1.19.3'
                    ) -> Path:
        stub_dir = Path(self.tmp) / 'stubdir'
        stub_dir.mkdir(exist_ok=True)
        # The --version output uses the REAL pinned-binary format: bare semver
        # (see VersionParsingTests / REAL_PINNED_VERSION_OUTPUT evidence).
        # The transcript is emitted byte-for-byte from a sibling file; a
        # heredoc would append a trailing blank line and inflate event_count.
        (stub_dir / 'media.jsonl').write_text(media, encoding='utf-8')
        stub = stub_dir / 'opencode'
        stub.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "--version" ]; then\n'
            f'  echo "{banner}"\n'
            '  exit 0\n'
            'fi\n'
            'cat "$(dirname "$0")/media.jsonl"\n'
            f'exit {exit_code}\n',
            encoding='utf-8')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)
        return stub

    def run_smoke(self, stub: Path, *, key: str | None = KEY,
                  binary: str | None = None, timeout_min: str = '10',
                  cwd: str | None = None):
        environment = dict(os.environ)
        environment.pop('OPENCODE_API_KEY', None)  # never use a real key
        if key is not None:
            environment['OPENCODE_API_KEY'] = key
        result = os.path.join(self.tmp, 'result.json')
        with tempfile.TemporaryDirectory() as work_root:
            agent_home = os.path.join(work_root, 'home')
            workdir = os.path.join(work_root, 'work')
            invocation_binary = binary or stub
            completed = subprocess.run(
                [sys.executable,
                 str(ROOT / '.github/scripts/opencode_native_smoke.py'),
                 result, str(invocation_binary), agent_home, workdir,
                 timeout_min],
                env=environment, capture_output=True, text=True, timeout=300,
                cwd=cwd)
        record = None
        if os.path.exists(result):
            with open(result, encoding='utf-8') as handle:
                record = json.load(handle)
        return completed, record

    def assert_log_hygiene(self, completed):
        for banned in self.BANNED_LOG_SUBSTRINGS:
            self.assertNotIn(banned, completed.stdout + completed.stderr)

    def test_success_fixture_completes_with_zero_exit(self):
        media = (FIXTURES / 'success_text.jsonl').read_text(encoding='utf-8')
        meta = fixture_meta('success_text')
        stub = self.stub_binary(media, meta['exit_code'])
        completed, record = self.run_smoke(stub)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['model'], MODEL)
        self.assertEqual(record['opencode_version'], '1.19.3')
        self.assertGreater(record['elapsed_ms'], 0)
        self.assertEqual(record['event_count'], meta['original_line_count'])

    def test_prefixed_version_banner_is_tolerated(self):
        # Older alt builds print 'opencode v<semver>'; the matcher must accept
        # it while the workflow keeps asserting the real bare format.
        stub = self.stub_binary(
            (FIXTURES / 'success_text.jsonl').read_text(encoding='utf-8'), 0,
            banner='opencode v1.19.3')
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['opencode_version'], '1.19.3')

    def test_relative_binary_path_with_different_cwd(self):
        # B4 regression: the script must canonicalize the pinned binary to an
        # absolute path before changing cwd to the smoke workdir — a relative
        # binary path containing '/' would otherwise resolve from the wrong
        # directory (POSIX ENOENT).
        media = (FIXTURES / 'success_text.jsonl').read_text(encoding='utf-8')
        stub = self.stub_binary(media, 0)
        relative_binary = os.path.relpath(stub, start=self.tmp)
        self.assertIn(os.sep, relative_binary)  # relative path with a slash
        completed, record = self.run_smoke(stub, binary=relative_binary,
                                           cwd=self.tmp)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['opencode_version'], '1.19.3')

    def test_gateway_401_fails_with_classified_artifact(self):
        media = (FIXTURES / 'fail_unauthorized_401.jsonl').read_text(
            encoding='utf-8')
        meta = fixture_meta('fail_unauthorized_401')
        stub = self.stub_binary(media, meta['exit_code'])
        completed, record = self.run_smoke(stub)
        self.assert_log_hygiene(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['reason'], 'provider_error')
        self.assertEqual(record['error_class'], 'auth_error')
        self.assertEqual(record['error_status_code'], 401)
        self.assertEqual(record['exit_code'], meta['exit_code'])

    def test_gateway_403_is_reported_as_forbidden(self):
        media = ('{"type":"error","error":{"name":"APIError","data":'
                 '{"message":"Upstream request failed: denied","statusCode":403,'
                 '"responseHeaders":{"cf-ray":"abc"},"responseBody":"denied"}}}')
        stub = self.stub_binary(media, 1)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['error_class'], 'forbidden')
        self.assertEqual(record['error_status_code'], 403)
        self.assertEqual(record['reason'], 'provider_error')
        self.assert_log_hygiene(completed)

    def test_version_mismatch_fails_before_any_inference(self):
        stub = Path(self.tmp) / 'stub-opencode-old'
        stub.write_text('#!/bin/sh\necho "opencode v1.19.9"\n', encoding='utf-8')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR
                   | stat.S_IXGRP | stat.S_IXOTH)
        completed, record = self.run_smoke(stub)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'opencode_version_mismatch')
        self.assertEqual(record['opencode_version'], '1.19.9')
        self.assertIsNone(record['exit_code'])
        self.assertEqual(record['event_count'], 0)

    def test_missing_key_fails_before_any_execution(self):
        stub = self.stub_binary(
            (FIXTURES / 'success_text.jsonl').read_text(encoding='utf-8'), 0)
        completed, record = self.run_smoke(stub, key=None)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(record['reason'], 'missing_api_key')
        self.assertIsNone(record['opencode_version'])
        self.assertEqual(record['event_count'], 0)
        self.assert_log_hygiene(completed)


if __name__ == '__main__':
    unittest.main()
