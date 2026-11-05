"""Client-level regression tests for GitHub API transient-failure retries.

Covers 429 rate limit, 502/503 server errors, timeouts, recovery and repeated
interruptions. Definitive failures (401/403/404/422) fail closed without retry.
"""
import email.message
import io
import json
import os
import sys
import time
import urllib.error
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import acceptance_api as a
from acceptance_core import Rejected

TOKEN = 'SYNTHETIC-TOKEN'


def setUpModule():
    a.GitHub._slept = 0.0


def tearDownModule():
    a.GitHub._slept = 0.0   # the budget is process-wide; do not leak it into other test modules


def http_error(code, url='https://api.github.com/repos/o/r/pulls/1'):
    """Build a real urllib HTTPError with optional Retry-After header."""
    headers = email.message.Message()
    if code == 429:
        headers['Retry-After'] = '0'
    return urllib.error.HTTPError(url, code, 'server error', headers, None)


class RecordingAPI(a.GitHub):
    """GitHub client stub; overrides the single-attempt _raw_request."""

    def __init__(self):
        self.policy = {'repository': 'o/r'}
        self.api_root = 'https://api.github.com/repos/o/r'
        self.root = ROOT
        self.calls = []  # (path, method) in order
        self.fail_always = None          # error raised on every _raw_request
        self.fail_times = {}             # path -> remaining failures; '_error' holds the instance

    def _raw_request(self, path, method='GET', body=None):
        if not os.environ.get('GITHUB_TOKEN'):
            raise Rejected('GitHub token absent')
        self.calls.append((path, method))
        if self.fail_always is not None:
            raise self.fail_always
        remaining = self.fail_times.get(path, 0)
        if remaining > 0:
            self.fail_times[path] = remaining - 1
            raise self.fail_times['_error']
        if 'runs' in path:
            return {'workflow_runs': [], 'total_count': 0}
        return dict(path=path, ok=True)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.api = RecordingAPI()

    def _with_token(self):
        return patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN})

    def test_successful_request_returns_json(self):
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(len(self.api.calls), 1)

    def test_429_retries_then_succeeds(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': http_error(429)}
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)

    def test_502_retries_then_succeeds(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': http_error(502)}
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)

    def test_503_retries_then_succeeds(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': http_error(503)}
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)

    def test_timeout_retries_then_succeeds(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': TimeoutError()}
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)

    def test_network_error_retries_then_succeeds(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': urllib.error.URLError('down')}
        with patch.object(a.time, 'sleep'), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)

    def test_repeated_interruptions_then_recovery(self):
        # Fail 429, then 502, then timeout, then succeed — all inside a single
        # request() call so the real bounded backoff-and-retry path runs.
        self.api.fail_times = {'/pulls/1': 3, '_error': http_error(429)}
        errors = iter([http_error(502), TimeoutError()])

        def mutate(delay):
            try:
                self.api.fail_times['_error'] = next(errors)
            except StopIteration:
                pass

        with patch.object(a.time, 'sleep', side_effect=mutate), self._with_token():
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 4)

    def test_exhausted_retries_raise_transient_error(self):
        self.api.fail_always = http_error(502)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(a.TransientError):
            self.api.request('/pulls/1')
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), a.MAX_REQUEST_ATTEMPTS)

    def test_unauthorized_fails_closed_without_retry(self):
        self.api.fail_always = http_error(401)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(Rejected):
            self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 1)

    def test_forbidden_fails_closed_without_retry(self):
        self.api.fail_always = http_error(403)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(Rejected):
            self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 1)

    def test_not_found_fails_closed_without_retry(self):
        self.api.fail_always = http_error(404)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(Rejected):
            self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 1)

    def test_unprocessable_fails_closed_without_retry(self):
        self.api.fail_always = http_error(422)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(Rejected):
            self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 1)

    def test_cas_conflict_fails_closed_without_retry(self):
        self.api.fail_always = http_error(409)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(Rejected):
            self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 1)

    def test_missing_token_fails_closed(self):
        with patch.object(a.time, 'sleep'), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Rejected):
                self.api.request('/pulls/1')
        self.assertEqual(len(self.api.calls), 0)

    def test_retry_backoff_honors_retry_after(self):
        self.api.fail_times = {'/pulls/1': 1, '_error': http_error(429)}
        with patch.object(a.time, 'sleep') as sleep, self._with_token():
            self.api.request('/pulls/1')
        self.assertEqual(len(sleep.call_args_list), 1)
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 0)

    def test_retry_after_value_is_honored_and_capped(self):
        for seconds, expected in ((7, 7), (60, 60)):
            with self.subTest(retry_after=seconds):
                api = RecordingAPI()
                api.fail_times = {'/pulls/1': 1, '_error': headered_error(429, Retry_After=seconds)}
                with patch.object(a.time, 'sleep') as sleep, self._with_token():
                    api.request('/pulls/1')
                self.assertGreaterEqual(sleep.call_args_list[0].args[0], expected)
                self.assertLessEqual(sleep.call_args_list[0].args[0], a.MAX_RETRY_DELAY_SECONDS)

    def test_backoff_across_requests_and_clients_shares_one_run_budget(self):
        slept = []
        with patch.object(a.time, 'sleep', side_effect=slept.append), self._with_token(), \
                patch.object(a.GitHub, '_slept', 0.0):
            with self.assertRaises(a.TransientError):
                for n in range(10):                         # a fresh client each time, as the collaboration code builds them
                    api = RecordingAPI()
                    api.fail_times = {f'/pulls/{n}': 1, '_error': headered_error(429, Retry_After=60)}
                    api.request(f'/pulls/{n}')
        self.assertLessEqual(sum(slept), a.MAX_RUN_RETRY_SECONDS)
        self.assertGreater(len(slept), 1)


