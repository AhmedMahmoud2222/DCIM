"""Authenticated GitHub evidence collection; no candidate checkout/execution."""
import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from acceptance_core import Rejected, require, decode, digest, source_run, jobs_and_checks


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Retryable failures are transient outages (rate limiting, server errors,
# network and timeout failures). Anything else - authorization (401/403),
# task-ineligibility (404/422), CAS conflicts (409) and malformed responses -
# is definitive and fails closed without retry.
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_REQUEST_ATTEMPTS = 5


class TransientError(Exception):
    """A retryable GitHub API failure exhausted its bounded backoff budget.

    Callers must treat this as an outage, never as an authorization or
    task-ineligibility decision, and preserve the existing task state so the
    next scheduled execution can retry.
    """


class GitHub:
    def __init__(self, policy, root=None):
        self.policy = policy
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.api_root = 'https://api.github.com/repos/' + policy['repository']
        self.opener = urllib.request.build_opener(NoRedirect)

    def _retry_delay(self, attempt, exc=None):
        """Bounded exponential backoff, honoring GitHub's Retry-After when given."""
        delay = min(30.0, 0.5 * (2 ** attempt))
        if isinstance(exc, urllib.error.HTTPError):
            retry_after = exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Reset')
            if retry_after is not None:
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
        return delay

    def _raw_request(self, path, method='GET', body=None):
        """One HTTP attempt; overridable by test stubs."""
        require(path.startswith('/') and not path.startswith('//'), 'invalid API path')
        token = os.environ.get('GITHUB_TOKEN')
        require(bool(token), 'GitHub token absent')
        request = urllib.request.Request(self.api_root + path, method=method,
            data=None if body is None else json.dumps(body).encode(), headers={
                'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'dcim-acceptance-gate'})
        with self.opener.open(request, timeout=25) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            require(len(raw) <= 8 * 1024 * 1024, 'API response too large')
            return json.loads(raw)

    def request(self, path, method='GET', body=None):
        """Bounded, backoff-aware retries over transient GitHub failures.

        Retryable network, rate-limit (429), timeout and HTTP 5xx failures are
        retried with backoff. Definitive authorization (401/403) and
        task-ineligibility (404/422) failures fail closed immediately, as do
        CAS conflicts (409). A persistent outage raises TransientError after
        exhausting the bounded budget; callers treat it as an outage, never an
        authorization decision, and preserve existing task state.
        """
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                return self._raw_request(path, method, body)
            except urllib.error.HTTPError as exc:
                # Definitive authorization (401/403) and task-ineligibility
                # (404/422/CAS 409) failures fail closed immediately and are
                # never retried. Retryable 5xx/429 failures back off; a
                # persistent outage escalates as TransientError, never as an
                # authorization failure.
                if exc.code not in RETRYABLE_HTTP_CODES:
                    raise Rejected('GitHub API failed: ' + path.split('?')[0] +
                                   f' (HTTP {exc.code})') from exc
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise TransientError('GitHub API unavailable: ' + path.split('?')[0]) from exc
                time.sleep(self._retry_delay(attempt, exc))
                continue
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise TransientError('GitHub API unavailable: ' + path.split('?')[0]) from exc
                time.sleep(self._retry_delay(attempt, exc))
                continue
            except ValueError as exc:
                raise Rejected('GitHub API failed: ' + path.split('?')[0]) from exc
        raise Rejected('GitHub API failed: ' + path.split('?')[0])  # unreachable

    def pages(self, path, key=None, total_key='total_count', limit=2000):
        values, total = [], None
        for page in range(1, 22):
            data = self.request(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            items = data.get(key) if key else data
            require(isinstance(items, list) and len(items) <= 100, 'malformed pagination')
            if key:
                n = data.get(total_key)
                require(type(n) is int and 0 <= n <= limit and (total is None or n == total), 'changing/incomplete pagination')
                total = n
                require(len(items) == min(100, total - len(values)), 'truncated page')
            values.extend(items)
            require(len(values) <= limit, 'evidence collection limit exceeded')
            if (key and len(values) == total) or (not key and len(items) < 100):
                return values
        raise Rejected('pagination limit exceeded')

    def workflow(self, path):
        return self.request('/actions/workflows/' + path.rsplit('/', 1)[-1])['id']

    def file(self, path, sha):
        data = self.request('/contents/' + path + '?ref=' + sha)
        require(data.get('type') == 'file' and data.get('encoding') == 'base64', 'missing workflow source')
        try:
            return base64.b64decode(data['content'].replace('\n', ''), validate=True)
        except (ValueError, KeyError) as exc:
            raise Rejected('invalid workflow source') from exc

    def trusted_file(self, path, sha):
        require(self.file(path, sha) == (self.root / path).read_bytes(), 'compromised workflow/policy provenance: ' + path)

    def runtime(self, path):
        """Refuse dispatches from feature branches, PR merge refs or stale main."""
        repo, branch = self.policy['repository'], self.policy['default_branch']
        require(os.environ.get('GITHUB_REPOSITORY') == repo, 'wrong runtime repository')
        require(os.environ.get('GITHUB_REF') == 'refs/heads/' + branch, 'default branch execution required')
        require(os.environ.get('GITHUB_WORKFLOW_REF') == repo + '/' + path + '@refs/heads/' + branch,
                'untrusted workflow ref')
        sha = os.environ.get('GITHUB_WORKFLOW_SHA', '')
        require(re.fullmatch(r'[0-9a-f]{40}', sha) is not None, 'missing workflow revision')
        require(self.request('/branches/' + branch)['commit']['sha'] == sha, 'workflow revision is not current main')
        commit = self.request('/commits/' + sha)
        v = commit.get('commit', {}).get('verification', {})
        require(commit.get('sha') == sha and v.get('verified') is True and v.get('reason') == 'valid', 'unsigned trusted workflow revision')
        self.trusted_file(path, sha)
        self.trusted_file('.github/acceptance/policy.json', sha)
        for name in ('acceptance_core.py', 'acceptance_api.py', 'acceptance_gate.py', 'acceptance_provider_witness.py', 'acceptance_read_guard.py'):
            self.trusted_file('.github/scripts/' + name, sha)
        return sha

    def validate_run(self, run, kind, trust_sha, completed=True):
        spec = self.policy['workflows'][kind]
        source_run(run, spec, self.workflow(spec['path']), self.policy, trust_sha, completed)
        self.trusted_file(spec['path'], trust_sha)
        self.trusted_file('.github/acceptance/policy.json', trust_sha)
        for name in ('acceptance_core.py', 'acceptance_api.py', 'acceptance_gate.py', 'acceptance_provider_witness.py', 'acceptance_read_guard.py'):
            self.trusted_file('.github/scripts/' + name, trust_sha)
        if completed:
            jobs = self.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
            checks = self.pages(f"/check-suites/{run['check_suite_id']}/check-runs", 'check_runs')
            jobs_and_checks(run, jobs, checks, spec['jobs'], self.policy)

    def artifact(self, run, name):
        artifacts = self.pages(f"/actions/runs/{run['id']}/artifacts", 'artifacts')
        matches = [a for a in artifacts if a.get('name') == name]
        require(len(matches) == 1, 'missing/duplicate evidence artifact')
        artifact = matches[0]
        require(artifact.get('expired') is False and 0 < artifact.get('size_in_bytes', 0) <= 1048576,
                'expired/oversized artifact')
        require(artifact.get('workflow_run', {}).get('id') == run['id']
                and artifact['workflow_run'].get('head_sha') == run['head_sha'], 'artifact belongs to another execution')
        path = f"/actions/artifacts/{artifact['id']}/zip"
        token = os.environ.get('GITHUB_TOKEN', '')
        req = urllib.request.Request(self.api_root + path, headers={'Authorization': 'Bearer ' + token})
        try:
            self.opener.open(req, timeout=25)
            raise Rejected('artifact redirect missing')
        except urllib.error.HTTPError as exc:
            require(exc.code == 302, 'artifact download failed')
            location = exc.headers.get('Location', '')
        u = urllib.parse.urlsplit(location)
        require(u.scheme == 'https' and u.hostname and not u.username and not u.password
                and u.port in (None, 443) and not u.fragment
                and (u.hostname.endswith('.blob.core.windows.net')
                     or u.hostname.endswith('.actions.githubusercontent.com')), 'untrusted artifact redirect')
        # Never forward Authorization to the signed storage URL or follow a second redirect.
        try:
            with self.opener.open(urllib.request.Request(location), timeout=25) as response:
                raw = response.read(1048577)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Rejected('artifact storage unavailable') from exc
        require(len(raw) <= 1048576 and artifact.get('digest') == 'sha256:' + hashlib.sha256(raw).hexdigest(),
                'artifact integrity mismatch')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                require(len(entries) == 1 and entries[0].filename == 'evidence.json'
                        and entries[0].file_size <= 524288
                        and (entries[0].external_attr >> 16) & 0o170000 != 0o120000, 'unsafe evidence archive')
                value = decode(archive.read(entries[0]))
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise Rejected('invalid evidence archive') from exc
        require(isinstance(value, dict), 'artifact is not an evidence object')
        require(value.get('source') == dict(run_id=run['id'], attempt=run['run_attempt'],
                                          trust_sha=run['head_sha']), 'artifact execution binding mismatch')
        require(value.get('policy_digest') == digest(self.policy), 'stale/modified evidence policy')
        return value, artifact['id']

    def threads(self, number):
        """GraphQL thread resolution is authoritative; ordinary comment verdicts are not."""
        repo_owner, repo_name = self.policy['repository'].split('/')
        query = '''query($o:String!,$r:String!,$n:Int!,$cursor:String){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100,after:$cursor){nodes{id isResolved} pageInfo{hasNextPage endCursor}}}}}'''
        token = os.environ.get('GITHUB_TOKEN', '')
        cursor, result = None, []
        for _ in range(20):
            req = urllib.request.Request('https://api.github.com/graphql', method='POST',
                data=json.dumps(dict(query=query, variables=dict(o=repo_owner, r=repo_name, n=number, cursor=cursor))).encode(),
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
            try:
                with self.opener.open(req, timeout=25) as response:
                    data = json.load(response)
                require(not data.get('errors'), 'review thread API error')
                connection = data['data']['repository']['pullRequest']['reviewThreads']
                result.extend(connection['nodes'])
                info = connection['pageInfo']
                if info['hasNextPage'] is False:
                    require(len({t['id'] for t in result}) == len(result), 'duplicate review threads')
                    require(all(type(t['isResolved']) is bool for t in result), 'malformed review threads')
                    return result
                require(info['endCursor'] and info['endCursor'] != cursor, 'invalid review cursor')
                cursor = info['endCursor']
            except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                raise Rejected('review thread evidence unavailable') from exc
        raise Rejected('too many review threads')
