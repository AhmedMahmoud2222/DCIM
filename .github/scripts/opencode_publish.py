#!/usr/bin/env python3
"""Publish agent changes as one GitHub-signed commit and a Draft PR (publisher v1.0).

Runs in a fresh job that never executed agent code. Inputs are untrusted data:
the collector manifest and file blobs. Commits go through GraphQL
createCommitOnBranch with a GitHub App installation token, so GitHub itself signs
and attributes them to the App; no signing key exists anywhere in this flow.

Environment: GH_PUBLISH_TOKEN (never printed), GITHUB_REPOSITORY, DEFAULT_BRANCH,
EVENT_KIND (issue|pr), TARGET_NUMBER, PR_HEAD_REF (pr only), RUN_URL, RUN_ID,
RUN_ATTEMPT, ACTOR_LOGIN, AGENT_OUTCOME (optional), EXPECT_BOT_LOGIN, TASK, MODEL, CATEGORY, ROUTE_REASON.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.request

MAX_CHANGES = 200
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$')
# Authorization and credential controls the agent may never change. The App has no
# `workflows` permission either; this list also covers non-workflow control files.
PROTECTED_PREFIXES = (
    '.github/workflows/', '.github/opencode/', '.github/codeowners', '.github/dependabot.yml',
    '.opencode/', 'docs/security/opencode_agent_authority.md', 'opencode.json', 'opencode.jsonc',
)
PROTECTED_BASENAME_PREFIXES = ('.github/scripts/opencode_', 'tests/deployment/test_opencode_')
PROTECTED_NAMES = ('codeowners', 'agents.md')


class PublishError(Exception):
    pass


def is_protected_path(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(PROTECTED_PREFIXES) or lowered.startswith(PROTECTED_BASENAME_PREFIXES):
        return True
    return posixpath.basename(lowered) in PROTECTED_NAMES


def safe_path(path: str) -> str:
    if (not path or len(path) > 300 or path.startswith('/') or '\\' in path or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        raise PublishError(f'unsafe path rejected: {path[:80]!r}')
    parts = path.split('/')
    if any(part in ('', '.', '..') or part.lower() == '.git' for part in parts):
        raise PublishError(f'unsafe path rejected: {path[:80]!r}')
    if posixpath.normpath(path) != path:
        raise PublishError(f'non-normalized path rejected: {path[:80]!r}')
    return path


def validate_manifest(manifest: dict, root: str) -> dict:
    if manifest.get('version') != 1 or not SHA_RE.match(str(manifest.get('base_sha', ''))):
        raise PublishError('manifest version or base_sha invalid')
    if manifest.get('unsupported'):
        names = ', '.join(item.get('path', '?') for item in manifest['unsupported'][:5])
        raise PublishError(f'unsupported change types (symlink, mode change or submodule): {names}')
    changes = manifest.get('changes') or []
    if len(changes) > MAX_CHANGES:
        raise PublishError('too many changed files')
    seen, total, additions, deletions = set(), 0, [], []
    for change in changes:
        path = safe_path(change.get('path', ''))
        if path.lower() in seen:
            raise PublishError(f'duplicate path {path}')
        seen.add(path.lower())
        if is_protected_path(path):
            raise PublishError(f'protected control file cannot be changed by the agent: {path}')
        if change.get('op') == 'delete':
            deletions.append(path)
            continue
        if change.get('op') not in ('add', 'modify'):
            raise PublishError(f'unknown operation for {path}')
        blob = str(change.get('blob', ''))
        if not re.fullmatch(r'files/\d{1,6}', blob):
            raise PublishError(f'invalid blob reference for {path}')
        full = os.path.realpath(os.path.join(root, blob))
        if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full) \
                or os.path.islink(os.path.join(root, blob)):
            raise PublishError(f'blob missing or unsafe for {path}')
        with open(full, 'rb') as handle:
            data = handle.read()
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != change.get('sha256'):
            raise PublishError(f'blob size or checksum mismatch for {path}')
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise PublishError('total change size exceeds limit')
        additions.append({'path': path, 'contents': base64.b64encode(data).decode()})
    return {'base_sha': manifest['base_sha'], 'additions': additions, 'deletions': deletions}


def one_line(text: str, limit: int) -> str:
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', text or '').strip()[:limit].strip()


def check_branch(kind: str, branch: str, default_branch: str, protected: bool) -> None:
    if not BRANCH_RE.match(branch or '') or branch.endswith(('/', '.lock')) or '..' in branch:
        raise PublishError('target branch name is invalid')
    if branch == default_branch or protected:
        raise PublishError(f'refusing to publish to protected or default branch {branch}')
    if kind == 'issue' and not branch.startswith('opencode/'):
        raise PublishError('new branches must live under opencode/')


class Api:
    def __init__(self, token: str, repo: str):
        self.token, self.repo = token, repo

    def call(self, method: str, path: str, body: dict | None = None):
        url = path if path.startswith('https://') else f'https://api.github.com/repos/{self.repo}/{path}'
        request = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'dcim-opencode-publisher'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise PublishError(f'GitHub API {method} {path.split("?")[0]} failed with HTTP {error.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PublishError(f'GitHub API {method} {path.split("?")[0]} unreachable') from None
        return json.loads(raw) if raw else {}

    def graphql(self, query: str, variables: dict):
        result = self.call('POST', 'https://api.github.com/graphql', {'query': query, 'variables': variables})
        if result.get('errors'):
            raise PublishError('GraphQL error: ' + one_line(str(result['errors'][0].get('message', '')), 160))
        return result['data']


COMMIT_MUTATION = ('mutation($input: CreateCommitOnBranchInput!) '
                   '{ createCommitOnBranch(input: $input) { commit { oid url } } }')


def verify_commit(api, sha: str, expect_login: str, sleep=time.sleep) -> str:
    """Return '' when GitHub reports a valid signature by the expected App, else a reason."""
    reason = 'not checked'
    for attempt in range(3):
        data = api.call('GET', f'commits/{sha}')
        verification = (data.get('commit') or {}).get('verification') or {}
        login = (data.get('author') or {}).get('login', '')
        if verification.get('verified') is True and login.lower() == expect_login.lower():
            return ''
        reason = f"verified={verification.get('verified')} reason={verification.get('reason')} author={login}"
        if attempt < 2:
            sleep(2)
    return reason


def publish(api, env: dict, plan: dict, sleep=time.sleep) -> dict:
    repo, default = env['GITHUB_REPOSITORY'], env['DEFAULT_BRANCH']
    kind, number = env['EVENT_KIND'], env['TARGET_NUMBER']
    if not plan['additions'] and not plan['deletions']:
        return {'published': False, 'reason': 'no file changes'}
    if api.call('GET', '')['default_branch'] != default:
        raise PublishError('default branch differs from the event context')
    base = plan['base_sha']
    created_ref = False
    if kind == 'issue':
        branch = f"opencode/issue{number}-{env['RUN_ID']}-{env['RUN_ATTEMPT']}"
        check_branch(kind, branch, default, protected=False)
        if api.call('GET', f'compare/{default}...{base}')['status'] not in ('behind', 'identical'):
            raise PublishError('base commit is not reachable from the default branch')
        api.call('POST', 'git/refs', {'ref': f'refs/heads/{branch}', 'sha': base})
        created_ref = True
    else:
        branch = env['PR_HEAD_REF']
        info = api.call('GET', f'branches/{branch}')
        check_branch(kind, branch, default, bool(info.get('protected')))
        if info['commit']['sha'] != base:
            raise PublishError('pull request branch moved since the agent started; rerun the command')
    headline = 'opencode: ' + (one_line(env['TASK'], 60) or 'automated change')
    body = '\n'.join([
        f"OpenCode-Run: {env['RUN_URL']}", f"OpenCode-Model: {env['MODEL']}",
        f"OpenCode-Category: {env['CATEGORY']}", f"Requested-By: {one_line(env['ACTOR_LOGIN'], 60)}"])
    try:
        data = api.graphql(COMMIT_MUTATION, {'input': {
            'branch': {'repositoryNameWithOwner': repo, 'branchName': branch},
            'message': {'headline': headline, 'body': body}, 'expectedHeadOid': base,
            'fileChanges': {'additions': plan['additions'], 'deletions': [{'path': p} for p in plan['deletions']]},
        }})
        sha = data['createCommitOnBranch']['commit']['oid']
        problem = verify_commit(api, sha, env['EXPECT_BOT_LOGIN'], sleep)
    except PublishError:
        if created_ref:
            api.call('DELETE', f'git/refs/heads/{branch}')
        raise
    if problem:
        if created_ref:  # nothing is exposed yet: remove the branch before any PR exists
            api.call('DELETE', f'git/refs/heads/{branch}')
        raise PublishError(f'commit {sha[:12]} is not GitHub-verified ({problem}); publication stopped')
    result = {'published': True, 'branch': branch, 'commit': sha, 'verified': True, 'pr': None}
    if kind == 'issue':
        pr = api.call('POST', 'pulls', {
            'title': headline, 'head': branch, 'base': default, 'draft': True,
            'body': '\n'.join([
                f"Draft PR opened by the OpenCode agent for #{number}.", '',
                f"- Model: `{env['MODEL']}` (category `{env['CATEGORY']}`)",
                f"- Routing: {one_line(env['ROUTE_REASON'], 300)}",
                f"- Run: {env['RUN_URL']}", f"- Commit: {sha} (GitHub-verified)", '',
                f"Agent step outcome: {one_line(env.get('AGENT_OUTCOME', 'unknown'), 20)} "
                '(failure is expected when its read-only token blocks its own push).',
                'Tests were not run by the publisher; review the CI checks on this PR.',
                f"Refs #{number}"])})
        result['pr'] = {'number': pr['number'], 'url': pr['html_url']}
    where = f"PR #{result['pr']['number']}" if result['pr'] else f'branch `{branch}`'
    api.call('POST', f'issues/{number}/comments', {'body': (
        f"OpenCode published commit `{sha[:12]}` (GitHub-verified) to {where} using "
        f"`{env['MODEL']}`. It did not merge anything; CI and review remain required.")})
    return result


def main() -> int:
    try:
        root = os.environ['ARTIFACT_DIR']
        with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as handle:
            plan = validate_manifest(json.load(handle), root)
        env = {key: os.environ[key] for key in (
            'GITHUB_REPOSITORY', 'DEFAULT_BRANCH', 'EVENT_KIND', 'TARGET_NUMBER', 'RUN_URL', 'RUN_ID',
            'RUN_ATTEMPT', 'ACTOR_LOGIN', 'EXPECT_BOT_LOGIN', 'TASK', 'MODEL', 'CATEGORY', 'ROUTE_REASON')}
        env['PR_HEAD_REF'] = os.environ.get('PR_HEAD_REF', '')
        env['AGENT_OUTCOME'] = os.environ.get('AGENT_OUTCOME', 'unknown')
        if env['EVENT_KIND'] not in ('issue', 'pr') or not env['TARGET_NUMBER'].isdigit():
            raise PublishError('invalid event context')
        result = publish(Api(os.environ['GH_PUBLISH_TOKEN'], env['GITHUB_REPOSITORY']), env, plan)
    except KeyError as error:
        print(f'::error::missing required configuration {error}')
        return 1
    except (PublishError, OSError, ValueError) as error:
        print(f'::error::OpenCode publication stopped: {error}')
        return 1
    print(json.dumps(result))
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as handle:
            handle.write(f"published={'true' if result['published'] else 'false'}\n")
            for key in ('commit', 'branch'):
                if result.get(key):
                    handle.write(f'{key}={result[key]}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