def headered_error(code, **headers):
    msg = email.message.Message()
    for key, value in headers.items():
        msg[key.replace('_', '-')] = str(value)
    return urllib.error.HTTPError('https://api.github.com/repos/o/r/pulls/1', code, 'err', msg, None)


class QuotaResetTests(unittest.TestCase):
    def setUp(self):
        self.api = RecordingAPI()

    def test_500_with_quota_and_far_reset_uses_ordinary_backoff(self):
        reset = int(time.time()) + 1800
        self.api.fail_times = {'/pulls/1': 1, '_error': headered_error(
            500, X_RateLimit_Remaining=4999, X_RateLimit_Reset=reset)}
        with patch.object(a.time, 'sleep') as sleep, patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            result = self.api.request('/pulls/1')
        self.assertEqual(result, dict(path='/pulls/1', ok=True))
        self.assertEqual(self.api.calls.count(('/pulls/1', 'GET')), 2)
        self.assertLessEqual(sleep.call_args_list[0].args[0], a.MAX_RETRY_DELAY_SECONDS)

    def test_exhausted_quota_429_with_far_reset_is_transient_not_slept(self):
        reset = int(time.time()) + 1800
        self.api.fail_times = {'/pulls/1': 1, '_error': headered_error(
            429, X_RateLimit_Remaining=0, X_RateLimit_Reset=reset)}
        with patch.object(a.time, 'sleep') as sleep, patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            with self.assertRaises(a.TransientError):
                self.api.request('/pulls/1')
        sleep.assert_not_called()

    def test_403_rate_limit_with_near_reset_is_honored(self):
        reset = int(time.time()) + 10
        self.api.fail_times = {'/pulls/1': 1, '_error': headered_error(
            403, X_RateLimit_Remaining=0, X_RateLimit_Reset=reset)}
        with patch.object(a.time, 'sleep') as sleep, patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            self.api.request('/pulls/1')
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 5)


