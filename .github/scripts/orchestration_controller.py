"""Default-disabled collaboration reconciler. Never merges, deploys or creates PRs.

Journal commits are GitHub-signed; Contents API blob SHA supplies compare-and-swap.
Delivery intent is persisted BEFORE an App-authenticated fresh comment. Ambiguous
POSTs are recovered by exact nonce/body, never blindly retried. No model receives
this process's token. Native adapters must emit authenticated result artifacts.
"""
import base64
from copy import deepcopy, copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from acceptance_api import GitHub
from acceptance_core import Rejected, require, decode, digest, binding, provider_identity, same_binding, jobs_and_checks
from acceptance_gate import load_policy, verify_check
from agent_authority import parse_command, comment_binding
from orchestration_provenance import execution_policy

POLICY = '.github/agents/collaboration-policy.json'
WORKFLOW = '.github/workflows/agent-collaboration.yml'
SCRIPT = '.github/scripts/orchestration_controller.py'
TERMINAL = {'ACCEPTED', 'BLOCKED', 'ESCALATED'}


def timestamp(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())


def signed(commit):
    verification = commit.get('commit', {}).get('verification', {})
    require(verification.get('verified') is True and verification.get('reason') == 'valid',
            'state commit is not GitHub-verified signed')


class Store:
    def __init__(self, api, policy):
        require(policy.get('state_branch') == 'automation/collaboration-state'
                and policy['state_branch'] != policy.get('default_branch')
                and policy.get('state_path') == '.github/agents/state.json',
                'journal branch/path is outside the fixed state namespace')
        self.api, self.policy = api, policy

    def read(self):
        p = self.policy
        branch = self.api.request('/branches/' + p['state_branch'])
        require(branch.get('protected') is True, 'state branch must be protected before activation')
        sha = branch['commit']['sha']
        commit = self.api.request('/commits/' + sha)
        signed(commit)
        data = self.api.request('/contents/' + p['state_path'] + '?ref=' + sha)
        require(data.get('type') == 'file' and data.get('encoding') == 'base64', 'state missing; owner initialization required')
        value = decode(base64.b64decode(data['content'].replace('\n', ''), validate=True))
        require(set(value) == {'version', 'revision', 'tasks'} and value['version'] == 1
                and type(value['revision']) is int and value['revision'] >= 0
                and isinstance(value['tasks'], dict) and len(value['tasks']) <= 100, 'invalid state journal')
        author = commit.get('author') or {}
        expected = p['owner'] if value['revision'] == 0 and value['tasks'] == {} else p['issuer']
        require(expected and author.get('id') == expected['id'] and author.get('login') == expected['login'],
                'state commit author is not trusted issuer')
        return value, data['sha']

    def save(self, value, blob):
        p = self.policy
        value = deepcopy(value)
        value['revision'] += 1
        # GitHub rejects a stale content SHA with 409. Never fetch/retry this write:
        # another execution may already own the delivery intent.
        response = self.api.request('/contents/' + p['state_path'], 'PUT', {
            'message': 'chore: record collaboration transition', 'branch': p['state_branch'],
            'sha': blob, 'content': base64.b64encode(json.dumps(value, sort_keys=True).encode()).decode()})
        commit = self.api.request('/commits/' + response['commit']['sha'])
        signed(commit)
        issuer = p['issuer']
        require(commit.get('author', {}).get('id') == issuer['id']
                and commit['author'].get('login') == issuer['login'], 'unexpected journal signer')
        return value, response['content']['sha']


def owner_comment(comment, policy):
    owner = policy['owner']
    user = comment.get('user') or {}
    return isinstance(user, dict) and all(user.get(k) == v for k, v in owner.items()) \
        and comment.get('author_association') == 'OWNER' \
        and comment.get('created_at') and comment.get('created_at') == comment.get('updated_at')


