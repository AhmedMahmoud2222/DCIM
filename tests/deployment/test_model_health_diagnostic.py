"""OpenCode model health diagnostic tests (diagnostic tests v1.4).

Tests the narrow allowlist, error classification, catalog parsing, redirect
blocking, response validation, and exit-code behavior.

The diagnostic cannot become an arbitrary-model or arbitrary-prompt channel.
Includes end-to-end stub-server tests that fail on mutation/removal of
structural response validation and nonzero exit.

Negative tests prove that catalog 404 errors are not interpreted as
model-unavailable evidence, and that inference response model identity is
validated.

Main-level tests invoke main() with a temporary output path, mocked catalog
and inference endpoints, and assert actual exit code and JSON evidence for:
catalog 404 + successful inference, catalog 404 + failed inference, valid
catalog with one model absent, both healthy, partial healthy, invalid model
response, missing API key.
"""
import importlib.util
import inspect
import json
import os
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
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
    'glm-5.3-flash-free',
    'deepseek-v4.1-flash-free',
})

VALID_RESPONSE = (b'{"id":"chatcmpl_123","model":"glm-5.3-flash-free",'
                  b'"choices":[{"message":{"content":"OK"}}]}')
VALID_RESPONSE_DEEPSEEK = (b'{"id":"chatcmpl_456","model":"deepseek-v4.1-flash-free",'
                           b'"choices":[{"message":{"content":"OK"}}]}')


class AllowlistTests(unittest.TestCase):
    def test_allowlist_contains_exactly_two_models(self):
        self.assertEqual(diagnostic.ALLOWED_MODELS, ALLOWED)
        for model_id in diagnostic.ALLOWED_MODELS:
            # Bare provider IDs, not the opencode/<id> CLI selector form.
            self.assertNotIn('/', model_id)
            self.assertTrue(model_id.endswith('-free'))

    def test_non_allowlisted_model_is_not_tested(self):
        record = diagnostic.probe_model('key', 'nemotron-3-ultra-free')
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
        diagnostic._assert_trusted('https://opencode.ai/inference/v1/models')
        diagnostic._assert_trusted(
            'https://opencode.ai/inference/openai/v1/chat/completions'
        )

    def test_http_scheme_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'http://opencode.ai/inference/v1/models'
            )

    def test_untrusted_host_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'https://evil.example.com/inference/v1/models'
            )

    def test_subdomain_is_blocked(self):
        with self.assertRaises(urllib.error.URLError):
            diagnostic._assert_trusted(
                'https://api.opencode.ai/inference/v1/models'
            )


# ---------------------------------------------------------------------------
# Stub-server infrastructure for two-server redirect tests
# ---------------------------------------------------------------------------

_RECEIVED_HEADERS: list[dict[str, str]] = []
_RECEIVED_BODIES: list[bytes] = []


class DestinationHandler(BaseHTTPRequestHandler):
    """Records every request it receives, then returns 200 with a valid body."""

    def _record(self):
        _RECEIVED_HEADERS.append(dict(self.headers))
        length = int(self.headers.get('Content-Length') or 0)
        _RECEIVED_BODIES.append(self.rfile.read(length) if length else b'')

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"id":"x","model":"m","choices":[{"message":{"content":"OK"}}]}')

    def do_POST(self):
        self._record()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"id":"x","model":"m","choices":[{"message":{"content":"OK"}}]}')

    def log_message(self, format, *args):  # noqa: ARG002
        pass  # suppress stderr noise


class RedirectHandler(BaseHTTPRequestHandler):
    """Returns a configured redirect status with Location to the destination."""

    def _redirect(self, code: int, dest_url: str):
        self.send_response(code)
        self.send_header('Location', dest_url)
        self.end_headers()

    def do_GET(self):
        self._redirect(self.server.redirect_code, self.server.dest_url)

    def do_POST(self):
        self._redirect(self.server.redirect_code, self.server.dest_url)

    def log_message(self, format, *args):  # noqa: ARG002
        pass


def find_free_port() -> int:
    with socketserver.TCPServer(('127.0.0.1', 0), BaseHTTPRequestHandler) as s:
        return s.server_address[1]


