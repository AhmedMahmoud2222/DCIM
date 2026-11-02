"""Inactive native Claude review adapter. Execution evidence never grants approval.

Only trusted main code runs. A deterministic preflight authenticates the actual
App command, independently claims its owner-root budget and prepares inert patches.
The model can read only that context; a separate runner seals structured results.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import sys

from acceptance_api import GitHub
from acceptance_core import Rejected, require, decode, digest, binding, same_binding, provider_identity, structured_review, jobs_and_checks
from acceptance_gate import prepare_context, write_record, finding_comments
from acceptance_provider_witness import clear_git_credentials
import agent_authority
from orchestration_provenance import verified_commit, execution_policy

POLICY = '.github/agents/collaboration-policy.json'
WORKFLOW = '.github/workflows/agent-claude.yml'
SCRIPT = '.github/scripts/orchestration_claude_adapter.py'
ACTION = 'anthropics/claude-code-action@d70510b174a1244958df0b03166a54912811f3e6'
CLAUDE = dict(id=209825114, login='claude[bot]', app_id=1236702, app_slug='claude')


def load_policy():
    return decode((Path(__file__).resolve().parents[2] / POLICY).read_bytes())


def runtime(api):
    p = api.policy
    require(p.get('enabled') is True and p['providers']['claude'].get('enabled') is True,
            'Claude collaboration adapter is not activated')
    require(p['providers']['claude']['identity'] == CLAUDE, 'Claude App identity differs from pinned native provider')
    require(os.environ.get('GITHUB_REPOSITORY') == p['repository']
            and os.environ.get('GITHUB_EVENT_NAME') == 'issue_comment'
            and os.environ.get('GITHUB_REF') == 'refs/heads/' + p['default_branch']
            and os.environ.get('GITHUB_WORKFLOW_REF') == p['repository'] + '/' + WORKFLOW + '@refs/heads/' + p['default_branch'],
            'untrusted Claude adapter execution')
    sha = os.environ.get('GITHUB_WORKFLOW_SHA')
    verified_commit(api, sha)
    current_sha = api.request('/branches/' + p['default_branch'])['commit']['sha']
    current_policy = decode(api.file(POLICY, current_sha))
    require(execution_policy(api, current_policy, sha, current_sha) == p,
            'historical adapter policy does not match authenticated execution policy')
    for path in (POLICY, WORKFLOW, SCRIPT, '.github/scripts/acceptance_api.py', '.github/scripts/acceptance_core.py',
                 '.github/scripts/acceptance_gate.py', '.github/scripts/acceptance_provider_witness.py',
                 '.github/scripts/acceptance_read_guard.py', '.github/scripts/agent_authority.py',
                 '.github/scripts/orchestration_provenance.py'):
        api.trusted_file(path, sha)
    run = api.request('/actions/runs/' + os.environ['GITHUB_RUN_ID'])
    require(run.get('id') == int(os.environ['GITHUB_RUN_ID']) and run.get('run_attempt') == int(os.environ['GITHUB_RUN_ATTEMPT'])
            and run.get('workflow_id') == api.workflow(WORKFLOW) and run.get('path') == WORKFLOW
            and run.get('head_sha') == sha and run.get('head_branch') == p['default_branch']
            and run.get('event') == 'issue_comment'
            and run.get('repository', {}).get('full_name') == p['repository']
            and run.get('head_repository', {}).get('full_name') == p['repository'], 'wrong native adapter run')
    issuer = p['issuer']
    require(issuer and issuer['id'] != CLAUDE['id'] and issuer['app_id'] != CLAUDE['app_id'], 'self-trigger or missing issuer')
    for key in ('actor', 'triggering_actor'):
        require(all(run.get(key, {}).get(k) == value for k, value in dict(id=issuer['id'],login=issuer['login'],type='Bot').items()),
                'unauthenticated source workflow actor')
    return run, sha


def native_to_oc(comment):
    value = deepcopy(comment)
    body = value.get('body', '')
    if isinstance(body, str) and body.startswith('/dcim-claude '):
        value['body'] = '/oc ' + body[len('/dcim-claude '):]
    return value


def authenticate(api, event, claim=False, captured=None):
    p = api.policy
    require(event.get('action') == 'created' and 'pull_request' in event.get('issue', {}), 'not a fresh PR command')
    number = event['issue']['number']
    supplied = event['comment']
    issuer = p['issuer']
    require(issuer and provider_identity(supplied, issuer), 'source App/actor is not explicitly trusted')
    require(all(event.get('sender', {}).get(k) == v for k, v in dict(id=issuer['id'],login=issuer['login'],type='Bot').items()),
            'spoofed command sender')
    current = api.request('/issues/comments/' + str(supplied['id']))
    require(agent_authority.comment_binding(current) == agent_authority.comment_binding(supplied)
            and provider_identity(current, issuer)
            and current.get('issue_url') == api.api_root + '/issues/' + str(number), 'edited/replaced/cross-PR command')
    body = current.get('body', '')
    require(isinstance(body, str) and body.startswith('/dcim-claude review '), 'only exact native review commands supported')
    match = re.search(r'\nDCIM-Task: (pr-([1-9][0-9]*)-root-([1-9][0-9]*))\nDCIM-Delivery: ([0-9a-f]{32})\nDCIM-Root: ([1-9][0-9]*)\nDCIM-Head: ([0-9a-f]{40})\Z', body)
    require(match and int(match[2]) == number and match[3] == match[5], 'invalid task/delivery/root metadata')
    pr = api.request('/pulls/' + str(number))
    current_binding = binding(pr, p)
    b = current_binding if captured is None else captured['binding']
    require(b.get('repository') == current_binding['repository'] and b.get('pr') == number
            and b.get('head') == current_binding['head'] == match[6]
            and b.get('base') == os.environ.get('GITHUB_WORKFLOW_SHA'), 'stale command HEAD/base')
    implementer = p['implementers'].get(str(number))
    info = p['providers'].get(implementer, {}).get('identity')
    require(implementer != 'claude' and info and info['id'] != CLAUDE['id'] and info['app_id'] != CLAUDE['app_id']
            and pr.get('user', {}).get('id') != CLAUDE['id'], 'reviewer is implementation author')
    comments = api.pages('/issues/' + str(number) + '/comments')
    # Conversion is syntax adaptation AFTER original GitHub identity/body checks.
    # Both the event and complete history are transformed, so shared budget and
    # duplicate-ID validation cannot be bypassed by mixing native command syntax.
    translated = [native_to_oc(c) for c in comments]
    valid, reason = agent_authority.authorize(native_to_oc(current), [issuer], translated, [p['owner']['id']], b['head'])
    require(valid, reason)
    if claim:
        require(agent_authority.claim(p['repository'], current['id'], int(match[5]), os.environ.get('GITHUB_TOKEN')),
                'command/root budget already claimed or claim ambiguous')
    return dict(binding=b, task_id=match[1], delivery_id=match[4], root=int(match[5]), command_id=current['id'])


def prepare(api):
    runtime(api)
    event = decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
    request = authenticate(api, event, claim=True)
    b = request['binding']
    pr = api.request('/pulls/' + str(b['pr']))
    files = api.pages('/pulls/' + str(b['pr']) + '/files')
    require(files and len(files) == pr.get('changed_files'), 'incomplete native review diff')
    for file in files:
        patch = file.get('patch')
        require(isinstance(patch, str) and patch
                and sum(line.startswith('+') for line in patch.splitlines()) == file.get('additions')
                and sum(line.startswith('-') for line in patch.splitlines()) == file.get('deletions'), 'missing/truncated patch')
    prepare_context(b, [], files)
    same_binding(binding(api.request('/pulls/' + str(b['pr'])), api.policy), b)
    schema = dict(type='object', additionalProperties=False,
        required=['binding','reviewer','verdict','findings','resolved_finding_ids'], properties={
            'binding':dict(type='object',const=b), 'reviewer':dict(type='string',const='claude'),
            'verdict':dict(type='string',enum=['ACCEPT','BLOCK']),
            'findings':dict(type='array',maxItems=100,items=dict(type='object',additionalProperties=False,
                required=['id','blocking','message'],properties={'id':dict(type='string'),'blocking':dict(type='boolean'),'message':dict(type='string')})),
            'resolved_finding_ids':dict(type='array',maxItems=1000,items=dict(type='string'))})
    args = '--tools Read --allowedTools ' + shlex.quote('Read(./review-input/**)')
    args += ' --permission-mode dontAsk --max-turns 20 --json-schema ' + shlex.quote(json.dumps(schema))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('request=' + json.dumps(request) + '\nclaude_args=' + args
                     + '\nallowed_bot=' + api.policy['issuer']['login'] + '\n')


def witness_body(request, run):
    return 'DCIM native Claude collaboration witness; NOT acceptance.\n' + json.dumps(
        dict(request=request,run_id=run['id'],attempt=run['run_attempt']),sort_keys=True)


def witness_path():
    return Path(os.environ['RUNNER_TEMP']) / ('collaboration-claude-witness-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'] + '.json')


def witness(api):
    # The official action persists its native App token in git origin. Remove it
    # before ANY network operation or model tool can run, even if this hook fails.
    clear_git_credentials(api.root, api.policy['repository'])
    run, _ = runtime(api)
    request = decode(os.environ['REVIEW_REQUEST'])
    require(request == authenticate(api, decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()), captured=request), 'witness request changed')
    path = witness_path()
    expected = witness_body(request, run)
    if path.exists():
        saved = decode(path.read_bytes())
        comment = api.request('/issues/comments/' + str(saved['id']))
        require(provider_identity(comment, CLAUDE) and comment.get('body') == expected, 'invalid previous native witness')
        return
    with path.open('x') as out:
        os.chmod(path,0o600)
        out.write('{}')
    comment = api.request('/issues/' + str(request['binding']['pr']) + '/comments','POST',dict(body=expected))
    require(provider_identity(comment, CLAUDE) and comment.get('body') == expected
            and comment.get('created_at') == comment.get('updated_at')
            and type(comment.get('id')) is int and comment['id'] > 0, 'native token is not pinned Claude App')
    path.write_text(json.dumps(dict(id=comment['id'])))


def seal(api):
    run, sha = runtime(api)
    request = decode(os.environ['REVIEW_REQUEST'])
    require(request == authenticate(api, decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()), captured=request), 'sealed request changed')
    require(os.environ.get('NATIVE_CONCLUSION') == 'success', 'native action failed')
    review = structured_review(decode(os.environ.get('REVIEW_JSON','')),request['binding'],'claude')
    session = os.environ.get('NATIVE_SESSION','')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session), 'missing native review session')
    jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",'jobs')
    checks = api.pages(f"/check-suites/{run['check_suite_id']}/check-runs",'check_runs')
    jobs_and_checks(run,jobs,checks,['prepare','review'],dict(api.policy,actions_app={'id':15368,'slug':'github-actions'}))
    native = next(j for j in jobs if j['name'] == 'review')
    witness_id = int(os.environ.get('PROVIDER_COMMENT_ID','0'))
    require(witness_id > 0,'native Claude witness is absent')
    comment = api.request('/issues/comments/' + str(witness_id))
    require(provider_identity(comment,CLAUDE) and comment.get('issue_url') == api.api_root + '/issues/' + str(request['binding']['pr'])
            and comment.get('created_at') == comment.get('updated_at')
            and comment.get('body') == witness_body(request,run)
            and native['started_at'] <= comment.get('created_at','') <= native['completed_at'], 'invalid native witness provenance')
    value = dict(request,provider='claude',mode='review',result='SUCCESS',source_sha=sha,run_id=run['id'],attempt=run['run_attempt'],
                 policy_digest=digest(api.policy),source=dict(run_id=run['id'],attempt=run['run_attempt'],trust_sha=sha),
                 provider_comment_id=witness_id,action=ACTION,session_id=session,
                 review=review,verdict=review['verdict'],findings=review['findings'])
    # A BLOCK is still a completed native execution. It is NOT acceptance. Only
    # the independent acceptance collector can issue acceptance authority.
    display = dict(binding=request['binding'],source=dict(run_id=run['id'],attempt=run['run_attempt'],trust_sha=sha),review=review,provider=CLAUDE)
    for body in finding_comments(display):
        api.request('/issues/' + str(request['binding']['pr']) + '/comments','POST',dict(body=body))
    write_record('sealed/evidence.json',value)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['prepare','witness','seal'])
    args=parser.parse_args()
    api=GitHub(load_policy())
    {'prepare':prepare,'witness':witness,'seal':seal}[args.mode](api)


if __name__ == '__main__':
    try:
        main()
    except (Rejected,KeyError,ValueError,TypeError,OSError):
        raise SystemExit('Claude collaboration adapter failed closed; no acceptance or publication authorized.') from None
