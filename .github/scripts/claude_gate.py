#!/usr/bin/env python3
"""Authorize an `@claude` PR comment before any secret is exposed (claude gate v1.0).

Independent of opencode_gate.py: separate allowlist variable, separate command grammar.
Reads only GitHub-signed event metadata (webhook payload at GITHUB_EVENT_PATH, the PR
object fetched from the API, and the reactions already present on the comment). Nothing in
the comment text grants authority: identity comes from comment.user.id / user.type /
author_association. Fails closed; exit code is always 0 when the script itself runs.
"""
from __future__ import annotations

import json
import os
import re
import sys

COMMENT_EVENTS = {'issue_comment'}
TRUSTED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}
# `@claude` must be the first token, case-sensitive, followed by whitespace and a task.
# Quoted replies, code blocks, mid-text mentions, `@claude-x` and `@claudette` never match.
COMMAND_RE = re.compile(r'\A@claude\s+(?P<task>\S[\s\S]*)\Z')
MAX_TASK_CHARS = 8000
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
CLAIM_REACTION = 'eyes'
CLAIM_USER = 'github-actions[bot]'


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


def extract_task(body: str) -> tuple[str | None, str]:
    match = COMMAND_RE.match((body or '').replace('\r\n', '\n').lstrip('﻿'))
    if not match:
        return None, 'comment does not start with an exact @claude command'
    task = match.group('task').strip()
    if len(task) > MAX_TASK_CHARS:
        return None, 'task exceeds length limit'
    return task, ''


def already_claimed(reactions: list | None) -> bool:
    """A claim is the workflow's own reaction left on the comment by an earlier run."""
    for item in reactions or []:
        user = (item or {}).get('user') or {}
        if item.get('content') == CLAIM_REACTION and user.get('login') == CLAIM_USER \
                and user.get('type') == 'Bot':
            return True
    return False


def decide(event_name: str, event: dict, repository: str, allowed_ids: set[int],
           pr: dict | None, reactions: list | None = None) -> tuple[bool, str]:
    """Return (authorized, reason)."""
    if event_name not in COMMENT_EVENTS:
        return False, f'unsupported event {event_name}'
    if event.get('action') != 'created':
        return False, 'only created comments are accepted'
    if (event.get('repository') or {}).get('full_name') != repository:
        return False, 'event repository mismatch'
    comment = event.get('comment') or {}
    user = comment.get('user') or {}
    if user.get('type') != 'User':
        return False, 'commenter is not a User account'
    if not allowed_ids:
        return False, 'actor allowlist is empty or malformed'
    if user.get('id') not in allowed_ids:
        return False, 'commenter is not in the owner-approved allowlist'
    if comment.get('author_association') not in TRUSTED_ASSOCIATIONS:
        return False, 'commenter lacks owner/member/collaborator association'
    task, reason = extract_task(comment.get('body'))
    if task is None:
        return False, reason
    if not isinstance(comment.get('id'), int):
        return False, 'comment id is invalid'

    issue = event.get('issue') or {}
    if 'pull_request' not in issue:
        return False, '@claude is accepted on pull requests only'
    pr_obj = pr or {}
    if not pr_obj:
        return False, 'PR metadata unavailable for PR comment'
    head_repo = ((pr_obj.get('head') or {}).get('repo') or {}).get('full_name')
    base_repo = ((pr_obj.get('base') or {}).get('repo') or {}).get('full_name')
    if head_repo != repository or base_repo != repository:
        return False, 'pull request head or base is not in this repository'
    if pr_obj.get('state') != 'open' or pr_obj.get('merged') is True:
        return False, 'pull request is not open'
    if pr_obj.get('number') != issue.get('number'):
        return False, 'pull request number does not match the event'
    if not SHA_RE.match(str((pr_obj.get('head') or {}).get('sha', ''))):
        return False, 'pull request head SHA is invalid'
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,255}', str((pr_obj.get('head') or {}).get('ref', ''))):
        return False, 'pull request head ref is invalid'
    if already_claimed(reactions):
        return False, 'duplicate event: this comment was already claimed by an earlier run'
    return True, 'authorized'


def write_output(path: str | None, authorized: bool, reason: str, target: dict) -> None:
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(f'authorized={"true" if authorized else "false"}\n')
        handle.write(f'reason={reason}\n')
        for key, value in target.items():
            handle.write(f'{key}={value}\n')


def _load(path_env: str):
    path = os.environ.get(path_env)
    if path and os.path.exists(path) and os.path.getsize(path):
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    return None


def main() -> int:
    target: dict = {}
    try:
        with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as handle:
            event = json.load(handle)
        pr = _load('PR_JSON_PATH')
        reactions = _load('REACTIONS_JSON_PATH')
        if reactions is not None and not isinstance(reactions, list):
            raise ValueError('reactions payload is not a list')
        authorized, reason = decide(
            os.environ['GITHUB_EVENT_NAME'], event, os.environ['GITHUB_REPOSITORY'],
            parse_allowlist(os.environ.get('ALLOWED_ACTOR_IDS', '')), pr, reactions,
        )
        if authorized:
            target = {
                'number': event['issue']['number'],
                'comment_id': event['comment']['id'],
                'authorized_sha': pr['head']['sha'],
                'head_ref': pr['head']['ref'],
            }
    except (KeyError, ValueError, TypeError, OSError):
        authorized, reason, target = False, 'gate failed closed on malformed input', {}
    write_output(os.environ.get('GITHUB_OUTPUT'), authorized, reason, target)
    print(f'claude gate: authorized={authorized} ({reason})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