def new_task(api, policy, number, command, comments, now):
    require(owner_comment(command, policy), 'root event is not fresh authenticated owner command')
    match = re.fullmatch(r'/collaborate ([1-9][0-9]*)', command.get('body', ''))
    require(match, 'expected exact /collaborate ROOT_COMMENT_ID')
    root_id = int(match[1])
    roots = [c for c in comments if c['id'] == root_id and c['id'] < command['id']]
    require(len(roots) == 1 and owner_comment(roots[0], policy), 'missing/edited owner OpenCode root')
    task, _ = parse_command(roots[0].get('body'))
    require(task, 'root must preserve existing exact /oc or /opencode command')
    pr = api.request('/pulls/' + str(number))
    b = binding(pr, policy)
    require(str(number) in policy['implementers'], 'PR has no owner-enrolled implementer')
    # Explicit immutable root binding prevents an old authorization acquiring a new HEAD.
    require(roots[0]['body'].endswith('\nDCIM-Head: ' + b['head']), 'owner root lacks current HEAD')
    return dict(id=f'pr-{number}-root-{root_id}', binding=b, root=root_id,
                root_digest=digest(comment_binding(roots[0])), event=command['id'], state='ASSIGNED',
                implementer=policy['implementers'][str(number)], mode='review',
                created_at=now, updated_at=now, attempts={}, deliveries=[], audit=[])


def audit(task, state, reason, now):
    task['state'], task['updated_at'] = state, now
    previous = task['audit'][-1]['hash'] if task['audit'] else '0' * 64
    value = dict(sequence=len(task['audit']) + 1, state=state, reason=reason,
                 at=now, previous=previous, controller_run=os.environ.get('GITHUB_RUN_ID'),
                 controller_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
                 source_sha=os.environ.get('GITHUB_WORKFLOW_SHA'))
    value['hash'] = digest(value)
    task['audit'].append(value)


def command_body(task, provider, nonce, policy):
    command = policy['providers'][provider]['command']
    require(command in {'/oc', '/dcim-claude', '@codex'}, 'unsupported native command')
    require(task['mode'] == 'review', 'implementation adapter has not been proven')
    return (command + ' review the current PR independently; post findings on this same PR. '
            'Do not modify code, create PRs, merge or deploy.\n'
            + 'DCIM-Task: ' + task['id'] + '\nDCIM-Delivery: ' + nonce
            + '\nDCIM-Root: ' + str(task['root']) + '\nDCIM-Head: ' + task['binding']['head'])


