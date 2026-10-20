#!/usr/bin/env python3
"""Authorize an OpenCode comment command before any secret is exposed (gate v1.0).

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

COMMENT_EVENTS = {'issue_comment', 'pull_request_review_comment'}
TRUSTED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}
# Command must be the first token of the comment, case-sensitive, followed by
# whitespace and a non-empty task. Quoted replies, code blocks and mid-text
# mentions never match.
COMMAND_RE = re.compile(r'\A(?:/opencode|/oc)\s+(?P<task>\S[\s\S]*)\Z')
MAX_TASK_CHARS = 8000


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
           pr: dict | None) -> tuple[bool, str, str]:
    """Return (authorized, reason, task)."""
    if event_name not in COMMENT_EVENTS:
        return False, f'unsupported event {event_name}', ''
    if event.get('action') != 'created':
        return False, 'only created comments are accepted', ''
    if (event.get('repository') or {}).get('full_name') != repository:
        return False, 'event repository mismatch', ''
    comment = event.get('comment') or {}
    user = comment.get('user') or {}
    if user.get('type') != 'User':
        return False, 'commenter is not a User account', ''
    if not allowed_ids:
        return False, 'actor allowlist is empty or malformed', ''
    if user.get('id') not in allowed_ids:
        return False, 'commenter is not in the owner-approved allowlist', ''
    if comment.get('author_association') not in TRUSTED_ASSOCIATIONS:
        return False, 'commenter lacks owner/member/collaborator association', ''

    task, reason = extract_task(comment.get('body'))
    if task is None:
        return False, reason, ''

    # Pull request scope: same-repository, open PRs only. Fork head branches
    # contain attacker-controlled content and must never reach the agent.
    if event_name == 'pull_request_review_comment':
        pr_obj = event.get('pull_request') or {}
    elif 'pull_request' in (event.get('issue') or {}):
        pr_obj = pr or {}
        if not pr_obj:
            return False, 'PR metadata unavailable for PR comment', ''
    else:
        pr_obj = None
    if pr_obj is not None:
        head_repo = ((pr_obj.get('head') or {}).get('repo') or {}).get('full_name')
        if head_repo != repository:
            return False, 'pull request head is not in this repository', ''
        if pr_obj.get('state') != 'open':
            return False, 'pull request is not open', ''
    return True, 'authorized', task


def pr_head_ref(event_name: str, event: dict, pr: dict | None) -> str:
    pr_obj = event.get('pull_request') if event_name == 'pull_request_review_comment' else pr
    ref = ((pr_obj or {}).get('head') or {}).get('ref') or ''
    return ref if re.fullmatch(r'[A-Za-z0-9._/-]{1,255}', ref) else ''


def write_output(path: str | None, authorized: bool, reason: str, task: str,
                 head_ref: str = '') -> None:
    lines = [f'authorized={"true" if authorized else "false"}']
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(lines[0] + '\n')
            handle.write(f'reason={reason}\n')
            if authorized and head_ref:
                handle.write(f'head_ref={head_ref}\n')
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
        authorized, reason, task = decide(
            event_name, event, os.environ['GITHUB_REPOSITORY'],
            parse_allowlist(os.environ.get('ALLOWED_ACTOR_IDS', '')), pr,
        )
    except (KeyError, ValueError, OSError):
        authorized, reason, task = False, 'gate failed closed on malformed input', ''
    head_ref = pr_head_ref(event_name, event, pr) if authorized else ''
    write_output(os.environ.get('GITHUB_OUTPUT'), authorized, reason, task, head_ref)
    print(f'opencode gate: authorized={authorized} ({reason})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