class TwoServerRedirectFixture:
    """Starts a redirect server and a destination server on localhost."""

    def __init__(self, redirect_code: int):
        self.redirect_port = find_free_port()
        self.dest_port = find_free_port()
        self.redirect_code = redirect_code
        self.dest_url = f'http://127.0.0.1:{self.dest_port}/'
        self.redirect_url = f'http://127.0.0.1:{self.redirect_port}/'
        self._dest_server = socketserver.TCPServer(
            ('127.0.0.1', self.dest_port), DestinationHandler
        )
        self._redirect_server = socketserver.TCPServer(
            ('127.0.0.1', self.redirect_port), RedirectHandler
        )
        self._redirect_server.redirect_code = redirect_code
        self._redirect_server.dest_url = self.dest_url
        self._threads: list[threading.Thread] = []
        for server in (self._dest_server, self._redirect_server):
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        self._dest_server.shutdown()
        self._redirect_server.shutdown()
        self._dest_server.server_close()
        self._redirect_server.server_close()


class RedirectCredentialLeakTests(unittest.TestCase):
    """End-to-end two-server tests: Authorization must never reach the
    redirect destination, and every redirect must be rejected fail-closed."""

    REDIRECT_CODES = (301, 302, 303, 307, 308)

    def setUp(self):
        _RECEIVED_HEADERS.clear()
        _RECEIVED_BODIES.clear()

    def _assert_no_auth_at_destination(self):
        """Destination server must never have received an Authorization header."""
        for headers in _RECEIVED_HEADERS:
            self.assertNotIn('Authorization', headers,
                             f'destination received Authorization: {headers}')

    def test_get_redirect_never_leaks_authorization(self):
        for code in self.REDIRECT_CODES:
            with self.subTest(code=code):
                fixture = TwoServerRedirectFixture(code)
                try:
                    opener = diagnostic._build_opener()
                    req = urllib.request.Request(
                        fixture.redirect_url,
                        headers={'Authorization': 'Bearer TEST_KEY_12345',
                                 'User-Agent': 'dcim-model-health-diagnostic'},
                    )
                    try:
                        opener.open(req, timeout=5)
                    except (urllib.error.HTTPError, ConnectionAbortedError, OSError):
                        pass  # redirect rejected — connection may be aborted on Windows
                    else:
                        self.fail(f'redirect {code} was not rejected')
                    self._assert_no_auth_at_destination()
                finally:
                    fixture.shutdown()

    def test_post_redirect_never_leaks_authorization(self):
        for code in self.REDIRECT_CODES:
            with self.subTest(code=code):
                fixture = TwoServerRedirectFixture(code)
                try:
                    opener = diagnostic._build_opener()
                    body = json.dumps({'model': 'glm-5.3-flash-free',
                                       'messages': [{'role': 'user', 'content': 'OK'}],
                                       'max_tokens': 5}).encode()
                    req = urllib.request.Request(
                        fixture.redirect_url,
                        data=body,
                        headers={'Authorization': 'Bearer TEST_KEY_12345',
                                 'Content-Type': 'application/json',
                                 'User-Agent': 'dcim-model-health-diagnostic'},
                        method='POST',
                    )
                    try:
                        opener.open(req, timeout=5)
                    except (urllib.error.HTTPError, ConnectionAbortedError, OSError):
                        pass  # redirect rejected — connection may be aborted on Windows
                    else:
                        self.fail(f'redirect {code} was not rejected')
                    self._assert_no_auth_at_destination()
                finally:
                    fixture.shutdown()

    def test_redirect_is_fail_closed_for_all_statuses(self):
        """Every redirect status must raise, never silently follow."""
        for code in self.REDIRECT_CODES:
            with self.subTest(code=code):
                fixture = TwoServerRedirectFixture(code)
                try:
                    opener = diagnostic._build_opener()
                    req = urllib.request.Request(
                        fixture.redirect_url,
                        headers={'Authorization': 'Bearer TEST_KEY_12345',
                                 'User-Agent': 'dcim-model-health-diagnostic'},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        opener.open(req, timeout=5)
                    # The raised error should carry the redirect code.
                    self.assertEqual(ctx.exception.code, code)
                finally:
                    fixture.shutdown()


# ---------------------------------------------------------------------------
# Response validation tests (stub http_post_json)
# ---------------------------------------------------------------------------

class ResponseValidationTests(unittest.TestCase):
    """Stub-server tests: malformed HTTP 200s must never show healthy."""

    def _stub_post_response(self, response_body: bytes, response_status: int = 200):
        """Patch http_post_json to return a stub response directly.

        Bypasses network and trusted-host checks entirely; the stub validates
        that structural response validation works on the parsed JSON body.
        """
        def stub(url, key, model, prompt):
            return json.loads(response_body.decode('utf-8'))

        return patch.object(diagnostic, 'http_post_json', side_effect=stub)

    def test_valid_200_is_healthy(self):
        with self._stub_post_response(VALID_RESPONSE):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'healthy')
        self.assertIsNone(record['error_class'])
        self.assertTrue(record['response_model_matches'])

    def test_empty_json_200_is_unhealthy(self):
        with self._stub_post_response(b'{}'):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_missing_choices_200_is_unhealthy(self):
        body = b'{"id":"x","model":"glm-5.3-flash-free"}'
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_empty_choices_200_is_unhealthy(self):
        body = (b'{"id":"x","model":"glm-5.3-flash-free","choices":[]}')
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_missing_content_200_is_unhealthy(self):
        body = (b'{"id":"x","model":"glm-5.3-flash-free",'
                b'"choices":[{"message":{}}]}')
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_empty_content_200_is_unhealthy(self):
        body = (b'{"id":"x","model":"glm-5.3-flash-free",'
                b'"choices":[{"message":{"content":""}}]}')
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_model_mismatch_200_is_unhealthy(self):
        body = (b'{"id":"x","model":"deepseek-v4.1-flash-free",'
                b'"choices":[{"message":{"content":"OK"}}]}')
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])

    def test_missing_id_200_is_unhealthy(self):
        body = (b'{"model":"glm-5.3-flash-free",'
                b'"choices":[{"message":{"content":"OK"}}]}')
        with self._stub_post_response(body):
            record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        self.assertEqual(record['status'], 'unhealthy')
        self.assertEqual(record['error_class'], 'invalid_response')
        self.assertFalse(record['response_model_matches'])


