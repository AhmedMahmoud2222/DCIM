#!/usr/bin/env python3
"""Publish agent changes as one GitHub-signed commit and a Draft PR (publisher v4.0).

Runs in a fresh job that never executed agent code. Target identity and the baseline
come ONLY from trusted workflow values (the gate's outputs and the event). The artifact
manifest is untrusted data: it must agree with them, and is otherwise used only as file
content. Commits use GraphQL createCommitOnBranch with a GitHub App installation token,
so GitHub signs them and attributes them to the App; no signing key exists here.

Existing PR branches are never touched until the commit is verified: the commit is
created on a fresh staging branch and verified. Immediately before the PR branch moves, the
PR is refetched and every authorization condition is checked again. The move itself is an
exact-SHA compare-and-swap (git push --force-with-lease=REF:EXPECTED): it succeeds only if
the remote branch is still exactly the authorized SHA, so a concurrent push, force-push or
reset to an ancestor makes it fail instead of silently undoing that change. The REST
ref-update endpoint cannot give that guarantee and is not used.

After any ambiguous GitHub failure the real remote state is queried before anything is
claimed; the facts are written to the evidence file for manual recovery. Nothing is ever
force-pushed or rewritten.

Environment (trusted): GH_PUBLISH_TOKEN (never printed), GITHUB_REPOSITORY, DEFAULT_BRANCH,
EVENT_KIND (issue|pr), TARGET_NUMBER, SESSION_ID, AUTHORIZED_SHA, PR_HEAD_REF (pr only),
GENERATION_STATUS, RUN_URL, RUN_ID, RUN_ATTEMPT, ACTOR_LOGIN, EXPECT_BOT_LOGIN, TASK, MODEL,
CATEGORY, ROUTE_REASON; ARTIFACT_DIR for the downloaded artifact.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode_review as review  # noqa: E402
import opencode_sandbox as sandbox  # noqa: E402

MAX_CHANGES = 200
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$')
TOP_KEYS = {'version', 'base_sha', 'changes', 'unsupported'}
CHANGE_KEYS = {'delete': {'path', 'op'}, 'add': {'path', 'op', 'size', 'sha256', 'blob'},
               'modify': {'path', 'op', 'size', 'sha256', 'blob'}}
# Authorization and credential controls the agent may never change. The App has no
# `workflows` permission either; this list also covers non-workflow control files.
PROTECTED_PREFIXES = (
    '.github/workflows/', '.github/opencode/', '.github/codeowners', '.github/dependabot.yml',
    '.opencode/', 'docs/security/opencode_agent_authority.md', 'opencode.json', 'opencode.jsonc',
)
# .github/scripts/ci_ and tests/ci/ hold the CI route, the required-check gates and
# their tests (docs/ci/CI_ARCHITECTURE_ADR.md): an agent PR must not choose its own CI scope.
PROTECTED_BASENAME_PREFIXES = ('.github/scripts/opencode_', 'tests/deployment/test_opencode_',
                               'tests/deployment/mutation_check', 'tests/deployment/fixtures/',
                               'tests/deployment/opencode_validation/', '.github/scripts/ci_',
                               'tests/ci/')
PROTECTED_NAMES = ('codeowners', 'agents.md')


class PublishError(Exception):
    pass


class AmbiguousApiError(PublishError):
    """The outcome of a GitHub call is unknown (network failure, timeout or 5xx)."""


class PartialPublication(PublishError):
    """Something durable exists on GitHub although the run did not complete."""

    def __init__(self, message: str, branch: str, commit: str, evidence: dict | None = None):
        super().__init__(message)
        self.branch, self.commit, self.evidence = branch, commit, evidence or {}


def is_protected_path(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(PROTECTED_PREFIXES) or lowered.startswith(PROTECTED_BASENAME_PREFIXES):
        return True
    if posixpath.basename(lowered) in PROTECTED_NAMES:
        return True
    return sandbox.is_sensitive_name(path) or sandbox.is_untrusted_config(path)


def safe_path(path: str) -> str:
    if (not isinstance(path, str) or not path or len(path) > 300 or path.startswith('/') or '\\' in path
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        raise PublishError(f'unsafe path rejected: {str(path)[:80]!r}')
    parts = path.split('/')
    if any(part in ('', '.', '..') or part.lower() == '.git' for part in parts):
        raise PublishError(f'unsafe path rejected: {path[:80]!r}')
    if posixpath.normpath(path) != path:
        raise PublishError(f'non-normalized path rejected: {path[:80]!r}')
    return path


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_manifest(manifest: object, root: str) -> dict:
    """Strictly validate untrusted manifest data and the artifact tree. Raises PublishError."""
    if not isinstance(manifest, dict) or set(manifest) != TOP_KEYS:
        raise PublishError('manifest has missing or unexpected keys')
    if manifest['version'] != 2 or not isinstance(manifest['base_sha'], str) \
            or not SHA_RE.match(manifest['base_sha']):
        raise PublishError('manifest version or base_sha invalid')
    unsupported = manifest['unsupported']
    if not isinstance(unsupported, list) or not isinstance(manifest['changes'], list):
        raise PublishError('manifest changes/unsupported must be lists')
    if unsupported:
        names = ', '.join(str(item.get('path', '?')) if isinstance(item, dict) else '?' for item in unsupported[:5])
        raise PublishError(f'unsupported change types (symlink, mode change or submodule): {names}')
    changes = manifest['changes']
    if len(changes) > MAX_CHANGES:
        raise PublishError('too many changed files')
    seen, total, additions, deletions, blobs = set(), 0, [], [], set()
    for change in changes:
        if not isinstance(change, dict) or change.get('op') not in CHANGE_KEYS:
            raise PublishError('malformed change entry')
        if set(change) != CHANGE_KEYS[change['op']]:
            raise PublishError('change entry has missing or unexpected keys')
        path = safe_path(change['path'])
        if path.lower() in seen:
            raise PublishError(f'duplicate path {path}')
        seen.add(path.lower())
        if is_protected_path(path):
            raise PublishError(f'protected control or credential-like file cannot be changed by the agent: {path}')
        if change['op'] == 'delete':
            deletions.append(path)
            continue
        blob, size, digest = change['blob'], change['size'], change['sha256']
        if not isinstance(blob, str) or not re.fullmatch(r'files/\d{1,6}', blob) or blob in blobs:
            raise PublishError(f'invalid blob reference for {path}')
        if not _is_int(size) or size < 0 or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise PublishError(f'invalid size or checksum field for {path}')
        blobs.add(blob)
        full = os.path.join(root, blob)
        if os.path.islink(full) or not os.path.isfile(full) \
                or not os.path.realpath(full).startswith(os.path.realpath(root) + os.sep):
            raise PublishError(f'blob missing or unsafe for {path}')
        if os.path.getsize(full) > MAX_FILE_BYTES:
            raise PublishError(f'blob too large for {path}')
        with open(full, 'rb') as handle:
            data = handle.read()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise PublishError(f'declared size or checksum does not match blob for {path}')
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise PublishError('total change size exceeds limit')
        additions.append({'path': path, 'contents': base64.b64encode(data).decode()})
    on_disk = set()
    for name in os.listdir(os.path.join(root, 'files')) if os.path.isdir(os.path.join(root, 'files')) else []:
        on_disk.add(f'files/{name}')
    if on_disk != blobs or set(os.listdir(root)) - {'manifest.json', 'files'}:
        raise PublishError('artifact contains files not declared in the manifest')
    return {'base_sha': manifest['base_sha'], 'additions': additions, 'deletions': deletions}


def load_manifest(root: str) -> dict:
    path = os.path.join(root, 'manifest.json')
    if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_MANIFEST_BYTES:
        raise PublishError('manifest missing or too large')
    with open(path, encoding='utf-8') as handle:
        try:
            return validate_manifest(json.load(handle), root)
        except ValueError:
            raise PublishError('manifest is not valid JSON') from None


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
        label = f'GitHub API {method} {path.split("?")[0]}'
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            cls = AmbiguousApiError if error.code >= 500 else PublishError
            failure = cls(f'{label} failed with HTTP {error.code}')
            failure.status = error.code
            raise failure from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AmbiguousApiError(f'{label} unreachable or timed out') from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise AmbiguousApiError(f'{label} returned an unreadable response') from None

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
        if verification.get('verified') is True and verification.get('reason') == 'valid' \
                and login.lower() == expect_login.lower():
            return ''
        reason = f"verified={verification.get('verified')} reason={verification.get('reason')} author={login}"
        if attempt < 2:
            sleep(2)
    return reason


def remote_sha(api, branch: str) -> str | None:
    """Actual remote tip of `branch` (None when the branch does not exist). Raises if unknowable."""
    try:
        return api.call('GET', f'git/ref/heads/{branch}')['object']['sha']
    except AmbiguousApiError:
        raise
    except PublishError as error:
        if getattr(error, 'status', None) == 404:
            return None
        raise


def check_pr_target(api, env: dict) -> str:
    """Refetch the PR and branch and verify every authorization condition. Returns the branch."""
    repo, default, number = env['GITHUB_REPOSITORY'], env['DEFAULT_BRANCH'], env['TARGET_NUMBER']
    authorized = env['AUTHORIZED_SHA']
    pr = api.call('GET', f'pulls/{number}')
    head, pr_base = pr.get('head') or {}, pr.get('base') or {}
    checks = [
        (pr.get('number') == int(number), 'pull request number does not match the authorized event'),
        (pr.get('state') == 'open' and not pr.get('merged'), 'pull request is not open and unmerged'),
        (((pr_base.get('repo') or {}).get('full_name') or '').lower() == repo.lower(), 'unexpected base repository'),
        (((head.get('repo') or {}).get('full_name') or '').lower() == repo.lower(), 'unexpected head repository'),
        (head.get('ref') == env['PR_HEAD_REF'], 'head branch is not the approved branch'),
        (head.get('sha') == authorized, 'pull request head moved since authorization'),
    ]
    for ok, message in checks:
        if not ok:
            raise PublishError(message)
    branch = env['PR_HEAD_REF']
    info = api.call('GET', f'branches/{branch}')
    check_branch('pr', branch, default, bool(info.get('protected')))
    if (info.get('commit') or {}).get('sha') != authorized:
        raise PublishError('live branch tip does not match the authorized baseline')
    return branch


def bind_target(api, env: dict, base: str) -> str:
    """Independently fetch the target from GitHub and bind it to the trusted values.

    Returns the PR branch (empty for issues). Raises before any mutation on a mismatch.
    """
    repo, default, number = env['GITHUB_REPOSITORY'], env['DEFAULT_BRANCH'], env['TARGET_NUMBER']
    authorized = env['AUTHORIZED_SHA']
    if not SHA_RE.match(authorized):
        raise PublishError('trusted baseline is not a full SHA')
    if base != authorized:
        raise PublishError('artifact baseline does not match the trusted authorized baseline')
    meta = api.call('GET', '')
    if meta.get('default_branch') != default or meta.get('full_name', repo).lower() != repo.lower():
        raise PublishError('repository identity or default branch differs from the event context')
    if not api.call('GET', f'branches/{default}').get('protected'):
        raise PublishError('default branch is not protected; refusing to publish')
    if env['EVENT_KIND'] == 'issue':
        issue = api.call('GET', f'issues/{number}')
        if issue.get('number') != int(number) or 'pull_request' in issue or issue.get('state') != 'open':
            raise PublishError('target is not an open issue with the authorized number')
        if api.call('GET', f'compare/{default}...{authorized}').get('status') not in ('behind', 'identical'):
            raise PublishError('authorized baseline is not reachable from the default branch')
        return ''
    return check_pr_target(api, env)


class PushRejected(PublishError):
    pass


def git_cas_push(repo_dir: str, remote: str, token: str | None, branch: str, expected: str, new: str,
                 staging: str, runner=subprocess.run) -> None:
    """Move refs/heads/<branch> from `expected` to `new` only if it still equals `expected`.

    Uses git's lease with the exact expected SHA. A lease is a compare-and-swap on the old
    value, enforced again by the server when it applies the update. The caller has already
    established (through GitHub's compare data) that `new` descends from `expected`, so the
    update is a fast-forward; the lease additionally refuses any other remote state, including
    a concurrent reset to an ancestor, which a fast-forward-only check would accept.
    """
    if not (SHA_RE.match(expected) and SHA_RE.match(new)):
        raise PushRejected('refusing to push with a non-SHA expectation')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'GH_', 'GITHUB_TOKEN'))}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
    if token:  # kept out of argv and out of logs
        credential = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                    'GIT_CONFIG_VALUE_0': f'AUTHORIZATION: basic {credential}'})
    base_cmd = ['git', '-C', repo_dir, '-c', 'protocol.version=2']
    fetch = runner([*base_cmd, 'fetch', '--no-tags', '--depth=2', remote,
                    f'+refs/heads/{staging}:refs/opencode/staging'], capture_output=True, text=True, env=env)
    if fetch.returncode != 0:
        raise PushRejected('could not fetch the verified staging commit: ' + one_line(fetch.stderr.replace(token or '\0', '***'), 160))
    head = runner([*base_cmd, 'rev-parse', 'refs/opencode/staging'], capture_output=True, text=True, env=env)
    if head.returncode != 0 or head.stdout.strip() != new:
        raise PushRejected('staging branch does not hold the verified commit')
    push = runner([*base_cmd, 'push', '--porcelain', f'--force-with-lease=refs/heads/{branch}:{expected}',
                   remote, f'{new}:refs/heads/{branch}'], capture_output=True, text=True, env=env)
    if push.returncode != 0:
        raise PushRejected('exact-SHA update was rejected (the branch is no longer at the authorized commit): '
                           + one_line(push.stderr.replace(token or '\0', '***'), 160))


def delete_ref(api, branch: str) -> str:
    """Delete a staging ref and confirm. Returns '' when it is gone, else a factual description."""
    try:
        api.call('DELETE', f'git/refs/heads/{branch}')
    except PublishError:
        pass
    try:
        left = remote_sha(api, branch)
    except PublishError as error:
        return f'could not confirm whether {branch} still exists ({error})'
    return '' if left is None else f'{branch} still exists at {left}'


def default_git_push(env: dict):
    def push(branch: str, expected: str, new: str, staging: str) -> None:
        git_cas_push(env.get('GIT_REPO_DIR', '.'), f"https://github.com/{env['GITHUB_REPOSITORY']}.git",
                     env.get('GH_PUBLISH_TOKEN'), branch, expected, new, staging)
    return push


def publish(api, env: dict, plan: dict, sleep=time.sleep, git_push=None, evidence: dict | None = None) -> dict:
    evidence = evidence if evidence is not None else {}
    evidence.update({'trusted_baseline': env.get('AUTHORIZED_SHA'), 'steps': []})
    note = evidence['steps'].append
    if env.get('GENERATION_STATUS') != 'success':
        raise PublishError('generation did not complete successfully; nothing is published')
    if not plan['additions'] and not plan['deletions']:
        return {'published': False, 'reason': 'no file changes'}
    git_push = git_push or default_git_push(env)
    base, kind, number, default = plan['base_sha'], env['EVENT_KIND'], env['TARGET_NUMBER'], env['DEFAULT_BRANCH']
    pr_branch = bind_target(api, env, base)  # no mutation before this returns
    suffix = f"{env['RUN_ID']}-{env['RUN_ATTEMPT']}"
    work_branch = f'opencode/issue{number}-{suffix}' if kind == 'issue' else f'opencode/pr{number}-{suffix}'
    check_branch('issue', work_branch, default, protected=False)
    evidence.update({'work_branch': work_branch, 'pr_branch': pr_branch})
    try:
        api.call('POST', 'git/refs', {'ref': f'refs/heads/{work_branch}', 'sha': base})
    except AmbiguousApiError:
        observed = remote_sha(api, work_branch)  # an unknowable state propagates as a hard failure
        evidence['ref_create_observed'] = observed
        if observed != base:
            raise PublishError(f'branch creation outcome unclear; observed remote state: {observed}') from None
    note(f'created {work_branch} at {base}')
    headline = 'opencode: ' + (one_line(env['TASK'], 60) or 'automated change')
    body = '\n'.join([
        f"OpenCode-Run: {env['RUN_URL']}", f"OpenCode-Model: {env['MODEL']}",
        f"OpenCode-Category: {env['CATEGORY']}", f"OpenCode-Session: {env['SESSION_ID']}",
        'OpenCode-Mode: implement', f"Requested-By: {one_line(env['ACTOR_LOGIN'], 60)}"])
    sha = None
    try:
        try:
            data = api.graphql(COMMIT_MUTATION, {'input': {
                'branch': {'repositoryNameWithOwner': env['GITHUB_REPOSITORY'], 'branchName': work_branch},
                'message': {'headline': headline, 'body': body}, 'expectedHeadOid': base,
                'fileChanges': {'additions': plan['additions'],
                                'deletions': [{'path': p} for p in plan['deletions']]}}})
            sha = data['createCommitOnBranch']['commit']['oid']
        except AmbiguousApiError:
            tip = remote_sha(api, work_branch)
            evidence['commit_observed_tip'] = tip
            if tip in (None, base):
                raise PublishError(f'commit was not created (verified: {work_branch} is at {tip})') from None
            sha = tip  # the mutation landed; it is verified below like any other commit
        note(f'commit {sha} on {work_branch}')
        problem = verify_commit(api, sha, env['EXPECT_BOT_LOGIN'], sleep)
    except PublishError:
        leftover = delete_ref(api, work_branch)
        if leftover and sha:
            raise PartialPublication(f'publication failed and {leftover}', work_branch, sha, evidence) from None
        raise
    if problem:
        leftover = delete_ref(api, work_branch)
        if leftover:
            raise PartialPublication(f'commit {sha[:12]} is not GitHub-verified ({problem}) and {leftover}',
                                     work_branch, sha, evidence)
        raise PublishError(f'commit {sha[:12]} is not GitHub-verified ({problem}); publication stopped')
    result = {'published': True, 'branch': work_branch, 'commit': sha, 'verified': True, 'pr': None,
              'warnings': []}
    if kind == 'pr':
        # Last-moment authorization, then an exact-SHA update. Any change aborts.
        try:
            check_pr_target(api, env)
            ahead = api.call('GET', f'compare/{base}...{sha}')
            if ahead.get('status') != 'ahead' or ahead.get('behind_by') != 0:
                raise PublishError('verified commit does not descend from the authorized baseline')
            git_push(pr_branch, base, sha, work_branch)
            note(f'moved {pr_branch} {base} -> {sha}')
        except PublishError as error:
            observed = None
            try:
                observed = remote_sha(api, pr_branch)
            except PublishError as lookup:
                evidence['pr_branch_lookup_error'] = str(lookup)
            evidence['pr_branch_observed_after_failure'] = observed
            leftover = delete_ref(api, work_branch)
            if observed == sha:
                note('update reported failure but the branch is at the new commit')
                result['warnings'].append(f'update call failed ({error}) but {pr_branch} is at {sha}')
            elif observed == base:
                raise PublishError(
                    f'{error}; verified: {pr_branch} is still at the authorized commit {base}'
                    + (f'; {leftover}' if leftover else '')) from None
            else:
                raise PartialPublication(
                    f'{error}; the state of {pr_branch} could not be confirmed as unchanged '
                    f'(observed {observed}); verified commit {sha} exists'
                    + (f'; {leftover}' if leftover else ''), pr_branch, sha, evidence) from None
        else:
            cleanup = delete_ref(api, work_branch)
            if cleanup:
                result['warnings'].append(cleanup)
        result['branch'] = pr_branch
    else:
        try:
            pr = api.call('POST', 'pulls', {
                'title': headline, 'head': work_branch, 'base': default, 'draft': True,
                'body': '\n'.join([
                    f'Draft PR opened by the OpenCode agent for #{number}.', '',
                    f"- Model: `{env['MODEL']}` (category `{env['CATEGORY']}`)",
                    f"- Routing: {one_line(env['ROUTE_REASON'], 300)}",
                    f"- Run: {env['RUN_URL']}", f'- Commit: {sha} (GitHub-verified)', '',
                    'Generation was confirmed complete by the run guard, which does not establish that the '
                    'change is correct. Tests were not run by the publisher; review the CI checks on this PR.',
                    f'Refs #{number}'])})
            result['pr'] = {'number': pr['number'], 'url': pr['html_url']}
        except (PublishError, KeyError) as error:
            raise PartialPublication(
                f'verified commit {sha[:12]} is on branch {work_branch} but the Draft PR could not be opened '
                f'or its creation could not be confirmed: {error}', work_branch, sha, evidence) from None
    where = f"PR #{result['pr']['number']}" if result['pr'] else f"branch `{result['branch']}`"
    notes = []
    if kind == 'pr':  # a new implementation commit voids any earlier OpenCode review
        try:
            reviews = api.call('GET', f'pulls/{number}/reviews?per_page=100')
            prior = review.acceptance_status(reviews, sha, env['EXPECT_BOT_LOGIN'])
            if prior['status'] == 'STALE':
                notes.append(f"The earlier OpenCode review of `{prior['reviewed_commit'][:12]}` is now STALE: "
                             'it does not cover this commit. Request a new review.')
        except PublishError as error:
            result['warnings'].append(f'review staleness check failed: {error}')
    try:
        api.call('POST', f'issues/{number}/comments', {'body': '\n'.join([
            f"OpenCode published commit `{sha[:12]}` (GitHub-verified) to {where}. It did not merge anything; "
            'CI, independent review and external acceptance (Codex or Jules) remain required.', '',
            f"- Implementer model: `{env['MODEL']}`", f"- Implementer session: `{env['SESSION_ID']}`",
            f"- Branch: `{result['branch']}`", f"- Commit: `{sha}`", *notes])})
    except PublishError as error:
        result['warnings'].append(f'status comment failed: {error}')
    return result


def write_evidence(evidence: dict) -> None:
    path = os.environ.get('EVIDENCE_PATH')
    if path:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(evidence, handle, indent=2, default=str)


def main() -> int:
    out = os.environ.get('GITHUB_OUTPUT')
    evidence: dict = {}

    def emit(**values):
        if out:
            with open(out, 'a', encoding='utf-8') as handle:
                for key, value in values.items():
                    handle.write(f'{key}={value}\n')

    try:
        plan = load_manifest(os.environ['ARTIFACT_DIR'])
        env = {key: os.environ[key] for key in (
            'GITHUB_REPOSITORY', 'DEFAULT_BRANCH', 'EVENT_KIND', 'TARGET_NUMBER', 'AUTHORIZED_SHA', 'GENERATION_STATUS',
            'RUN_URL', 'RUN_ID', 'RUN_ATTEMPT', 'ACTOR_LOGIN', 'EXPECT_BOT_LOGIN', 'TASK', 'MODEL', 'CATEGORY',
            'ROUTE_REASON', 'SESSION_ID')}
        env['PR_HEAD_REF'] = os.environ.get('PR_HEAD_REF', '')
        env['GIT_REPO_DIR'] = os.environ.get('GIT_REPO_DIR', '.')
        env['GH_PUBLISH_TOKEN'] = os.environ['GH_PUBLISH_TOKEN']
        if not review.SESSION_RE.match(env['SESSION_ID']):
            raise PublishError('agent session identity is missing or malformed')
        if env['EVENT_KIND'] not in ('issue', 'pr') or not env['TARGET_NUMBER'].isdigit():
            raise PublishError('invalid event context')
        result = publish(Api(env['GH_PUBLISH_TOKEN'], env['GITHUB_REPOSITORY']), env, plan, evidence=evidence)
    except KeyError as error:
        print(f'::error::missing required configuration {error}')
        return 1
    except PartialPublication as error:
        evidence['partial'] = {'message': str(error), 'branch': error.branch, 'commit': error.commit}
        write_evidence(evidence)
        print(f'::error::PARTIAL PUBLICATION: {error}')
        emit(published='partial', branch=error.branch, commit=error.commit)
        return 1
    except (PublishError, OSError, ValueError) as error:
        evidence['failure'] = str(error)
        write_evidence(evidence)
        print(f'::error::OpenCode publication stopped: {error}')
        emit(published='false')
        return 1
    write_evidence(evidence)
    print(json.dumps(result))
    emit(published='true' if result['published'] else 'false',
         **{k: result[k] for k in ('commit', 'branch') if result.get(k)})
    return 0


if __name__ == '__main__':
    sys.exit(main())
