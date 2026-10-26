"""Bounded cross-agent requests. All identity fields must come from GitHub's API.

No name in comment text grants authority. Empty configuration disables bots.
This module does not dispatch agents, merge, or deploy.
"""
import json
import re
import urllib.request

MAX_BOT_REQUESTS = 4
ROOT_COMMAND = re.compile(r'\A(?:/oc|/opencode|@claude|@codex)\s+\S')
BUDGET_REACTIONS = ('rocket', 'hooray', 'heart', '+1')


def root_id(comment, comments, owner_ids):
    roots = [c['id'] for c in comments if type(c.get('id')) is int and c['id'] < comment['id']
             and (c.get('user') or {}).get('type') == 'User'
             and type((c.get('user') or {}).get('id')) is int
             and c['user']['id'] in owner_ids
             and c.get('author_association') in {'OWNER', 'MEMBER', 'COLLABORATOR'}
             and ROOT_COMMAND.match(c.get('body') or '')]
    return max(roots, default=None)


def identities(raw):
    """Owner-managed JSON, with complete numeric and textual identity bindings."""
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not 1 <= len(values) <= 4:
            return []
        seen_ids, seen_apps = set(), set()
        for item in values:
            if set(item) != {'id', 'login', 'app_id', 'app_slug'}:
                return []
            if any(type(item[k]) is not int or item[k] <= 0 for k in ('id', 'app_id')):
                return []
            if not re.fullmatch(r'[A-Za-z0-9-]+(?:\[bot\])?', item['login']):
                return []
            if not re.fullmatch(r'[a-z0-9-]+', item['app_slug']):
                return []
            if item['id'] in seen_ids or item['app_id'] in seen_apps:
                return []
            seen_ids.add(item['id'])
            seen_apps.add(item['app_id'])
        return values
    except (ValueError, TypeError, KeyError):
        return []


def trusted_bot(comment, configured):
    user = comment.get('user') or {}
    app = comment.get('performed_via_github_app') or {}
    return user.get('type') == 'Bot' and any(
        type(user.get('id')) is int and user['id'] == item['id']
        and user.get('login') == item['login']
        and type(app.get('id')) is int and app['id'] == item['app_id']
        and app.get('slug') == item['app_slug'] for item in configured)


def authorize(comment, configured, comments, owner_ids, head):
    """Require a human root, exact HEAD, and a four-request conversation budget.

    Count every bot command after the root, including other providers and denied
    requests. Only a new authorized human command can renew the budget. Callers
    serialize/claim each event before exposing credentials. PR checks remain in
    the provider gate. Comments must be the complete API history, including this
    exact event (a partial or stale history fails closed).
    """
    if not trusted_bot(comment, configured):
        return False, 'bot identity or App is not explicitly trusted'
    cid = comment.get('id')
    if type(cid) is not int or cid <= 0 or not isinstance(comments, list):
        return False, 'missing authenticated comment history'
    ids = [c.get('id') for c in comments]
    if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        return False, 'malformed comment history'
    if not any(c == comment for c in comments):
        return False, 'event no longer matches current comment'
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head):
        return False, 'invalid HEAD'
    if not (comment.get('body') or '').endswith('\nDCIM-Head: ' + head):
        return False, 'bot request does not bind current HEAD'
    root = root_id(comment, comments, owner_ids)
    if root is None:
        return False, 'no authorized human root command'
    count = sum(1 for c in comments if root < c['id'] <= cid
                and (c.get('user') or {}).get('type') == 'Bot'
                and ROOT_COMMAND.match(c.get('body') or ''))
    if count > MAX_BOT_REQUESTS:
        return False, 'bot chain budget exhausted; escalate to owner'
    return True, 'bounded trusted bot request'


def claim(repository, comment_id, root, token, opener=urllib.request.urlopen):
    """GitHub's unique (user, content, comment) reaction is an atomic claim.

    POST returns 201 only on creation, 200 if already present. Ambiguous failures
    fail closed: do not retry generation. A consumed claim needs owner recovery.
    """
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        return False
    if any(type(i) is not int or i <= 0 for i in (comment_id, root)) or not token:
        return False
    def post(cid, content):
        request = urllib.request.Request(
            f'https://api.github.com/repos/{repository}/issues/comments/{cid}/reactions',
            data=json.dumps({'content': content}).encode(), method='POST', headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        with opener(request, timeout=20) as response:
            return response.status
    try:
        if post(comment_id, 'eyes') != 201:
            return False
        # Durable budget belongs to the human root, so deleting bot comments
        # cannot replenish it. Concurrent runs compete for the same unique slots.
        for content in BUDGET_REACTIONS:
            status = post(root, content)
            if status == 201:
                return True
            if status != 200:
                return False
        return False
    except (OSError, ValueError):
        return False