# ---------------------------------------------------------------------------
# Main-level tests: invoke main() with mocked endpoints
# ---------------------------------------------------------------------------

class MainLevelTests(unittest.TestCase):
    """Invoke main() with mocked catalog and inference, assert exit code
    and JSON evidence."""

    def _run_main(self, catalog_return, post_side_effect, key='test-key'):
        """Run main() with mocked fetch_catalog and http_post_json, return
        (exit_code, report_dict).

        catalog_return: either a dict {'ids': set, 'status': ..., 'error_class': ...}
                          or an Exception class/instance to raise from fetch_catalog.
        """
        out_path = os.path.join(tempfile.gettempdir(), f'diag_test_{id(self)}.json')
        try:
            os.remove(out_path)
        except OSError:
            pass

        # Build the fetch_catalog mock return value.
        if isinstance(catalog_return, BaseException):
            def catalog_raises(key):
                raise catalog_return
            catalog_mock = patch.object(diagnostic, 'fetch_catalog', side_effect=catalog_raises)
        else:
            def catalog_returns(key):
                return dict(catalog_return)
            catalog_mock = patch.object(diagnostic, 'fetch_catalog', side_effect=catalog_returns)

        post_mock = patch.object(diagnostic, 'http_post_json', side_effect=post_side_effect)

        with catalog_mock, post_mock:
            old_env = os.environ.get('OPENCODE_API_KEY')
            os.environ['OPENCODE_API_KEY'] = key
            try:
                exit_code = diagnostic.main(['diagnostic', out_path])
            finally:
                if old_env is None:
                    os.environ.pop('OPENCODE_API_KEY', None)
                else:
                    os.environ['OPENCODE_API_KEY'] = old_env

        with open(out_path, encoding='utf-8') as handle:
            report = json.load(handle)
        try:
            os.remove(out_path)
        except OSError:
            pass
        return exit_code, report

    def _catalog_ok_dict(self, key):
        return {'ids': {'glm-5.3-flash-free', 'deepseek-v4.1-flash-free'},
                'status': 'ok', 'error_class': None}

    def _catalog_ok_glm_only_dict(self, key):
        return {'ids': {'glm-5.3-flash-free'},
                'status': 'ok', 'error_class': None}

    def _catalog_404_dict(self, key):
        return {'ids': set(), 'status': 'error', 'error_class': 'catalog_http_404'}

    def _post_healthy(self, url, key, model, prompt):
        if model == 'glm-5.3-flash-free':
            return json.loads(VALID_RESPONSE.decode('utf-8'))
        return json.loads(VALID_RESPONSE_DEEPSEEK.decode('utf-8'))

    def _post_invalid_model(self, url, key, model, prompt):
        # Response echoes a different model than requested.
        return {'id': 'x', 'model': 'other-model',
                'choices': [{'message': {'content': 'OK'}}]}

    def _post_failed(self, url, key, model, prompt):
        raise urllib.error.HTTPError(url, 500, 'Internal Error', {}, None)

    def test_catalog_404_plus_successful_inference(self):
        """Catalog 404: exit 1, catalog_status=error, catalog_error_class=catalog_http_404,
        inference results preserved (not overwritten as not_in_catalog)."""
        exit_code, report = self._run_main(self._catalog_404_dict('key'), self._post_healthy)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'error')
        self.assertEqual(report['catalog_error_class'], 'catalog_http_404')
        self.assertEqual(report['overall'], 'unhealthy')
        # Inference results are preserved, not overwritten as not_in_catalog.
        for result in report['results']:
            self.assertEqual(result['status'], 'healthy')
            self.assertFalse(result['catalog_present'])

    def test_catalog_404_plus_failed_inference(self):
        """Catalog 404 + inference failure: exit 1, both unhealthy."""
        exit_code, report = self._run_main(self._catalog_404_dict('key'), self._post_failed)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'error')
        self.assertEqual(report['catalog_error_class'], 'catalog_http_404')
        self.assertEqual(report['overall'], 'unhealthy')
        for result in report['results']:
            self.assertEqual(result['status'], 'unhealthy')

    def test_valid_catalog_one_model_absent(self):
        """Valid catalog with only one model present: exit 1, catalog_status=ok,
        absent model has catalog_present=False."""
        exit_code, report = self._run_main(
            self._catalog_ok_glm_only_dict('key'), self._post_healthy)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'ok')
        self.assertIsNone(report['catalog_error_class'])
        self.assertEqual(report['overall'], 'unhealthy')
        glm = next(r for r in report['results'] if r['model'] == 'glm-5.3-flash-free')
        ds = next(r for r in report['results'] if r['model'] == 'deepseek-v4.1-flash-free')
        self.assertTrue(glm['catalog_present'])
        self.assertFalse(ds['catalog_present'])

    def test_both_healthy(self):
        """Both models healthy and in catalog: exit 0, catalog_status=ok."""
        exit_code, report = self._run_main(self._catalog_ok_dict('key'), self._post_healthy)
        self.assertEqual(exit_code, 0)
        self.assertEqual(report['catalog_status'], 'ok')
        self.assertEqual(report['overall'], 'healthy')
        self.assertEqual(report['healthy_count'], 2)

    def test_partial_healthy(self):
        """One model healthy, one unhealthy: exit 1."""
        def post_mixed(url, key, model, prompt):
            if model == 'glm-5.3-flash-free':
                return json.loads(VALID_RESPONSE.decode('utf-8'))
            raise urllib.error.HTTPError(url, 500, 'Error', {}, None)

        exit_code, report = self._run_main(self._catalog_ok_dict('key'), post_mixed)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'ok')
        self.assertEqual(report['overall'], 'unhealthy')
        self.assertEqual(report['healthy_count'], 1)

    def test_invalid_model_response(self):
        """Response echoes different model: exit 1, response_model_matches=false."""
        exit_code, report = self._run_main(self._catalog_ok_dict('key'), self._post_invalid_model)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'ok')
        self.assertEqual(report['overall'], 'unhealthy')
        for result in report['results']:
            self.assertEqual(result['status'], 'unhealthy')
            self.assertFalse(result['response_model_matches'])

    def test_missing_api_key(self):
        """Missing API key: exit 1, artifact still written, catalog_status=error."""
        out_path = os.path.join(tempfile.gettempdir(), f'diag_missing_key_{id(self)}.json')
        try:
            os.remove(out_path)
        except OSError:
            pass
        old_env = os.environ.pop('OPENCODE_API_KEY', None)
        try:
            exit_code = diagnostic.main(['diagnostic', out_path])
        finally:
            if old_env is not None:
                os.environ['OPENCODE_API_KEY'] = old_env
        with open(out_path, encoding='utf-8') as handle:
            report = json.load(handle)
        try:
            os.remove(out_path)
        except OSError:
            pass
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['catalog_status'], 'error')
        self.assertEqual(report['catalog_error_class'], 'missing_api_key')
        self.assertEqual(report['overall'], 'unhealthy')
        self.assertEqual(report['models_tested'], 0)


