"""Fail-closed task/release policy, independent of provider dispatch.

This is a policy library, not an authority source. A future trusted controller
must collect facts through authenticated GitHub APIs, persist with compare-and-
swap, and never construct facts from agent comments or artifacts. No controller
or automatic merge/deployment is enabled by this library.
"""
from copy import deepcopy
import hashlib
import json
import re

STATES = ('NEW', 'ASSIGNED', 'IMPLEMENTING', 'CI', 'INDEPENDENT_REVIEW',
          'REMEDIATION', 'ACCEPTED', 'MERGE_READY', 'MERGED', 'STAGING',
          'PRODUCTION', 'VERIFIED', 'BLOCKED', 'FAILED', 'ROLLED_BACK', 'ESCALATED')
NEXT = {
    'NEW': {'ASSIGNED'}, 'ASSIGNED': {'IMPLEMENTING'}, 'IMPLEMENTING': {'CI'},
    'CI': {'INDEPENDENT_REVIEW', 'REMEDIATION'},
    'INDEPENDENT_REVIEW': {'ACCEPTED', 'REMEDIATION'}, 'REMEDIATION': {'CI'},
    'ACCEPTED': {'MERGE_READY'}, 'MERGE_READY': {'MERGED'},
    'MERGED': {'STAGING'}, 'STAGING': {'PRODUCTION', 'ROLLED_BACK'},
    'PRODUCTION': {'VERIFIED', 'ROLLED_BACK'},
}
TERMINAL = {'VERIFIED', 'ROLLED_BACK', 'ESCALATED', 'BLOCKED', 'FAILED'}
PREMERGE = {'ASSIGNED', 'IMPLEMENTING', 'CI', 'INDEPENDENT_REVIEW', 'REMEDIATION',
            'ACCEPTED', 'MERGE_READY'}
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 3600
AGENTS = ('opencode', 'claude', 'codex', 'copilot')
HIGH_RISK = re.compile(r'auth|secur|credential|secret|password|token|migration|'
                       r'infra|power|cooling|control|permission|rbac|sandbox', re.I)


class PolicyError(ValueError):
    pass


def canonical_sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None


def risk(paths):
    """Unknown scope is high risk; only plain documentation is eligible as low."""
    if not paths:
        return 'high'
    for path in paths:
        if not isinstance(path, str) or path.startswith('/') or '\\' in path \
                or any(p in ('', '.', '..') for p in path.split('/')):
            return 'high'
        if HIGH_RISK.search(path) or path.startswith(('.github/', 'scripts/', 'tests/', 'docker')) \
                or path.endswith(('Dockerfile', '.tf', '.yml', '.yaml')):
            return 'high'
    if all(p.startswith('docs/') and p.endswith('.md') for p in paths):
        return 'low'
    if all(p.startswith(('backend/app/', 'frontend/src/', 'edge_collector/')) for p in paths):
        return 'medium'
    return 'high'


def select_agent(observations, failures, capability, now, implementer=None):
    """Fallback uses fresh successful capability probes, never key presence alone."""
    for agent in AGENTS:
        observed = observations.get(agent, {})
        checked = observed.get('checked_at')
        if agent == implementer or failures.get(agent, 0) >= MAX_ATTEMPTS:
            continue
        if observed.get('ready') is not True or type(checked) is not int \
                or not 0 <= now - checked <= 300:
            continue
        if capability not in observed.get('capabilities', []):
            continue
        return agent
    raise PolicyError('ESCALATED: no eligible independently observed agent')


def acceptance_blockers(facts):
    """No acceptance is inferred from text, a passing CI status, or PR author."""
    reasons = []
    head, base = facts.get('head'), facts.get('base')
    if not canonical_sha(head) or not canonical_sha(base):
        reasons.append('missing canonical HEAD/base')
    commits = facts.get('commits')
    commit_range = facts.get('commit_range') or {}
    if not isinstance(commits, list) or not commits:
        reasons.append('missing complete commit evidence')
    else:
        shas = [c.get('sha') for c in commits]
        if any(not canonical_sha(sha) for sha in shas) or len(set(shas)) != len(shas) \
                or shas[-1] != head or base in shas or commit_range.get('base') != base \
                or commit_range.get('head') != head or commit_range.get('complete') is not True \
                or commit_range.get('commits') != shas:
            reasons.append('commit evidence is not bound to complete base/HEAD range')
        if any(c.get('verification', {}).get('verified') is not True
               or c.get('verification', {}).get('reason') != 'valid' for c in commits):
            reasons.append('unsigned or invalid commit')
    for field in ('ci_passed', 'security_passed', 'regression_passed', 'dependencies_accepted',
                  'migration_compatible', 'protections_satisfied', 'conflict_free',
                  'findings_resolved', 'evidence_complete'):
        if facts.get(field) is not True:
            reasons.append(field)
    if facts.get('ci_head') != head or facts.get('ci_base') != base:
        reasons.append('stale CI')
    review = facts.get('review') or {}
    if review.get('head') != head or review.get('base') != base:
        reasons.append('stale independent review')
    if facts.get('implementer') not in AGENTS or review.get('reviewer') not in AGENTS \
            or facts.get('implementer') == review.get('reviewer'):
        reasons.append('review is not independent')
    if review.get('authenticated') is not True or review.get('acceptance_check') is not True:
        reasons.append('missing authenticated acceptance evidence')
    if review.get('verdict') != 'ACCEPT':
        reasons.append('independent reviewer has not accepted')
    if type(review.get('comment_id')) is not int or review['comment_id'] <= 0:
        reasons.append('missing same-PR findings comment')
    if review.get('pr') != facts.get('pr') or type(facts.get('pr')) is not int:
        reasons.append('review targets another PR')
    return reasons


