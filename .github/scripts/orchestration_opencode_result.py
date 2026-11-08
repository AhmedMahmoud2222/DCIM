"""Seal native review execution, never acceptance. No model can call this adapter."""
import json
import os
from pathlib import Path
import re

from acceptance_api import GitHub
from acceptance_core import require, decode, binding, provider_identity, digest
from agent_authority import authorize, comment_binding
from orchestration_provenance import execution_policy

POLICY = '.github/agents/collaboration-policy.json'


def seal(api, policy, event, review_id, head, base, run_id, attempt, sha):
    require(policy.get('enabled') is True, 'collaboration is disabled')
    provider = policy['providers']['opencode']['identity']
    require(provider and policy['issuer'], 'unverified native identities')
    require(event.get('action') == 'created' and event.get('issue', {}).get('pull_request'), 'not a native PR command')
    number = event['issue']['number']
    pr = api.request('/pulls/' + str(number))
    b = binding(pr, policy)
    current_base = b['base']
    require(b['head'] == head and base == sha, 'stale native execution')
    b = dict(b, base=base)  # Preserve execution binding; acceptance uses live base.
    comment = api.request('/issues/comments/' + str(event['comment']['id']))
    require(comment_binding(comment) == comment_binding(event['comment'])
            and comment.get('issue_url') == api.api_root + '/issues/' + str(number), 'command event changed')
    comments = api.pages('/issues/' + str(number) + '/comments')
    allowed, _ = authorize(comment, [policy['issuer']], comments, {policy['owner']['id']}, head)
    require(allowed, 'unauthenticated native delegation')
    match = re.search(r'\nDCIM-Task: (pr-[1-9][0-9]*-root-[1-9][0-9]*)\nDCIM-Delivery: ([0-9a-f]{32})\nDCIM-Root: ([1-9][0-9]*)\nDCIM-Head: ([0-9a-f]{40})\Z', comment['body'])
    require(match and match[1] == f'pr-{number}-root-{match[3]}'
            and re.match(r'\A/oc review(?:\s|\Z)', comment['body']), 'invalid delivery envelope')
    require(type(review_id) is int and review_id > 0, 'missing native review ID')
    review = api.request(f'/pulls/{number}/reviews/{review_id}')
    require(review.get('id') == review_id and review.get('commit_id') == head
            and review.get('state') == 'COMMENTED'
            and all(review.get('user', {}).get(k) == provider[k] for k in ('id', 'login'))
            and review.get('user', {}).get('type') == 'Bot', 'wrong native review identity or commit')
    marker = re.match(r'\A<!-- opencode-review:(\{.*?\}) -->', review.get('body', ''))
    require(marker, 'missing native publisher record')
    record = decode(marker[1])
    require(record.get('reviewed_commit') == head and record.get('base_commit') == base
            and (record.get('stale_on_arrival') is False
                 or record.get('stale_on_arrival') is True and current_base != base)
            and record.get('run_url') == f"https://github.com/{policy['repository']}/actions/runs/{run_id}", 'native review provenance mismatch')
    # The immutable artifact authenticates the producer. A fixed fresh witness
    # proves its actual App token, including when PR review REST omits App fields.
    body = 'DCIM native execution witness; NOT acceptance.\n' + json.dumps(dict(
        task_id=match[1], delivery_id=match[2], binding=b,
        review_id=review_id, run_id=run_id, attempt=attempt), sort_keys=True)
    prior = [c for c in comments if c.get('body') == body]
    require(len(prior) <= 1, 'duplicate native witness')
    witness = prior[0] if prior else api.request(f'/issues/{number}/comments', 'POST', {'body': body})
    require(provider_identity(witness, provider) and witness.get('body') == body
            and witness.get('created_at') == witness.get('updated_at'), 'spoofed/edited native App witness')
    return dict(provider='opencode', mode='review', result='SUCCESS', task_id=match[1],
                delivery_id=match[2], binding=b, provider_comment_id=witness['id'],
                native_review_id=review_id, source_sha=sha, run_id=run_id, attempt=attempt,
                source=dict(run_id=run_id, attempt=attempt, trust_sha=sha),
                policy_digest=digest(policy))


def main():
    root = Path(__file__).resolve().parents[2]
    policy = decode((root / POLICY).read_bytes())
    repo = policy['repository']
    require(os.environ.get('GITHUB_REPOSITORY') == repo
            and os.environ.get('GITHUB_REF') == 'refs/heads/main'
            and os.environ.get('GITHUB_WORKFLOW_REF') == repo + '/.github/workflows/opencode.yml@refs/heads/main', 'untrusted native runtime')
    api = GitHub(policy, root=root)
    sha = os.environ['GITHUB_WORKFLOW_SHA']
    current_sha = api.request('/branches/main')['commit']['sha']
    current_policy = decode(api.file(POLICY, current_sha))
    require(execution_policy(api, current_policy, sha, current_sha) == policy,
            'native source or execution authority revoked')
    for path in (POLICY, '.github/workflows/opencode.yml', '.github/scripts/orchestration_opencode_result.py',
                 '.github/scripts/opencode_review.py', '.github/scripts/agent_authority.py',
                 '.github/scripts/acceptance_api.py', '.github/scripts/acceptance_core.py',
                 '.github/scripts/orchestration_provenance.py'):
        api.trusted_file(path, sha)
    value = seal(api, policy, decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()),
                 int(os.environ['NATIVE_REVIEW_ID']), os.environ['AUTHORIZED_SHA'], os.environ['BASE_SHA'],
                 int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT']), sha)
    output = root / 'sealed-collaboration'
    output.mkdir(exist_ok=True)
    (output / 'evidence.json').write_text(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