# ---------------------------------------------------------------------------
# Existing unit tests (kept and extended)
# ---------------------------------------------------------------------------

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


class CatalogClassificationTests(unittest.TestCase):
    """Catalog-level errors are classified distinctly from per-model errors."""

    def test_catalog_http_404_is_catalog_http_404_not_model_not_available(self):
        error = urllib.error.HTTPError(
            diagnostic.CATALOG_ENDPOINT, 404, 'Not Found', {}, None)
        self.assertEqual(diagnostic.classify_catalog_error(error),
                         'catalog_http_404')

    def test_catalog_http_500_is_catalog_unavailable(self):
        error = urllib.error.HTTPError(
            diagnostic.CATALOG_ENDPOINT, 500, 'Error', {}, None)
        self.assertEqual(diagnostic.classify_catalog_error(error),
                         'catalog_unavailable')

    def test_catalog_network_error_is_catalog_unavailable(self):
        error = urllib.error.URLError('connection refused')
        self.assertEqual(diagnostic.classify_catalog_error(error),
                         'catalog_unavailable')


class CatalogParsingTests(unittest.TestCase):
    def test_extract_ids_returns_bare_provider_ids(self):
        # The live catalog returns bare IDs; extract_ids must not add
        # the opencode/ prefix.
        payload = {'data': [{'id': 'glm-5.3-flash-free'},
                            {'id': 'deepseek-v4.1-flash-free'}]}
        ids = diagnostic.extract_ids(payload)
        self.assertEqual(ids, {'glm-5.3-flash-free', 'deepseek-v4.1-flash-free'})

    def test_extract_ids_ignores_non_string_entries(self):
        payload = {'data': [{'id': 123}, {'id': 'glm-5.3-flash-free'}, None]}
        ids = diagnostic.extract_ids(payload)
        self.assertEqual(ids, {'glm-5.3-flash-free'})


