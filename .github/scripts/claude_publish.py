#!/usr/bin/env python3
"""Read-only execution / trusted publication split for the Claude agent (claude publish v1.0).

Runs only from the default-branch workflow commit, never from the PR checkout.
Modes:
  preflight  verify the live PR against the gate's authorization.
  prepare    (read-only job, before Claude) pin the PR checkout to the authorized commit, record its
             tree and remove executable agent configuration from it.
  collect    (read-only job, after Claude) turn the working-tree edits into a bounded change manifest.
  publish    (publisher job, no Claude, no PR code) re-validate everything the manifest says, build one
             commit through the Git Data API and fast-forward the existing PR head branch to it.

The manifest is untrusted input to `publish`. The ref update is never forced, so GitHub itself rejects
it if the head moved, and it can only move a ref that exists: this script never creates a branch.
Every failure exits 1 before any write that matters.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

APPROVED_BASE_REF = 'main'
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
REQUEST_TIMEOUT = 20
MAX_FILES = 100
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 5 << 20
MAX_PATH_CHARS = 500
FILE_MODES = {'100644', '100755'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.pytest_cache'}
# Executable or startup configuration: removed from the checkout before Claude runs and never published.
CONFIG_COMPONENTS = {'.git', '.claude', '.husky'}
CONFIG_ROOT_FILES = {'.mcp.json', '.claude.json', '.gitmodules', '.ripgreprc', 'bunfig.toml', '.npmrc',
                     'claude.local.md'}
# Never published even though Claude may read them: workflows, scripts and agent instructions.
PROTECTED_NAMES = {'claude.md', 'claude.local.md'}


class Denied(Exception):
    pass


# ---------------------------------------------------------------------------- paths


def is_executable_config(path: str) -> bool:
    parts = [p.lower() for p in path.split('/')]
    return bool(CONFIG_COMPONENTS & set(parts)) or (len(parts) == 1 and parts[0] in CONFIG_ROOT_FILES)


def is_protected(path: str) -> bool:
    parts = [p.lower() for p in path.split('/')]
    return parts[0] == '.github' or is_executable_config(path) or parts[-1] in PROTECTED_NAMES


def validate_path(path: object) -> str:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise Denied('invalid change path')
    if path.startswith('/') or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise Denied(f'unsafe change path {path!r}')
    if any(part in ('', '.', '..') for part in path.split('/')):
        raise Denied(f'unsafe change path {path!r}')
    if is_protected(path):
        raise Denied(f'change touches a protected path: {path}')
    return path


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()  # noqa: S324 - git object id


# ---------------------------------------------------------------------------- environment


def target_from_env() -> dict:
    target = {
        'repository': os.environ.get('CLAUDE_GUARD_REPOSITORY', ''),
        'number': os.environ.get('CLAUDE_GUARD_NUMBER', ''),
        'head_ref': os.environ.get('CLAUDE_GUARD_HEAD_REF', ''),
        'authorized_sha': os.environ.get('CLAUDE_GUARD_AUTHORIZED_SHA', ''),
        'comment_id': os.environ.get('CLAUDE_GUARD_COMMENT_ID', ''),
    }
    if not re.fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', target['repository']) \
            or not target['number'].isdigit() \
            or not SHA_RE.match(target['authorized_sha']) \
            or not re.fullmatch(r'[A-Za-z0-9._/-]{1,255}', target['head_ref']) \
            or (target['comment_id'] and not target['comment_id'].isdigit()):
        raise Denied('guard target is missing or malformed')
    target['number'] = int(target['number'])
    return target


def runner_path(env_name: str, default_rel: str) -> str:
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    base = os.environ.get('RUNNER_TEMP')
    if not base:
        raise Denied(f'{env_name} and RUNNER_TEMP are both unset')
    return os.path.join(base, default_rel)


def workdir() -> str:
    explicit = os.environ.get('CLAUDE_PUBLISH_WORKDIR')
    if explicit:
        return explicit
    workspace = os.environ.get('GITHUB_WORKSPACE')
    if not workspace:
        raise Denied('CLAUDE_PUBLISH_WORKDIR and GITHUB_WORKSPACE are both unset')
    return os.path.join(workspace, 'pr')


# ---------------------------------------------------------------------------- GitHub API


def api(method: str, path: str, body: object = None) -> tuple[int, object]:
    token = os.environ.get('CLAUDE_GUARD_TOKEN', '')
    if not token:
        raise Denied('guard token unavailable')
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == 'GET':
            return 404, None
        raise Denied(f'GitHub API returned {error.code} for {method} {path}')


def check_live(pr: object, base_branch: object, head_ref_obj: object, target: dict) -> None:
    """Raise Denied unless the live state still matches the authorization exactly."""
    if not isinstance(pr, dict):
        raise Denied('pull request unavailable')
    repo, expected = target['repository'], target['authorized_sha']
    head, base = pr.get('head') or {}, pr.get('base') or {}
    if pr.get('state') != 'open' or pr.get('merged') is True:
        raise Denied('pull request is closed or merged')
    if pr.get('number') != target['number']:
        raise Denied('pull request number changed')
    if (head.get('repo') or {}).get('full_name') != repo or (base.get('repo') or {}).get('full_name') != repo:
        raise Denied('pull request head or base left this repository')
    if head.get('ref') != target['head_ref']:
        raise Denied('pull request head branch changed')
    if base.get('ref') != APPROVED_BASE_REF:
        raise Denied(f'pull request base is not the approved {APPROVED_BASE_REF} branch')
    if not isinstance(base_branch, dict) or base_branch.get('name') != APPROVED_BASE_REF \
            or base_branch.get('protected') is not True:
        raise Denied(f'approved base {APPROVED_BASE_REF} is not protected')
    if head.get('sha') != expected:
        raise Denied('pull request head moved since authorization')
    if not isinstance(head_ref_obj, dict) or head_ref_obj.get('ref') != f"refs/heads/{target['head_ref']}" \
            or (head_ref_obj.get('object') or {}).get('sha') != expected:
        raise Denied('head branch is missing or not at the authorized commit; refusing to create or move it')


def live_check(target: dict) -> None:
    repo = target['repository']
    _s, pr = api('GET', f"/repos/{repo}/pulls/{target['number']}")
    _s, base_branch = api('GET', f'/repos/{repo}/branches/{APPROVED_BASE_REF}')
    _s, head_ref_obj = api('GET', f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(target['head_ref'], safe='/')}")
    check_live(pr, base_branch, head_ref_obj, target)


# ---------------------------------------------------------------------------- read-only job


def git(*args: str) -> bytes:
    return subprocess.run(['git', *args], check=True, capture_output=True, timeout=120).stdout


def mode_preflight() -> int:
    live_check(target_from_env())
    print('claude publish: preflight ok')
    return 0


def mode_prepare() -> int:
    target, root = target_from_env(), workdir()
    if git('-C', root, 'rev-parse', 'HEAD').decode().strip() != target['authorized_sha']:
        raise Denied('PR checkout is not at the authorized commit')
    snapshot = {}
    for entry in git('-C', root, 'ls-files', '-s', '-z').split(b'\0'):
        if entry:
            meta, path = entry.decode().split('\t', 1)
            mode, sha, _stage = meta.split(' ')
            snapshot[path] = [mode, sha]
    with open(runner_path('CLAUDE_PUBLISH_SNAPSHOT', 'claude-snapshot.json'), 'w', encoding='utf-8') as handle:
        json.dump(snapshot, handle)
    removed = 0
    for path in snapshot:
        if is_executable_config(path) and os.path.lexists(os.path.join(root, path)):
            os.remove(os.path.join(root, path))
            removed += 1
    print(f'claude publish: prepared {len(snapshot)} tracked paths; removed {removed} executable config files')
    return 0


def emit_output(name: str, value: str) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(f'{name}={value}\n')


def mode_collect() -> int:
    target, root = target_from_env(), workdir()
    with open(runner_path('CLAUDE_PUBLISH_SNAPSHOT', 'claude-snapshot.json'), encoding='utf-8') as handle:
        snapshot = json.load(handle)
    seen: set[str] = set()
    changes, total, skipped = [], 0, 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files + [d for d in dirs if os.path.islink(os.path.join(current, d))]):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            seen.add(rel)
            info = os.lstat(full)
            if os.path.islink(full):
                if is_protected(rel):
                    continue
                if snapshot.get(rel) != ['120000', blob_sha(os.readlink(full).encode())]:
                    raise Denied(f'symlink added or changed: {rel}')
                continue
            if not os.path.isfile(full):
                raise Denied(f'special file in working tree: {rel}')
            if info.st_size > MAX_FILE_BYTES:
                raise Denied(f'file too large: {rel}')
            with open(full, 'rb') as handle:
                data = handle.read()
            mode = '100755' if info.st_mode & 0o100 else '100644'
            if snapshot.get(rel) == [mode, blob_sha(data)]:
                continue
            if is_protected(rel):
                skipped += 1
                continue
            total += len(data)
            changes.append({'op': 'upsert', 'path': rel, 'mode': mode,
                            'content_b64': base64.b64encode(data).decode()})
    for rel, (mode, _sha) in sorted(snapshot.items()):
        if rel in seen or is_protected(rel) or mode == '160000':
            continue
        if mode not in FILE_MODES:
            raise Denied(f'symlink deleted: {rel}')
        changes.append({'op': 'delete', 'path': rel})
    if len(changes) > MAX_FILES or total > MAX_TOTAL_BYTES:
        raise Denied('change set exceeds the publishing limits')
    manifest = {'authorized_sha': target['authorized_sha'], 'head_ref': target['head_ref'],
                'number': target['number'], 'changes': sorted(changes, key=lambda c: c['path'])}
    out = runner_path('CLAUDE_PUBLISH_CHANGES', 'claude-changes/changes.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle)
    emit_output('has_changes', 'true' if changes else 'false')
    print(f'claude publish: {len(changes)} publishable changes; {skipped} protected-path edits dropped')
    return 0


# ---------------------------------------------------------------------------- publisher job


def validate_manifest(doc: object, target: dict) -> list[dict]:
    """Treat the manifest as hostile: bind it to this authorization and bound every field."""
    if not isinstance(doc, dict) or set(doc) != {'authorized_sha', 'head_ref', 'number', 'changes'}:
        raise Denied('manifest has unexpected shape')
    if doc['authorized_sha'] != target['authorized_sha'] or doc['head_ref'] != target['head_ref'] \
            or doc['number'] != target['number']:
        raise Denied('manifest does not match this authorization')
    changes = doc['changes']
    if not isinstance(changes, list) or not changes or len(changes) > MAX_FILES:
        raise Denied('manifest change count is outside the allowed range')
    seen, total, out = set(), 0, []
    for change in changes:
        if not isinstance(change, dict):
            raise Denied('malformed change entry')
        path = validate_path(change.get('path'))
        if path in seen:
            raise Denied(f'duplicate change path {path}')
        seen.add(path)
        if change.get('op') == 'delete' and set(change) == {'op', 'path'}:
            out.append({'op': 'delete', 'path': path})
        elif change.get('op') == 'upsert' and set(change) == {'op', 'path', 'mode', 'content_b64'} \
                and change['mode'] in FILE_MODES and isinstance(change['content_b64'], str):
            try:
                data = base64.b64decode(change['content_b64'], validate=True)
            except (binascii.Error, ValueError):
                raise Denied(f'invalid content encoding for {path}')
            if len(data) > MAX_FILE_BYTES:
                raise Denied(f'file too large: {path}')
            total += len(data)
            out.append({'op': 'upsert', 'path': path, 'mode': change['mode'], 'content_b64': change['content_b64']})
        else:
            raise Denied(f'unsupported change for {path}')
    if total > MAX_TOTAL_BYTES:
        raise Denied('manifest exceeds the total size limit')
    return out


def mode_publish() -> int:
    target = target_from_env()
    with open(runner_path('CLAUDE_PUBLISH_CHANGES', 'claude-changes/changes.json'), encoding='utf-8') as handle:
        changes = validate_manifest(json.load(handle), target)
    live_check(target)
    repo, sha = target['repository'], target['authorized_sha']
    _s, parent = api('GET', f'/repos/{repo}/git/commits/{sha}')
    base_tree = ((parent or {}).get('tree') or {}).get('sha', '') if isinstance(parent, dict) else ''
    if not SHA_RE.match(base_tree):
        raise Denied('authorized commit tree unavailable')
    entries = []
    for change in changes:
        if change['op'] == 'delete':
            entries.append({'path': change['path'], 'mode': '100644', 'type': 'blob', 'sha': None})
            continue
        _s, blob = api('POST', f'/repos/{repo}/git/blobs', {'content': change['content_b64'], 'encoding': 'base64'})
        blob_id = (blob or {}).get('sha', '') if isinstance(blob, dict) else ''
        if not SHA_RE.match(blob_id):
            raise Denied('blob creation returned no sha')
        entries.append({'path': change['path'], 'mode': change['mode'], 'type': 'blob', 'sha': blob_id})
    _s, tree = api('POST', f'/repos/{repo}/git/trees', {'base_tree': base_tree, 'tree': entries})
    tree_id = (tree or {}).get('sha', '') if isinstance(tree, dict) else ''
    if not SHA_RE.match(tree_id):
        raise Denied('tree creation returned no sha')
    comment = f" from comment {target['comment_id']}" if target['comment_id'] else ''
    message = f"claude: apply requested changes{comment}\n\nPublished by the claude-agent publisher job."
    # No author or committer is supplied, so GitHub signs the commit for the workflow token.
    _s, commit = api('POST', f'/repos/{repo}/git/commits', {'message': message, 'tree': tree_id, 'parents': [sha]})
    commit_id = (commit or {}).get('sha', '') if isinstance(commit, dict) else ''
    if not SHA_RE.match(commit_id) or ((commit or {}).get('verification') or {}).get('verified') is not True:
        raise Denied('commit was not created as a GitHub-verified signed commit; ref not updated')
    live_check(target)  # last read before the write; the non-forced update below is the atomic guard
    ref_path = f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(target['head_ref'], safe='/')}"
    api('PATCH', ref_path, {'sha': commit_id, 'force': False})
    _s, after = api('GET', ref_path.replace('/git/refs/', '/git/ref/'))
    if (after or {}).get('object', {}).get('sha') != commit_id:
        raise Denied('head branch is not at the published commit after update')
    print(f'claude publish: fast-forwarded {target["head_ref"]} {sha[:10]} -> {commit_id[:10]} ({len(changes)} files)')
    return 0


MODES = {'preflight': mode_preflight, 'prepare': mode_prepare, 'collect': mode_collect, 'publish': mode_publish}


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2 or argv[1] not in MODES:
            raise Denied('unknown mode')
        return MODES[argv[1]]()
    except Denied as error:
        print(f'::error::claude publish: {error}', file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f'::error::claude publish: failed closed ({type(error).__name__})', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
