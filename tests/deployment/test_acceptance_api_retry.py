"""Client-level regression tests for GitHub API transient-failure retries.

Covers 429 rate limit, 502/503 server errors, timeouts, recovery and repeated
interruptions. Definitive failures (401/403/404/422) fail closed without retry.
"""
import email.message
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


if __name__ == '__main__':
    unittest.main()