class PagesTests(unittest.TestCase):
    """Pagination itself must not mask transient failures."""

    def setUp(self):
        self.api = RecordingAPI()

    def _with_token(self):
        return patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN})

    def test_pages_recovers_from_transient_failure(self):
        self.api.fail_times = {'/actions/workflows/1/runs?per_page=100&page=1': 1,
                               '_error': http_error(503)}
        with patch.object(a.time, 'sleep'), self._with_token():
            values = self.api.pages('/actions/workflows/1/runs', 'workflow_runs')
        self.assertEqual(values, [])

    def test_pages_persistent_failure_raises_transient_not_rejected(self):
        # A persistent outage escalates as TransientError; it is never
        # reclassified as an authorization/eligibility failure (Rejected).
        self.api.fail_always = http_error(503)
        with patch.object(a.time, 'sleep'), self._with_token(), self.assertRaises(a.TransientError):
            self.api.pages('/actions/workflows/1/runs', 'workflow_runs')


class ArtifactRetrievalTests(unittest.TestCase):
    """Temporary artifact outages are TransientError; definitive failures stay Rejected."""
    LOCATION = 'https://pipelines.actions.githubusercontent.com/blob'

    def _api(self, outcomes):
        api = RecordingAPI()
        api.pages = lambda path, key=None: [dict(
            id=5, name='evidence', expired=False, size_in_bytes=10, digest='sha256:00',
            workflow_run=dict(id=7, head_sha='m' * 40))]
        outcomes = list(outcomes)

        class Opener:
            def open(self_inner, req, timeout=None):
                outcome = outcomes.pop(0)
                raise outcome
        api.opener = Opener()
        return api

    def _fetch(self, outcomes):
        api = self._api(outcomes)
        with patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            return api.artifact(dict(id=7, head_sha='m' * 40, run_attempt=1), 'evidence')

    def _redirect(self):
        headers = email.message.Message()
        headers['Location'] = self.LOCATION
        return urllib.error.HTTPError('https://api.github.com/x', 302, 'found', headers, None)

    def test_redirect_step_outages_are_transient(self):
        for exc in (http_error(429), http_error(500), http_error(503),
                    urllib.error.URLError('down'), TimeoutError()):
            with self.assertRaises(a.TransientError, msg=repr(exc)):
                self._fetch([exc])

    def test_storage_outages_are_transient(self):
        for exc in (urllib.error.URLError('down'), TimeoutError(), http_error(503), http_error(429)):
            with self.assertRaises(a.TransientError, msg=repr(exc)):
                self._fetch([self._redirect(), exc])

    def test_definitive_artifact_failures_stay_rejected(self):
        for outcomes in ([http_error(404)], [http_error(403)],
                         [self._redirect(), http_error(404)], [self._redirect(), http_error(403)]):
            with self.assertRaises(Rejected):
                self._fetch(outcomes)
            # A definitive failure must not be misclassified as an outage.
            try:
                self._fetch(list(outcomes))
            except a.TransientError:
                self.fail('definitive failure classified as transient')
            except Rejected:
                pass

    def test_rate_limited_403_on_the_redirect_step_is_transient(self):
        limited = email.message.Message()
        limited['Retry-After'] = '1'
        exhausted = email.message.Message()
        exhausted['X-RateLimit-Remaining'] = '0'
        for headers in (limited, exhausted):
            exc = urllib.error.HTTPError('https://api.github.com/x', 403, 'forbidden', headers, None)
            with self.assertRaises(a.TransientError):
                self._fetch([exc])

    def test_plain_403_and_404_are_rejected_but_marked_as_possibly_infrastructure(self):
        for outcomes in ([http_error(403)], [http_error(404)], [self._redirect(), http_error(404)]):
            with self.assertRaises(a.ArtifactUnavailable):
                self._fetch(outcomes)
        self.assertTrue(issubclass(a.ArtifactUnavailable, Rejected))


def http_error_with(code, headers=None, body=b''):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError('https://api.github.com/repos/o/r/issues/1/comments', code, 'err', message,
                                  io.BytesIO(body))