def change_risk(facts):
    files = facts.get('files')
    if not isinstance(files, list) or not files or facts.get('files_complete') is not True:
        return 'high'
    paths = []
    for file in files:
        if not isinstance(file, dict) or file.get('kind') != 'regular' \
                or file.get('status') not in {'added', 'modified', 'removed', 'renamed'}:
            return 'high'
        paths.append(file.get('filename'))
        if file['status'] == 'renamed':
            paths.append(file.get('previous_filename'))
    return risk(paths)


def merge_blockers(facts):
    reasons = acceptance_blockers(facts)
    level = change_risk(facts)
    # Stronger evidence is necessary but not sufficient: initial policy permits
    # automatic merge ONLY for low-risk documentation. No override boolean.
    if level != 'low':
        reasons.append(level + ' risk requires protected owner approval')
    return reasons


def new_task(task_id, pr, head, base, now):
    if type(task_id) is not int or task_id <= 0 or type(pr) is not int or pr <= 0 \
            or not canonical_sha(head) or not canonical_sha(base) or type(now) is not int:
        raise PolicyError('invalid task binding')
    return dict(task_id=task_id, pr=pr, head=head, base=base, state='NEW', revision=0,
                attempts=0, created_at=now, updated_at=now, events=[], audit_hash='0' * 64)


def transition(task, target, event_id, expected_revision, facts, now):
    """Pure transition; persistent callers MUST atomically CAS expected_revision.

    Exact event repeats are idempotent. An event cannot be reused with a new
    payload. Changed HEAD/base discards acceptance and restarts CI. Terminal tasks
    require a new task identity rather than silently renewing their retry budget.
    """
    if target not in STATES or not isinstance(event_id, str) or not event_id \
            or len(event_id) > 200 or type(now) is not int:
        raise PolicyError('invalid transition')
    if not isinstance(facts, dict):
        raise PolicyError('facts must be a JSON object')
    payload = dict(target=target, facts=facts)
    try:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        raise PolicyError('facts must contain canonical JSON values') from None
    for event in task['events']:
        if event['id'] == event_id:
            if event['digest'] != digest:
                raise PolicyError('event replay with different payload')
            return deepcopy(task)
    if expected_revision != task['revision']:
        raise PolicyError('stale revision; compare-and-swap required')
    if task['state'] in TERMINAL or now < task['updated_at']:
        raise PolicyError('terminal task or backwards clock')
    result = deepcopy(task)
    if now - task['created_at'] > TIMEOUT_SECONDS:
        target = 'ESCALATED'
    elif target not in {'BLOCKED', 'FAILED', 'ESCALATED'}:
        if target not in NEXT.get(task['state'], set()):
            raise PolicyError('illegal lifecycle transition')
        if facts.get('pr') != task['pr']:
            raise PolicyError('task cannot switch PR')
        if facts.get('head') != task['head'] or facts.get('base') != task['base']:
            if task['state'] not in PREMERGE:
                raise PolicyError('changed commits require a new task before assignment or after merge')
            if not canonical_sha(facts.get('head')) or not canonical_sha(facts.get('base')):
                raise PolicyError('invalid changed commit binding')
            result.update(head=facts['head'], base=facts['base'])
            target = 'CI'
        elif target == 'ASSIGNED':
            if facts.get('implementer') not in AGENTS:
                raise PolicyError('no eligible task owner')
            result['implementer'] = facts['implementer']
        elif target == 'IMPLEMENTING':
            if facts.get('implementer') != task.get('implementer'):
                raise PolicyError('task owner mismatch')
        elif target == 'INDEPENDENT_REVIEW':
            if facts.get('ci_passed') is not True:
                raise PolicyError('CI must pass before review')
        elif target in {'ACCEPTED', 'MERGE_READY', 'MERGED'}:
            blockers = acceptance_blockers(facts) if target == 'ACCEPTED' else merge_blockers(facts)
            if facts.get('implementer') != task.get('implementer') or blockers:
                raise PolicyError('release evidence rejected')
            if target == 'MERGED' and facts.get('github_merged') is not True:
                raise PolicyError('GitHub has not confirmed merge')
        elif target in {'STAGING', 'PRODUCTION', 'VERIFIED'}:
            # No real release adapter has been validated. Never permit a caller
            # to activate deployment by supplying an agent-written boolean.
            raise PolicyError('BLOCKED: deployment adapter is not activated')
        elif target == 'ROLLED_BACK':
            if facts.get('rollback_verified') is not True:
                raise PolicyError('rollback has not been verified')
    if target == 'CI':
        result['attempts'] += 1
        if result['attempts'] > MAX_ATTEMPTS:
            target = 'ESCALATED'
    result.update(state=target, revision=task['revision'] + 1, updated_at=now)
    entry = dict(id=event_id, digest=digest, previous=task['audit_hash'], state=target,
                 revision=result['revision'], head=result['head'], base=result['base'], at=now)
    result['audit_hash'] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    result['events'].append(entry)
    return result