def authenticated_execution(api, proof, policy, provider, now, task=None, delivery=None):
    """Only artifacts of pinned trusted-native adapters establish capability/results."""
    require(isinstance(proof, dict) and set(proof) == {'run_id', 'workflow_id', 'workflow_path', 'jobs'}, 'missing native proof')
    require(re.fullmatch(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', proof['workflow_path']), 'invalid native workflow path')
    run = api.request('/actions/runs/' + str(proof['run_id']))
    current_sha = os.environ.get('GITHUB_WORKFLOW_SHA')
    sha = run.get('head_sha')
    require(run.get('id') == proof['run_id'] and run.get('workflow_id') == proof['workflow_id']
            and run.get('path') == proof['workflow_path'] and run.get('head_sha') == sha
            and run.get('head_branch') == policy['default_branch']
            and run.get('event') == 'issue_comment' and run.get('status') == 'completed'
            and run.get('conclusion') == 'success'
            and run.get('repository', {}).get('full_name') == policy['repository']
            and run.get('head_repository', {}).get('full_name') == policy['repository']
            and 0 <= now - timestamp(run['updated_at']) <= policy['proof_max_age_seconds'], 'stale/compromised native execution')
    historical_policy = execution_policy(api, policy, sha, current_sha)
    api.trusted_file(proof['workflow_path'], current_sha)
    jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
    checks = api.pages(f"/check-suites/{run['check_suite_id']}/check-runs", 'check_runs')
    jobs_and_checks(run, jobs, checks, proof['jobs'], {'repository': policy['repository'], 'actions_app': {'id': 15368, 'slug': 'github-actions'}})
    # The immutable artifact must match the policy of its ORIGINAL execution.
    # Current revocations/authority were checked separately above.
    evidence_api = copy(api)
    evidence_api.policy = historical_policy
    value, _ = evidence_api.artifact(run, 'dcim-agent-result-attempt-' + str(run['run_attempt']))
    require(value.get('source_sha') == sha and value.get('run_id') == run['id']
            and value.get('attempt') == run['run_attempt'] and value.get('policy_digest') == digest(historical_policy),
            'result execution/policy binding mismatch')
    require(value.get('provider') == provider and value.get('mode') == 'review'
            and value.get('result') in {'SUCCESS', 'FAILED', 'BLOCKED'}, 'invalid provider result contract')
    if task:
        require(value.get('task_id') == task['id'] and value.get('delivery_id') == delivery['id'], 'result targets another delivery')
        same_binding(value.get('binding'), task['binding'])
    references = [key for key in ('provider_comment_id', 'provider_review_id') if key in value]
    require(len(references) == 1 and type(value[references[0]]) is int and value[references[0]] > 0,
            'missing/ambiguous native result reference')
    if references[0] == 'provider_review_id':
        require(provider == 'opencode', 'unsupported native PR-review adapter')
        number = value['binding']['pr']
        comment = api.request(f'/pulls/{number}/reviews/{value[references[0]]}')
        require(comment.get('pull_request_url') == api.api_root + '/pulls/' + str(number)
                and comment.get('commit_id') == value['binding']['head']
                and comment.get('state') == 'COMMENTED', 'stale/wrong native PR review')
    else:
        comment = api.request('/issues/comments/' + str(value[references[0]]))
        require(comment.get('issue_url') == api.api_root + '/issues/' + str(value['binding']['pr'])
                and comment.get('created_at') == comment.get('updated_at'), 'edited/wrong-PR provider result')
    require(provider_identity(comment, policy['providers'][provider]['identity']), 'spoofed provider result identity')
    return value


def choose(api, policy, task, now):
    for name in ('opencode', 'claude', 'codex', 'copilot'):
        info = policy['providers'][name]
        if not info['enabled'] or not info['identity'] or not info['command'] \
                or name == task['implementer'] or task['attempts'].get(name, 0) >= policy['max_attempts']:
            continue
        issuer = policy['issuer']
        implementer = policy['providers'][task['implementer']]['identity']
        if not implementer or info['identity']['id'] == implementer['id'] or info['identity']['app_id'] == implementer['app_id']:
            continue
        if info['identity']['id'] == issuer['id'] or info['identity']['app_id'] == issuer['app_id']:
            continue  # Never interpret an App's own command as cross-provider work.
        try:
            evidence = authenticated_execution(api, info['proof'], policy, name, now)
            if evidence['result'] == 'SUCCESS':
                return name
        except Rejected:
            continue
    return None


def execution_result(api, policy, task, now):
    delivery = task['deliveries'][-1] if task['deliveries'] else None
    if not delivery or delivery['status'] != 'DELIVERED':
        return None
    proof = policy['providers'].get(delivery['provider'], {}).get('proof')
    if not isinstance(proof, dict):
        return None  # Revoked/missing enrollment never authorizes a result.
    runs = api.pages('/actions/workflows/' + str(proof['workflow_id']) + '/runs?event=issue_comment', 'workflow_runs')
    for run in sorted(runs, key=lambda r: r['id'], reverse=True)[:20]:
        if run.get('status') != 'completed':
            continue
        try:
            result = authenticated_execution(api, dict(proof, run_id=run['id']), policy,
                                             delivery['provider'], now, task, delivery)
        except Rejected:
            continue
        return result, run['id']
    return None


def execution_summary(result):
    # Bound durable state size independently of provider artifact size.
    fields = ('binding', 'task_id', 'delivery_id', 'source', 'source_sha', 'run_id',
              'attempt', 'policy_digest', 'provider_comment_id', 'provider_review_id', 'native_review_id')
    return dict({k: result[k] for k in fields if k in result}, evidence_digest=digest(result))


def reconcile(api, store, policy, state, blob, task_id, now):
    state = deepcopy(state)
    task = state['tasks'][task_id]
    if task['state'] in TERMINAL:
        return state, blob
    try:
        current = binding(api.request('/pulls/' + str(task['binding']['pr'])), policy)
    except Rejected:
        audit(task, 'BLOCKED', 'PR unavailable or no longer eligible', now)
        return store.save(state, blob)
    if current != task['binding']:
        # A main advance must not erase a valid completed execution. Preserve its
        # original exact binding, but NEVER accept it for the new HEAD/base.
        observed = execution_result(api, policy, task, now)
        if observed:
            result, run_id = observed
            task['deliveries'][-1].update(status='COMPLETED', result=result['result'],
                                         run_id=run_id, execution_evidence=execution_summary(result))
        audit(task, 'BLOCKED', 'HEAD/base changed; new owner-bound task required', now)
        return store.save(state, blob)
    roots = api.pages('/issues/' + str(current['pr']) + '/comments')
    root = [c for c in roots if c['id'] == task['root']]
    if len(root) != 1 or not owner_comment(root[0], policy) or digest(comment_binding(root[0])) != task['root_digest']:
        audit(task, 'BLOCKED', 'owner root changed or disappeared', now)
        return store.save(state, blob)
    if now - task['created_at'] >= policy['timeout_seconds']:
        audit(task, 'ESCALATED', 'task deadline exhausted', now)
        return store.save(state, blob)
    if task['deliveries']:
        delivery = task['deliveries'][-1]
        if delivery['status'] == 'RESERVED':
            matches = [c for c in roots if c.get('body') == delivery['body']
                       and provider_identity(c, policy['issuer'])
                       and c.get('created_at') == c.get('updated_at')]
            if len(matches) != 1:
                audit(task, 'ESCALATED', 'missing/ambiguous authenticated delivery; no automatic repost', now)
                return store.save(state, blob)
            delivery.update(status='DELIVERED', comment_id=matches[0]['id'])
            audit(task, 'INDEPENDENT_REVIEW', 'recovered authenticated delivery', now)
            return store.save(state, blob)
        if delivery['status'] == 'DELIVERED':
            observed = execution_result(api, policy, task, now)
            if observed:
                result, run_id = observed
                delivery.update(status='COMPLETED', result=result['result'], run_id=run_id,
                                execution_evidence=execution_summary(result))
                if result['result'] == 'SUCCESS':
                    try:
                        # Live collector verifies exact current review, CI, signatures,
                        # owner attestation, Actions provenance and unresolved findings.
                        verify_check(GitHub(load_policy(), root=api.root), current['pr'])
                        audit(task, 'ACCEPTED', 'authenticated current acceptance gate', now)
                    except Rejected:
                        audit(task, 'BLOCKED', 'review completed; independent acceptance requirements unmet', now)
                else:
                    audit(task, 'ASSIGNED', 'provider failed/blocked; bounded fallback', now)
                return store.save(state, blob)
            return state, blob  # Interrupted/quota-limited providers expire at deadline.
    if len(task['deliveries']) >= policy['max_deliveries']:
        audit(task, 'ESCALATED', 'delegation budget exhausted', now)
        return store.save(state, blob)
    provider = choose(api, policy, task, now)
    if not provider:
        audit(task, 'ESCALATED', 'no independently proven eligible native provider', now)
        return store.save(state, blob)
    nonce = digest(dict(task=task['id'], index=len(task['deliveries']), head=current['head']))[:32]
    body = command_body(task, provider, nonce, policy)
    delivery = dict(id=nonce, provider=provider, body=body, status='RESERVED', created_at=now)
    task['deliveries'].append(delivery)
    task['attempts'][provider] = task['attempts'].get(provider, 0) + 1
    audit(task, 'ASSIGNED', 'reserved native delivery', now)
    state, blob = store.save(state, blob)  # CAS AND verified signature before POST.
    # A second live read closes normal queued execution races. The destination
    # must independently revalidate HEAD as OpenCode already does.
    same_binding(binding(api.request('/pulls/' + str(current['pr'])), policy), current)
    comment = api.request('/issues/' + str(current['pr']) + '/comments', 'POST', dict(body=body))
    require(provider_identity(comment, policy['issuer']) and comment.get('body') == body
            and comment.get('created_at') == comment.get('updated_at'), 'wrong delivery App or mutated command')
    task = state['tasks'][task_id]
    task['deliveries'][-1].update(status='DELIVERED', comment_id=comment['id'])
    audit(task, 'INDEPENDENT_REVIEW', 'fresh authenticated native command delivered', now)
    return store.save(state, blob)



def ingest_pending(api, store, policy, state, blob, now):
    """Discover owner events lost from GitHub's single pending concurrency slot.

    API history is authoritative. Only fresh commands on explicitly enrolled PRs
    can start tasks; rediscovery never resets an existing identity or deadline.
    Invalid/stale commands are ignored, but a failed CAS is never retried here.
    """
    enrolled = policy['implementers']
    require(isinstance(enrolled, dict) and len(enrolled) <= 50, 'too many enrolled PRs')
    state = deepcopy(state)
    for number in sorted(enrolled, key=int):
        require(re.fullmatch(r'[1-9][0-9]*', number), 'invalid enrolled PR')
        comments = api.pages('/issues/' + number + '/comments')
        for comment in sorted(comments, key=lambda item: item['id']):
            if not owner_comment(comment, policy) or not isinstance(comment.get('body'), str):
                continue
            match = re.fullmatch(r'/collaborate ([1-9][0-9]*)', comment['body'])
            if not match:
                continue
            task_id = f'pr-{number}-root-{int(match[1])}'
            if task_id in state['tasks']:
                continue
            try:
                created = timestamp(comment['created_at'])
                require(0 <= now - created < policy['timeout_seconds'], 'stale initiating command')
                task = new_task(api, policy, int(number), comment, comments, now)
            except (Rejected, ValueError):
                continue
            require(len(state['tasks']) < 100, 'journal capacity; owner archival required')
            task['created_at'] = created
            state['tasks'][task_id] = task
            state, blob = store.save(state, blob)
    return state, blob


def main():
    root = Path(__file__).resolve().parents[2]
    policy = decode((root / POLICY).read_bytes())
    require(policy.get('enabled') is True, 'collaboration controller is disabled pending independent/live proof')
    api = GitHub(policy, root=root)
    require(os.environ.get('GITHUB_REPOSITORY') == policy['repository']
            and os.environ.get('GITHUB_REF') == 'refs/heads/' + policy['default_branch']
            and os.environ.get('GITHUB_WORKFLOW_REF') == policy['repository'] + '/' + WORKFLOW + '@refs/heads/' + policy['default_branch'], 'untrusted controller runtime')
    sha = os.environ.get('GITHUB_WORKFLOW_SHA')
    require(api.request('/branches/' + policy['default_branch'])['commit']['sha'] == sha, 'stale controller revision')
    signed(api.request('/commits/' + sha))
    for path in (POLICY, WORKFLOW, SCRIPT, '.github/scripts/orchestration_provenance.py', '.github/scripts/acceptance_api.py', '.github/scripts/acceptance_core.py', '.github/scripts/acceptance_gate.py', '.github/scripts/agent_authority.py', '.github/acceptance/policy.json'):
        api.trusted_file(path, sha)
    require(policy['issuer'] and all(p['identity'] is None or p['identity']['id'] != policy['owner']['id'] for p in policy['providers'].values()), 'issuer/provider configuration incomplete')
    now = int(datetime.now(timezone.utc).timestamp())
    event = decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
    store = Store(api, policy)
    state, blob = store.read()
    if os.environ.get('GITHUB_EVENT_NAME') == 'issue_comment':
        require(event.get('action') == 'created' and event.get('issue', {}).get('pull_request')
                and event.get('sender', {}).get('id') == policy['owner']['id'], 'unauthorized initiating event')
        number = event['issue']['number']
        comment = api.request('/issues/comments/' + str(event['comment']['id']))
        require(comment.get('issue_url') == api.api_root + '/issues/' + str(number)
                and all(comment.get(k) == event['comment'].get(k) for k in ('id', 'body', 'created_at', 'updated_at')), 'event/comment mismatch')
        comments = api.pages('/issues/' + str(number) + '/comments')
        task = new_task(api, policy, number, comment, comments, now)
        if task['id'] not in state['tasks']:
            require(len(state['tasks']) < 100, 'journal capacity; owner archival required')
            state['tasks'][task['id']] = task
            state, blob = store.save(state, blob)
    else:
        require(os.environ.get('GITHUB_EVENT_NAME') in {'schedule', 'workflow_run'}, 'unsupported event')
    # Scan even after issue_comment: a newer pending callback may have displaced
    # another root event before it ever reached the journal.
    state, blob = ingest_pending(api, store, policy, state, blob, now)
    for task_id in sorted(state['tasks']):
        state, blob = reconcile(api, store, policy, state, blob, task_id, now)
    print(json.dumps(dict(revision=state['revision'], states={k: t['state'] for k, t in state['tasks'].items()})))


if __name__ == '__main__':
    try:
        main()
    except (Rejected, KeyError, ValueError, TypeError, OSError):
        raise SystemExit('Collaboration failed closed; inspect trusted run and journal. No automatic delivery retry.') from None
