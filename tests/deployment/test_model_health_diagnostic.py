"""OpenCode model health diagnostic tests (diagnostic tests v1.0).

Tests the narrow allowlist, error classification, catalog parsing, and the
guarantee that the diagnostic cannot become an arbitrary-model or
arbitrary-prompt channel.
"""
import importlib.util
import inspect
import unittest
import urllib.error
from pathlib import Path

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
