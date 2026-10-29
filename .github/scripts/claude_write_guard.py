#!/usr/bin/env python3
"""Fail-closed write boundary for the Claude agent (claude write guard v1.0).

Runs from a trusted copy of the default-branch workflow commit (never from the PR checkout).
Modes:
  preflight  verify the live PR against the gate's authorization; exit 1 on any mismatch.
  hook       Claude Code PreToolUse hook for the action's file-ops tools; exit 2 blocks the write.
  record     Claude Code PostToolUse hook; advances the expected head only across our own commit.
  verify     final step; exit 1 if any write was blocked-open (state locked).

Every write is authorized against live state immediately before it happens: the PR is open and
unmerged, same repository, base is the approved protected branch, the head branch exists and
sits exactly on the head this guard expects. Any error, timeout or malformed input denies.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

APPROVED_BASE_REF = 'main'
WRITE_TOOL_PREFIX = 'mcp__github_file_ops__'
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
REQUEST_TIMEOUT = 15
DENY = 2  # Claude Code treats only exit code 2 from a PreToolUse hook as blocking.


class Denied(Exception):
    pass


def target_from_env() -> dict:
    target = {
        'repository': os.environ.get('CLAUDE_GUARD_REPOSITORY', ''),
        'number': os.environ.get('CLAUDE_GUARD_NUMBER', ''),
        'head_ref': os.environ.get('CLAUDE_GUARD_HEAD_REF', ''),
        'authorized_sha': os.environ.get('CLAUDE_GUARD_AUTHORIZED_SHA', ''),
        'state_path': os.environ.get('CLAUDE_GUARD_STATE', ''),
    }
    if not re.fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', target['repository']) \
            or not target['number'].isdigit() \
            or not SHA_RE.match(target['authorized_sha']) \
            or not re.fullmatch(r'[A-Za-z0-9._/-]{1,255}', target['head_ref']) \
            or not target['state_path']:
        raise Denied('guard target is missing or malformed')
    target['number'] = int(target['number'])
    return target


def api_get(path: str) -> tuple[int, object]:
    token = os.environ.get('CLAUDE_GUARD_TOKEN', '')
    if not token:
        raise Denied('guard token unavailable')
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    request = urllib.request.Request(base + path, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        raise Denied(f'GitHub API returned {error.code} for {path}')


def check_live(pr: object, base_branch: object, head_ref_obj: object, target: dict,
               expected_head: str) -> None:
    """Raise Denied unless the live state still matches the authorization exactly."""
    if not isinstance(pr, dict):
        raise Denied('pull request unavailable')
    repo = target['repository']
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
    if head.get('sha') != expected_head:
        raise Denied('pull request head moved since authorization')
    if not isinstance(head_ref_obj, dict) or head_ref_obj.get('ref') != f"refs/heads/{target['head_ref']}" \
            or (head_ref_obj.get('object') or {}).get('sha') != expected_head:
        raise Denied('head branch is missing or not at the expected commit; refusing to create or move it')


def live_check(target: dict, expected_head: str) -> None:
    repo, number = target['repository'], target['number']
    _status, pr = api_get(f'/repos/{repo}/pulls/{number}')
    _status, base_branch = api_get(f'/repos/{repo}/branches/{APPROVED_BASE_REF}')
    ref_path = urllib.parse.quote(target['head_ref'], safe='/')
    _status, head_ref_obj = api_get(f'/repos/{repo}/git/ref/heads/{ref_path}')
    check_live(pr, base_branch, head_ref_obj, target, expected_head)


def read_state(target: dict) -> dict:
    try:
        with open(target['state_path'], encoding='utf-8') as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {'expected_head': target['authorized_sha'], 'locked': False}
    if not isinstance(state, dict) or not SHA_RE.match(str(state.get('expected_head', ''))):
        raise Denied('guard state is corrupt')
    return state


def write_state(target: dict, state: dict) -> None:
    tmp = target['state_path'] + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)
    os.replace(tmp, target['state_path'])


def mode_preflight() -> int:
    live_check(target_from_env(), target_from_env()['authorized_sha'])
    print('claude write guard: preflight ok')
    return 0


def mode_hook() -> int:
    payload = json.load(sys.stdin)
    tool = payload.get('tool_name') if isinstance(payload, dict) else None
    if not isinstance(tool, str) or not tool.startswith(WRITE_TOOL_PREFIX):
        raise Denied('unrecognized tool routed to the write guard')
    target = target_from_env()
    state = read_state(target)
    if state.get('locked'):
        raise Denied('write boundary locked after an unexpected head change')
    live_check(target, state['expected_head'])
    return 0


def mode_record() -> int:
    target = target_from_env()
    try:
        state = read_state(target)
        _status, pr = api_get(f"/repos/{target['repository']}/pulls/{target['number']}")
        new_head = ((pr or {}).get('head') or {}).get('sha', '')
        if new_head != state['expected_head']:
            parents = []
            if SHA_RE.match(new_head):
                _status, commit = api_get(f"/repos/{target['repository']}/git/commits/{new_head}")
                parents = [p.get('sha') for p in (commit or {}).get('parents', [])]
            if parents == [state['expected_head']]:
                state['expected_head'] = new_head
            else:
                state['locked'] = True
        write_state(target, state)
    except Exception:  # noqa: BLE001 - any doubt after a write locks every later write
        write_state(target, {'expected_head': target['authorized_sha'], 'locked': True})
    return 0


def mode_verify() -> int:
    try:
        state = read_state(target_from_env())
    except Denied:
        state = {'locked': True}
    if state.get('locked'):
        print('::error::claude write guard locked: the PR head changed outside this run')
        return 1
    return 0


MODES = {'preflight': mode_preflight, 'hook': mode_hook, 'record': mode_record, 'verify': mode_verify}


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) == 2 else ''
    fail_code = DENY if mode == 'hook' else 1
    try:
        if mode not in MODES:
            raise Denied('unknown guard mode')
        return MODES[mode]()
    except Denied as error:
        print(f'claude write guard: {error}', file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f'claude write guard: failed closed ({type(error).__name__})', file=sys.stderr)
    return fail_code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
