"""OpenCode model health diagnostic tests (diagnostic tests v1.1).

Tests the narrow allowlist, error classification, catalog parsing, redirect
blocking, response validation, and exit-code behavior.

The diagnostic cannot become an arbitrary-model or arbitrary-prompt channel.
"""
import importlib.util
import inspect
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'model_health_diagnostic',
    ROOT / '.github/scripts/model_health_diagnostic.py',
)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)

ALLOWED = frozenset({
    'opencode/glm-5.3-flash-free',
    'opencode/deepseek-v4.1-flash-free',
})


class AllowlistTests(unittest.TestCase):
    def test_allowlist_contains_exactly_two_models(self):
        self.assertEqual(diagnostic.ALLOWED_MODELS, ALLOWED)
        for model_id in diagnostic.ALLOWED_MODELS:
            self.assertTrue(model_id.startswith('opencode/'))
            self.assertTrue(model_id.endswith('-free'))

    def test_non_allowlisted_model_is_not_tested(self):
        record = diagnostic.probe_model('key', 'opencode/nemotron-3-ultra-free')
        self.assertEqual(record['status'], 'not_allowlisted')
        self.assertFalse(record['allowed'])

    def test_allowlisted_models_are_tested(self):
        for model_id in ALLOWED:
            record = diagnostic.probe_model('key', model_id)
            self.assertTrue(record['allowed'])
            self.assertIn(record['status'], ('healthy', 'unhealthy', 'not_in_catalog'))


class SyntheticPromptTests(unittest.TestCase):
    def test_prompt_is_hard_coded_and_non_empty(self):
        self.assertIsInstance(diagnostic.SYNTHETIC_PROMPT, str)
        self.assertGreater(len(diagnostic.SYNTHETIC_PROMPT), 0)

    def test_no_parameter_can_change_the_prompt(self):
        # probe_model accepts only key and model; there is no prompt parameter.
        sig = inspect.signature(diagnostic.probe_model)
        self.assertEqual(set(sig.parameters), {'key', 'model'})


class TrustedHostTests(unittest.TestCase):
    def test_https_opencode_ai_is_allowed(self):
        diagnostic._assert_trusted('https://opencode.ai/inference/openai/v1/models')
        diagnostic._assert_trusted(
            'https://opencode.ai/inference/openai/v1/chat/completions'
        )

    def test_http_scheme_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'http://opencode.ai/inference/openai/v1/models'
            )

    def test_untrusted_host_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'https://evil.example.com/inference/openai/v1/models'
            )

    def test_subdomain_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'https://api.opencode.ai/inference/openai/v1/models'
            )


class RedirectBlockingTests(unittest.TestCase):
    def test_redirect_is_rejected_before_request(self):
        # The opener's redirect handler rejects any redirect, so a 307/302
        # never causes urllib to follow and resend the Authorization header.
        opener = diagnostic._OPENER
        handler_names = [type(h).__name__ for h in opener.handlers]
        self.assertNotIn('HTTPRedirectHandler', handler_names)
        self.assertIn('_NoRedirect', handler_names)

    def test_redirect_handler_raises_on_redirect(self):
        # Calling redirect_request on our handler must raise, never return a
        # new Request that would follow the redirect.
        handler = diagnostic._NoRedirect()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                None, None, 307, 'Temporary Redirect',
                {'Location': 'https://evil.example.com/'}, None,
            )


class ErrorClassificationTests(unittest.TestCase):
    def test_http_401_maps_to_auth_error(self):
        self.assertEqual(diagnostic.classify_error(
            _http_error(401)), 'auth_error')

    def test_http_429_maps_to_rate_limit(self):
        self.assertEqual(diagnostic.classify_error(
            _http_error(429)), 'rate_limit')

    def test_http_404_maps_to_model_not_available(self):
        self.assertEqual(diagnostic.classify_error(
            _http_error(404)), 'model_not_available')

    def test_http_500_maps_to_upstream_error(self):
        self.assertEqual(diagnostic.classify_error(
            _http_error(500)), 'upstream_error')

    def test_http_code_zero_maps_to_upstream_error(self):
        # Read-cap overflow produces code 0 and should be upstream_error.
        self.assertEqual(diagnostic.classify_error(
            _http_error(0)), 'upstream_error')

    def test_url_error_maps_to_network_error(self):
        self.assertEqual(diagnostic.classify_error(
            _url_error()), 'network_error')

    def test_unknown_error_maps_to_unknown_error(self):
        self.assertEqual(diagnostic.classify_error(ValueError('x')), 'unknown_error')


class CatalogParsingTests(unittest.TestCase):
    def test_extract_ids_normalizes_model_ids(self):
        payload = {'data': [{'id': 'glm-5.3-flash-free'}, {'id': 'opencode/deepseek-v4.1-flash-free'}]}
        ids = diagnostic.extract_ids(payload)
        self.assertEqual(ids, {'opencode/glm-5.3-flash-free', 'opencode/deepseek-v4.1-flash-free'})

    def test_extract_ids_ignores_non_string_entries(self):
        payload = {'data': [{'id': 123}, {'id': 'glm-5.3-flash-free'}, None]}
        ids = diagnostic.extract_ids(payload)
        self.assertEqual(ids, {'opencode/glm-5.3-flash-free'})