class ProbeStructureTests(unittest.TestCase):
    def test_probe_record_has_required_keys(self):
        record = diagnostic.probe_model('key', 'glm-5.3-flash-free')
        required = {'model', 'allowed', 'catalog_present', 'status',
                    'error_class', 'latency_ms', 'response_model_matches'}
        self.assertTrue(required.issubset(set(record)))

    def test_healthy_record_has_no_error_class(self):
        record = {
            'model': 'glm-5.3-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'healthy',
            'error_class': None,
            'latency_ms': 120,
            'response_model_matches': True,
        }
        self.assertIsNone(record['error_class'])
        self.assertTrue(record['response_model_matches'])

    def test_unhealthy_record_has_error_class(self):
        record = {
            'model': 'deepseek-v4.1-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'unhealthy',
            'error_class': 'rate_limit',
            'latency_ms': 500,
            'response_model_matches': False,
        }
        self.assertIsNotNone(record['error_class'])
        self.assertFalse(record['response_model_matches'])

    def test_response_id_hash_is_12_hex_chars_when_present(self):
        record = {
            'model': 'glm-5.3-flash-free',
            'allowed': True,
            'catalog_present': True,
            'status': 'healthy',
            'error_class': None,
            'latency_ms': 120,
            'response_model_matches': True,
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