class WriteRetryTests(unittest.TestCase):
    """Non-idempotent writes retry only on a rate limit, never on outages; the 403 heuristic is bounded."""

    def setUp(self):
        self.api = RecordingAPI()

    def _call(self, method, error):
        self.api.fail_times = {'/issues/1/comments': 1, '_error': error}
        with patch.object(a.time, 'sleep'), patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            return self.api.request('/issues/1/comments', method, {'body': 'x'})

    def test_writes_retry_once_on_a_rate_limit_then_succeed(self):
        for method in ('PUT', 'POST'):
            for error in (http_error(429), http_error_with(403, {'Retry-After': '1'}),
                          http_error_with(403, {'X-RateLimit-Remaining': '0'})):
                with self.subTest(method=method, error=error.code):
                    self.api.calls = []
                    self.assertTrue(self._call(method, error)['ok'])
                    self.assertEqual(self.api.calls, [('/issues/1/comments', method)] * 2)

    def test_writes_are_never_retried_after_an_outage(self):
        for method in ('PUT', 'POST'):
            for code in (500, 502, 503):
                with self.subTest(method=method, code=code):
                    self.api.calls = []
                    with self.assertRaises(a.TransientError):
                        self._call(method, http_error(code))
                    self.assertEqual(len(self.api.calls), 1)

    def test_403_body_text_only_counts_with_rate_limit_wording(self):
        limited = http_error_with(403, body=b'{"message":"You have exceeded a secondary rate limit"}')
        abuse = http_error_with(403, body=b'{"message":"abuse detection mechanism triggered"}')
        denied = http_error_with(403, body=b'{"message":"Resource not accessible by integration"}')
        self.assertTrue(self.api._is_rate_limit_response(limited))
        self.assertTrue(self.api._is_rate_limit_response(abuse))
        self.assertFalse(self.api._is_rate_limit_response(denied))

    def test_a_permission_denial_is_rejected_after_a_single_attempt(self):
        denied = http_error_with(403, body=b'{"message":"Resource not accessible by integration"}')
        self.api.fail_always = denied
        with patch.object(a.time, 'sleep'), patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            with self.assertRaises(Rejected):
                self.api.request('/issues/1/comments', 'POST', {'body': 'x'})
        self.assertEqual(len(self.api.calls), 1)


class ThreadsAndPaginationTests(unittest.TestCase):
    """GraphQL and pagination outages defer; malformed or unauthorized answers still fail closed."""

    def _threads(self, outcome):
        api = RecordingAPI()
        api.policy = {'repository': 'o/r'}

        class Opener:
            def open(self_inner, req, timeout=None):
                if isinstance(outcome, BaseException):
                    raise outcome
                return io.BytesIO(json.dumps(outcome).encode())
        api.opener = Opener()
        with patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            return api.threads(211)

    def test_graphql_outages_and_throttles_are_transient(self):
        for error in (http_error(502), http_error(503), http_error(429), http_error_with(403, {'Retry-After': '5'}),
                      urllib.error.URLError('down'), TimeoutError()):
            with self.subTest(error=repr(error)):
                with self.assertRaises(a.TransientError):
                    self._threads(error)

    def test_graphql_denials_and_malformed_answers_still_fail_closed(self):
        for outcome in (http_error(401), http_error_with(403, body=b'{"message":"no"}'), {'errors': [{'message': 'x'}]},
                        {'data': {}}):
            with self.subTest(outcome=repr(outcome)):
                with self.assertRaises(Rejected) as ctx:
                    self._threads(outcome)
                self.assertNotIsInstance(ctx.exception, a.TransientError)

    def test_pagination_drift_defers_and_malformed_pages_still_fail_closed(self):
        api = RecordingAPI()
        pages = iter([{'workflow_runs': [{}] * 100, 'total_count': 150}, {'workflow_runs': [{}] * 50, 'total_count': 151}])
        api.request = lambda path: next(pages)
        with self.assertRaises(a.TransientError):
            api.pages('/actions/runs', 'workflow_runs')
        short = RecordingAPI()
        short.request = lambda path: {'workflow_runs': [{}] * 10, 'total_count': 150}
        with self.assertRaises(a.TransientError):
            short.pages('/actions/runs', 'workflow_runs')
        bad = RecordingAPI()
        bad.request = lambda path: {'workflow_runs': 'nope', 'total_count': 1}
        with self.assertRaises(Rejected):
            bad.pages('/actions/runs', 'workflow_runs')


if __name__ == '__main__':
    unittest.main()
