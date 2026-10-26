#!/usr/bin/env python3
"""Authorize an OpenCode comment command before any secret is exposed (gate v4.0).

Reads only GitHub-signed event metadata (the webhook payload at GITHUB_EVENT_PATH
and, for PR conversation comments, the PR object fetched from the GitHub API).
Nothing in the comment text can grant authority: identity comes from
comment.user.id / comment.user.type / comment.author_association.

Exit code is always 0 when the script itself runs; the decision is written to
GITHUB_OUTPUT as authorized=true|false. Any parse problem fails closed.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_authority

# pull_request_review_comment is deliberately absent: GitHub runs that event's workflow
# file from the PR merge ref, so PR content could rewrite this gate.
COMMENT_EVENTS = {'issue_comment'}
TRUSTED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}
# Command must be the first token of the comment, case-sensitive, followed by
# whitespace and a non-empty task. Quoted replies, code blocks and mid-text
# mentions never match.
COMMAND_RE = re.compile(r'\A(?:/opencode|/oc)\s+(?P<task>\S[\s\S]*)\Z')
MAX_TASK_CHARS = 8000
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def parse_allowlist(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        if not item.isdigit():
            return set()  # malformed list: fail closed
        ids.add(int(item))
    return ids


REVIEW_RE = re.compile(r'\Areview(?:\s+(?P<focus>[\s\S]*))?\Z')


def split_mode(task: str) -> tuple[str, str]:
    """`review [focus]` selects the read-only reviewer; anything else is implementation."""
    match = REVIEW_RE.match(task)
    if match:
        return 'review', (match.group('focus') or '').strip()
    return 'implement', task


def extract_task(body: str) -> tuple[str | None, str]:
    match = COMMAND_RE.match((body or '').replace('\r\n', '\n').lstrip('﻿'))
    if not match:
        return None, 'comment is not an exact /oc or /opencode command'
    task = match.group('task').strip()
    if not task:
        return None, 'command has no task'
    if len(task) > MAX_TASK_CHARS:
        return None, 'task exceeds length limit'
    if task.startswith('/'):
        return None, 'task must not start with a slash command'
    return task, ''


def decide(event_name: str, event: dict, repository: str, allowed_ids: set[int],
           pr: dict | None, bots: list | None = None,
           comments: list | None = None) -> tuple[bool, str, str]:
    """Return (authorized, reason, task)."""
    if event_name not in COMMENT_EVENTS:
        return False, f'unsupported event {event_name}', ''
    if event.get('action') != 'created':
        return False, 'only created comments are accepted', ''
    if (event.get('repository') or {}).get('full_name') != repository:
        return False, 'event repository mismatch', ''
    comment = event.get('comment') or {}
    user = comment.get('user') or {}
    is_bot = user.get('type') == 'Bot'
    if is_bot:
        if 'pull_request' not in (event.get('issue') or {}):
            return False, 'bot commands require an existing PR', ''
        ok, reason = agent_authority.authorize(comment, bots or [], comments,
                                               allowed_ids, ((pr or {}).get('head') or {}).get('sha'))
        if not ok:
            return False, reason, ''
    elif user.get('type') != 'User':
        return False, 'commenter is not a User account', ''
    if not allowed_ids:
        return False, 'actor allowlist is empty or malformed', ''
    if not is_bot and user.get('id') not in allowed_ids:
        return False, 'commenter is not in the owner-approved allowlist', ''
    if not is_bot and comment.get('author_association') not in TRUSTED_ASSOCIATIONS:
        return False, 'commenter lacks owner/member/collaborator association', ''

    task, reason = extract_task(comment.get('body'))
    if task is None:
        return False, reason, ''

    # Pull request scope: same-repository, open PRs only. Fork head branches
    # contain attacker-controlled content and must never reach the agent.
    if 'pull_request' in (event.get('issue') or {}):
        pr_obj = pr or {}
        if not pr_obj:
            return False, 'PR metadata unavailable for PR comment', ''
    else:
        pr_obj = None
    if pr_obj is not None:
        head_repo = ((pr_obj.get('head') or {}).get('repo') or {}).get('full_name')
        base_repo = ((pr_obj.get('base') or {}).get('repo') or {}).get('full_name')
        if head_repo != repository or base_repo != repository:
            return False, 'pull request head or base is not in this repository', ''
        if pr_obj.get('state') != 'open' or pr_obj.get('merged') is True:
            return False, 'pull request is not open', ''
        if pr_obj.get('number') != (event.get('issue') or {}).get('number'):
            return False, 'pull request number does not match the event', ''
        if not SHA_RE.match(str((pr_obj.get('head') or {}).get('sha', ''))):
            return False, 'pull request head SHA is invalid', ''
    return True, 'authorized', task


def pr_head_ref(event_name: str, event: dict, pr: dict | None) -> str:
    ref = ((pr or {}).get('head') or {}).get('ref') or ''
    return ref if re.fullmatch(r'[A-Za-z0-9._/-]{1,255}', ref) else ''


def binding(event: dict, pr: dict | None, event_sha: str, mode: str = 'implement') -> dict:
    """Trusted target identity, fixed at authorization time and never taken from artifacts."""
    number = (event.get('issue') or {}).get('number')
    if 'pull_request' in (event.get('issue') or {}):
        base_sha = ((pr or {}).get('base') or {}).get('sha', '')
        if not SHA_RE.match(str(base_sha)):
            raise ValueError('pull request base SHA is invalid')
        return {'kind': 'pr', 'number': number, 'authorized_sha': (pr or {})['head']['sha'],
                'base_sha': base_sha, 'head_ref': pr_head_ref('issue_comment', event, pr), 'mode': mode}
    if mode == 'review':
        raise ValueError('review applies to pull requests only')
    return {'kind': 'issue', 'number': number, 'authorized_sha': event_sha, 'base_sha': '', 'head_ref': '',
            'mode': mode}


def write_output(path: str | None, authorized: bool, reason: str, task: str,
                 target: dict | None = None) -> None:
    lines = [f'authorized={"true" if authorized else "false"}']
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(lines[0] + '\n')
            handle.write(f'reason={reason}\n')
            for key, value in (target or {}).items():
                handle.write(f'{key}={value}\n')
            if task:
                delimiter = f'EOF_{secrets.token_hex(16)}'
                handle.write(f'task<<{delimiter}\n{task}\n{delimiter}\n')


def main() -> int:
    event_name = ''
    event: dict = {}
    pr = None
    try:
        with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as handle:
            event = json.load(handle)
        pr_path = os.environ.get('PR_JSON_PATH')
        if pr_path and os.path.exists(pr_path) and os.path.getsize(pr_path):
            with open(pr_path, encoding='utf-8') as handle:
                pr = json.load(handle)
        event_name = os.environ['GITHUB_EVENT_NAME']
        comments = None
        if os.environ.get('AGENT_COMMENTS_PATH'):
            with open(os.environ['AGENT_COMMENTS_PATH'], encoding='utf-8') as handle:
                comments = json.load(handle)
        authorized, reason, task = decide(
            event_name, event, os.environ['GITHUB_REPOSITORY'],
            parse_allowlist(os.environ.get('ALLOWED_ACTOR_IDS', '')), pr,
            agent_authority.identities(os.environ.get('TRUSTED_AGENT_IDENTITIES', '')), comments,
        )
    except (KeyError, ValueError, OSError):
        authorized, reason, task = False, 'gate failed closed on malformed input', ''
    target = None
    if authorized:
        try:
            mode, task = split_mode(task)
            target = binding(event, pr, os.environ.get('GITHUB_SHA', ''), mode)
            if not SHA_RE.match(target['authorized_sha']) or not isinstance(target['number'], int) \
                    or (target['kind'] == 'pr' and not target['head_ref']):
                raise ValueError('invalid binding')
        except (KeyError, ValueError, TypeError):
            authorized, reason, task, target = False, 'could not establish a trusted baseline', '', None
    if authorized and (event.get('comment', {}).get('user') or {}).get('type') == 'Bot':
        if not agent_authority.claim(os.environ['GITHUB_REPOSITORY'], event['comment'].get('id'),
                                     agent_authority.root_id(event['comment'], comments,
                                         parse_allowlist(os.environ.get('ALLOWED_ACTOR_IDS', ''))),
                                     os.environ.get('GITHUB_TOKEN', '')):
            authorized, reason, task, target = False, 'bot request already claimed or claim failed', '', None
    write_output(os.environ.get('GITHUB_OUTPUT'), authorized, reason, task, target)
    print(f'opencode gate: authorized={authorized} ({reason})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