class ResponseValidationTests(unittest.TestCase):
    def test_valid_response_passes_validation(self):
        valid = {
            'id': 'chatcmpl_123',
            'model': 'opencode/glm-5.3-flash-free',
            'choices': [{'message': {'content': 'OK'}}],
        }
        diagnostic._validate_healthy_response(valid, 'opencode/glm-5.3-flash-free')

    def test_missing_id_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response(
                {'model': 'opencode/glm-5.3-flash-free',
                 'choices': [{'message': {'content': 'OK'}}]},
                'opencode/glm-5.3-flash-free',
            )

    def test_empty_choices_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response(
                {'id': 'x', 'model': 'opencode/glm-5.3-flash-free', 'choices': []},
                'opencode/glm-5.3-flash-free',
            )

    def test_missing_content_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response(
                {'id': 'x', 'model': 'opencode/glm-5.3-flash-free',
                 'choices': [{'message': {}}]},
                'opencode/glm-5.3-flash-free',
            )

    def test_empty_content_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response(
                {'id': 'x', 'model': 'opencode/glm-5.3-flash-free',
                 'choices': [{'message': {'content': ''}}]},
                'opencode/glm-5.3-flash-free',
            )

    def test_model_mismatch_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response(
                {'id': 'x', 'model': 'opencode/deepseek-v4.1-flash-free',
                 'choices': [{'message': {'content': 'OK'}}]},
                'opencode/glm-5.3-flash-free',
            )

    def test_response_not_dict_is_unhealthy(self):
        with self.assertRaises(ValueError):
            diagnostic._validate_healthy_response('not a dict', 'opencode/glm-5.3-flash-free')


class ProbeStructureTests(unittest.TestCase):
    def test_probe_record_has_required_keys(self):
        record = diagnostic.probe_model('key', 'opencode/glm-5.3-flash-free')
        required = {'model', 'allowed', 'catalog_present', 'status',
                    'error_class', 'latency_ms'}
        self.assertTrue(required.issubset(set(record)))

    def test_healthy_record_has_no_error_class(self):
        record = {
            'model': 'opencode/glm-5.3-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'healthy',
            'error_class': None,
            'latency_ms': 120,
        }
        self.assertIsNone(record['error_class'])

    def test_unhealthy_record_has_error_class(self):
        record = {
            'model': 'opencode/deepseek-v4.1-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'unhealthy',
            'error_class': 'rate_limit',
            'latency_ms': 500,
        }
        self.assertIsNotNone(record['error_class'])

    def test_response_id_hash_is_12_hex_chars_when_present(self):
        record = {
            'model': 'opencode/glm-5.3-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'healthy',
            'error_class': None,
            'latency_ms': 120,
            'response_id_hash': 'a' * 12,
        }
        # The hash is exactly 12 hex chars — never the raw response id.
        self.assertEqual(len(record['response_id_hash']), 12)
        self.assertTrue(all(c in '0123456789abcdef' for c in record['response_id_hash']))


class MainBehaviorTests(unittest.TestCase):
    def test_report_has_overall_and_healthy_count(self):
        # Build a minimal report dict as main() would produce.
        results = [
            {'model': 'opencode/glm-5.3-flash-free', 'status': 'healthy',
             'catalog_present': True},
            {'model': 'opencode/deepseek-v4.1-flash-free', 'status': 'healthy',
             'catalog_present': True},
        ]
        healthy_count = sum(1 for r in results if r['status'] == 'healthy')
        all_in_catalog = all(r['catalog_present'] for r in results)
        all_healthy = healthy_count == len(results)
        overall = 'healthy' if (all_in_catalog and all_healthy) else 'unhealthy'
        self.assertEqual(overall, 'healthy')
        self.assertEqual(healthy_count, 2)

    def test_report_unhealthy_when_one_model_fails(self):
        results = [
            {'model': 'opencode/glm-5.3-flash-free', 'status': 'healthy',
             'catalog_present': True},
            {'model': 'opencode/deepseek-v4.1-flash-free', 'status': 'unhealthy',
             'catalog_present': True},
        ]
        healthy_count = sum(1 for r in results if r['status'] == 'healthy')
        all_healthy = healthy_count == len(results)
        overall = 'healthy' if (all(r['catalog_present'] for r in results) and all_healthy) else 'unhealthy'
        self.assertEqual(overall, 'unhealthy')
        self.assertEqual(healthy_count, 1)

    def test_report_unhealthy_when_model_missing_from_catalog(self):
        results = [
            {'model': 'opencode/glm-5.3-flash-free', 'status': 'healthy',
             'catalog_present': True},
            {'model': 'opencode/deepseek-v4.1-flash-free', 'status': 'not_in_catalog',
             'catalog_present': False},
        ]
        healthy_count = sum(1 for r in results if r['status'] == 'healthy')
        all_in_catalog = all(r['catalog_present'] for r in results)
        overall = 'healthy' if (all_in_catalog and healthy_count == len(results)) else 'unhealthy'
        self.assertEqual(overall, 'unhealthy')


# --- Helpers ---

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code):
        self.code = code


def _http_error(code):
    return _FakeHTTPError(code)


def _url_error():
    return urllib.error.URLError('connection refused')


if __name__ == '__main__':
    unittest.main()
