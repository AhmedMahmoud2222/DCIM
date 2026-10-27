#!/usr/bin/env python3
"""Trusted native-review adapter, owner attestation and acceptance gate.

No command in this program merges, deploys or executes candidate code. Workflows
must be installed on main before runtime() permits their operation. Comment text
is never read as a verdict; only the pinned native action's structured output is
sealed, and subsequently re-authenticated through run/artifact provenance.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import shlex
import sys

from acceptance_api import GitHub
from acceptance_core import (Rejected, CHECK, require, decode, digest, binding, same_binding,
    independent, structured_review, source_run, jobs_and_checks, signed_range, owner_approval,
    provider_identity, decision)

ROOT = Path(__file__).resolve().parents[2]


def load_policy():
    return decode((ROOT / '.github/acceptance/policy.json').read_bytes())


def source(run):
    return dict(run_id=run['id'], attempt=run['run_attempt'], trust_sha=run['head_sha'])


def record_name(kind, number, attempt):
    return f'dcim-{kind}-pr-{number}-attempt-{attempt}'


def context(api, number):
    require(type(number) is int and number > 0, 'invalid PR number')
    pr = api.request(f'/pulls/{number}')
    b = binding(pr, api.policy)
    require(api.request('/branches/' + api.policy['default_branch'])['commit']['sha'] == b['base'], 'base is not current main')
    return pr, b


def self_run(api, kind):
    trust_sha = api.runtime(api.policy['workflows'][kind]['path'])
    run = api.request('/actions/runs/' + os.environ['GITHUB_RUN_ID'])
    if kind in {'review', 'owner'}:
        api.validate_run(run, kind, trust_sha, completed=False)
    else:
        require(run.get('path') == api.policy['workflows']['gate']['path']
                and run.get('workflow_id') == api.workflow(run['path'])
                and run.get('head_sha') == trust_sha and run.get('head_branch') == api.policy['default_branch']
                and run.get('event') in {'workflow_dispatch', 'workflow_run', 'schedule'}
                and run.get('repository', {}).get('full_name') == api.policy['repository']
                and run.get('head_repository', {}).get('full_name') == api.policy['repository'], 'untrusted gate execution')
    require(str(run.get('run_attempt')) == os.environ.get('GITHUB_RUN_ATTEMPT'), 'runtime attempt mismatch')
    return run, trust_sha


def write_record(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, allow_nan=False)
    require(len(raw.encode()) <= 524288, 'record too large')
    path.write_text(raw + '\n')


def prepare_context(b, history, files):
    # Claude Read truncates long lines. GitHub patches are strings with escaped
    # newlines in JSON; store explicit line arrays and bounded prior messages.
    prepared = []
    for file in files:
        value = dict(file)
        value['patch_lines'] = value.pop('patch').splitlines()
        prepared.append(value)
    prior = json.loads(json.dumps(history))
    for review in prior:
        for finding in review['findings']:
            message = finding.pop('message')
            finding['message_parts'] = [message[i:i+200] for i in range(0, len(message), 200)]
    raw = json.dumps(dict(binding=b, prior_reviews=prior, files=prepared), indent=2, sort_keys=True)
    require(len(raw.encode()) <= 524288 and all(len(line) <= 1800 for line in raw.splitlines()),
            'review context exceeds safe Read limits; manual review required')
    path = Path('review-input/context.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw + '\n')


def finding_comments(value):
    """Bounded complete same-PR records, below GitHub's comment size limit."""
    review = value['review']
    header = dict(binding=value['binding'], source=value['source'], verdict=review['verdict'],
                  evidence_digest=digest(value), provider=value['provider'])
    items = [dict(finding=f) for f in review['findings']]
    items += [dict(resolved_finding_id=i) for i in review['resolved_finding_ids']]
    groups, current = [], []
    for item in items:
        if current and len(json.dumps(current + [item]).encode()) > 24000:
            groups.append(current); current = []
        current.append(item)
    groups.append(current)
    return ['Trusted review adapter record (not merge authorization).\n\n```json\n'
            + json.dumps(dict(header, part=i+1, parts=len(groups), items=group), sort_keys=True) + '\n```'
            for i, group in enumerate(groups)]


def records(api, kind, number, exclude_run=None):
    spec = api.policy['workflows'][kind]
    wid = api.workflow(spec['path'])
    runs = api.pages(f"/actions/workflows/{wid}/runs?event={spec['event']}&branch={api.policy['default_branch']}", 'workflow_runs')
    title = ('DCIM review PR ' if kind == 'review' else 'DCIM owner PR ') + str(number)
    # Unrelated comments still instantiate this workflow with all jobs skipped.
    # They are not review attempts; failed/cancelled/in-progress attempts remain
    # authoritative and must never fall back to an older acceptance.
    selected = [r for r in runs if r.get('display_title') == title and r.get('id') != exclude_run
                and not (kind == 'review' and r.get('conclusion') == 'skipped')]
    require(len({r['id'] for r in selected}) == len(selected), 'duplicate source runs')
    selected.sort(key=lambda r: (r['run_number'], r['id']), reverse=True)
    return selected


def review_record(api, run, expected_pr):
    api.validate_run(run, 'review', run['head_sha'])
    value, artifact_id = api.artifact(run, record_name('review', expected_pr, run['run_attempt']))
    require(value.get('kind') == 'review' and value.get('binding', {}).get('pr') == expected_pr, 'wrong review artifact')
    require(value['binding']['repository'] == api.policy['repository'] and value['binding']['base'] == run['head_sha'],
            'review did not execute from reviewed base')
    reviewer = api.policy['reviewer']
    structured_review(value.get('review'), value['binding'], reviewer)
    require(value.get('provider') == api.policy['providers'][reviewer]
            and value.get('action') == api.policy['review_action'], 'wrong provider/action')
    require(isinstance(value.get('session_id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['session_id']), 'missing native session')
    witness = api.request(f"/issues/comments/{value.get('provider_comment_id')}")
    require(provider_identity(witness, api.policy['providers'][reviewer]), 'provider App/actor identity mismatch')
    require(witness.get('issue_url') == api.api_root + '/issues/' + str(expected_pr), 'provider witness on another PR')
    from acceptance_provider_witness import witness_body
    require(witness.get('body') == witness_body(value['binding'], run['id'], run['run_attempt']),
            'provider witness belongs to another run')
    return value, artifact_id


def previous_findings(api, number, exclude_run=None):
    runs = records(api, 'review', number, exclude_run)
    outstanding, history, cutoff = checkpoint(api, number)
    for run in reversed(runs):
        if run['run_number'] <= cutoff:
            continue
        if run.get('status') != 'completed' or run.get('conclusion') != 'success':
            continue  # An incomplete run is never positive evidence; newest is checked separately.
        value, _ = review_record(api, run, number)
        review = value['review']
        outstanding.difference_update(review['resolved_finding_ids'])
        outstanding.update(f['id'] for f in review['findings'] if f['blocking'])
        history.append(review)
    return outstanding, history


def checkpoint(api, number):
    """Explicit signed owner configuration permits artifact expiry/policy migration.

    Never silently forget findings when GitHub expires a run/artifact. The owner
    must preserve open findings and an audit digest in a reviewed main commit.
    Agent publication is forbidden for the checkpoint directory.
    """
    rel = f'.github/acceptance/checkpoints/{number}.json'
    path = api.root / rel
    if not path.exists():
        return set(), [], 0
    main_sha = api.request('/branches/' + api.policy['default_branch'])['commit']['sha']
    api.trusted_file(rel, main_sha)
    value = decode(path.read_bytes())
    require(set(value) == {'version', 'pr', 'workflow_id', 'through_run_number', 'through_run_id',
                           'history_digest', 'outstanding_findings'}, 'invalid owner checkpoint')
    require(value['version'] == 1 and value['pr'] == number
            and value['workflow_id'] == api.workflow(api.policy['workflows']['review']['path'])
            and type(value['through_run_number']) is int and value['through_run_number'] > 0
            and type(value['through_run_id']) is int and value['through_run_id'] > 0
            and isinstance(value['history_digest'], str) and re.fullmatch(r'[0-9a-f]{64}', value['history_digest']),
            'invalid checkpoint execution boundary')
    boundary = api.request('/actions/runs/' + str(value['through_run_id']))
    spec = api.policy['workflows']['review']
    # The signed checkpoint explicitly migrates old source/policy. Authenticate
    # the historical boundary metadata and ancestry, not byte equality to today's
    # adapter. It supplies a cutoff, never an ACCEPT verdict.
    source_run(boundary, spec, api.workflow(spec['path']), api.policy, boundary['head_sha'], completed=False)
    ancestry = api.request('/compare/' + boundary['head_sha'] + '...' + main_sha)
    require(ancestry.get('status') in {'ahead', 'identical'}
            and ancestry.get('merge_base_commit', {}).get('sha') == boundary['head_sha'], 'checkpoint boundary is outside trusted main history')
    require(boundary.get('run_number') == value['through_run_number']
            and boundary.get('display_title') == 'DCIM review PR ' + str(number)
            and boundary.get('status') == 'completed', 'checkpoint does not match its authenticated run boundary')
    commits = api.request('/commits?path=' + rel + '&sha=' + main_sha + '&per_page=1')
    require(isinstance(commits, list) and len(commits) == 1, 'checkpoint commit unavailable')
    commit = commits[0]
    require(commit.get('author', {}).get('id') == api.policy['owner']['id']
            and commit.get('committer', {}).get('id') in {api.policy['owner']['id'], 19864447}
            and commit.get('commit', {}).get('verification', {}).get('verified') is True
            and commit['commit']['verification'].get('reason') == 'valid', 'checkpoint is not owner-signed configuration')
    # Reuse strict finding validation without granting an ACCEPT verdict.
    b = dict(repository=api.policy['repository'], pr=number, head=main_sha, base=main_sha)
    review = dict(binding=b, reviewer=api.policy['reviewer'], verdict='BLOCK',
                  findings=value['outstanding_findings'], resolved_finding_ids=[])
    structured_review(review, b, api.policy['reviewer'])
    require(all(f['blocking'] for f in review['findings']), 'checkpoint must enumerate open blocking findings')
    return {f['id'] for f in review['findings']}, [review], value['through_run_number']


def prepare_review(api):
    run, trust_sha = self_run(api, 'review')
    event = decode(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
    owner = api.policy['owner']
    require(event.get('action') == 'created', 'not a fresh authenticated command')
    user = event['comment']['user']
    require(all(user.get(k) == owner[k] for k in ('id', 'login', 'type'))
            and all(event.get('sender', {}).get(k) == owner[k] for k in ('id', 'login', 'type'))
            and event['comment'].get('author_association') == 'OWNER', 'only owner may request native acceptance review')
    require(event['comment'].get('body', '').strip() == '/acceptance-review'
            and event['comment'].get('created_at') == event['comment'].get('updated_at'), 'invalid review command')
    require('pull_request' in event.get('issue', {}), 'review requires existing PR')
    pr, b = context(api, event['issue']['number'])
    require(b['base'] == trust_sha, 'base changed before native review')
    _, reviewer = independent(dict(api.policy, _binding=b), pr['user']['id'])
    _, history = previous_findings(api, b['pr'], run['id'])
    files = api.pages(f"/pulls/{b['pr']}/files")
    require(len(files) == pr.get('changed_files'), 'incomplete PR diff')
    for file in files:
        patch = file.get('patch')
        require(isinstance(patch, str) and bool(patch), 'missing/binary/unsupported patch; manual review required')
        require(sum(line.startswith('+') for line in patch.splitlines()) == file.get('additions')
                and sum(line.startswith('-') for line in patch.splitlines()) == file.get('deletions'),
                'truncated patch; manual review required')
    # JSON preserves filenames/patch boundaries; data is read by the model, never executed.
    prepare_context(b, history, files)
    require(binding(api.request(f"/pulls/{b['pr']}"), api.policy) == b, 'PR changed while preparing review')
    schema = dict(type='object', additionalProperties=False,
        required=['binding', 'reviewer', 'verdict', 'findings', 'resolved_finding_ids'], properties={
            'binding': dict(type='object', const=b), 'reviewer': dict(type='string', const=reviewer),
            'verdict': dict(type='string', enum=['ACCEPT', 'BLOCK']),
            'findings': dict(type='array', maxItems=100, items=dict(type='object', additionalProperties=False,
                required=['id', 'blocking', 'message'], properties={'id':dict(type='string'), 'blocking':dict(type='boolean'), 'message':dict(type='string')})),
            'resolved_finding_ids': dict(type='array', items=dict(type='string'))})
    args = '--tools Read --allowedTools ' + shlex.quote('Read(./review-input/**)')
    args += ' --permission-mode dontAsk --max-turns 20 --json-schema ' + shlex.quote(json.dumps(schema))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write('binding=' + json.dumps(b) + '\nclaude_args=' + args + '\n')


def seal_review(api):
    run, trust_sha = self_run(api, 'review')
    b = decode(os.environ['REVIEW_BINDING'])
    pr, current = context(api, b['pr'])
    same_binding(b, current)
    require(b['base'] == trust_sha, 'stale reviewer base')
    _, reviewer = independent(dict(api.policy, _binding=b), pr['user']['id'])
    require(os.environ.get('NATIVE_CONCLUSION') == 'success', 'native action did not succeed')
    result = structured_review(decode(os.environ.get('REVIEW_JSON', '')), b, reviewer)
    session = os.environ.get('NATIVE_SESSION', '')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session) is not None, 'missing native session')
    jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
    checks = api.pages(f"/check-suites/{run['check_suite_id']}/check-runs", 'check_runs')
    jobs_and_checks(run, jobs, checks, ['prepare', 'review'], api.policy)
    native_job = next(j for j in jobs if j['name'] == 'review')
    from acceptance_provider_witness import witness_body
    witness_id = int(os.environ.get('PROVIDER_COMMENT_ID', '0'))
    require(witness_id > 0, 'native provider hook did not produce a witness')
    witness = api.request('/issues/comments/' + str(witness_id))
    require(provider_identity(witness, api.policy['providers'][reviewer])
            and witness.get('issue_url') == api.api_root + '/issues/' + str(b['pr'])
            and witness.get('body') == witness_body(b, run['id'], run['run_attempt'])
            and native_job['started_at'] <= witness.get('created_at', '') <= native_job['completed_at'],
            'missing/invalid native provider App witness')
    value = dict(kind='review', binding=b, source=source(run), policy_digest=digest(api.policy),
                 provider=api.policy['providers'][reviewer], action=api.policy['review_action'],
                 session_id=session, provider_comment_id=witness_id, review=result)
    write_record('sealed/evidence.json', value)
    # Fixed trusted wrapper plus schema-validated JSON, no raw model Markdown authority.
    for body in finding_comments(value):
        api.request(f"/issues/{b['pr']}/comments", 'POST', dict(body=body))


def attest_owner(api):
    run, trust_sha = self_run(api, 'owner')
    number = int(os.environ['ATTEST_PR'])
    _, b = context(api, number)
    require(b['head'] == os.environ['ATTEST_HEAD'] and b['base'] == os.environ['ATTEST_BASE'] == trust_sha, 'stale owner attestation')
    owner_approval(api.request(f"/actions/runs/{run['id']}/approvals"), api.policy['owner_environment'], api.policy)
    write_record('sealed/evidence.json', dict(kind='owner', binding=b, source=source(run),
        policy_digest=digest(api.policy), actor=api.policy['owner'], verdict='AUTHORIZE_INITIAL_SECURITY_REVIEW'))


def ci_evidence(api, b):
    runs = api.pages('/actions/runs?head_sha=' + b['head'], 'workflow_runs')
    checks = api.pages('/commits/' + b['head'] + '/check-runs?filter=all', 'check_runs')
    evidence = []
    for spec in api.policy['ci']:
        candidates = [r for r in runs if r.get('workflow_id') == spec['id']]
        require(bool(candidates), 'required workflow absent: ' + spec['path'])
        run = max(candidates, key=lambda r: (r['run_number'], r['id']))
        require(run.get('path') == spec['path'] and run.get('event') == 'pull_request'
                and run.get('head_sha') == b['head'] and run.get('status') == 'completed'
                and run.get('conclusion') == 'success'
                and run.get('repository', {}).get('full_name') == api.policy['repository']
                and run.get('head_repository', {}).get('full_name') == api.policy['repository'], 'untrusted/failed CI workflow')
        prs = [p for p in run.get('pull_requests', []) if p.get('number') == b['pr']]
        require(len(prs) == 1 and prs[0].get('head', {}).get('sha') == b['head']
                and prs[0].get('base', {}).get('sha') == b['base'], 'CI executed against another PR/base')
        require(api.file(spec['path'], b['head']) == api.file(spec['path'], b['base']), 'candidate changed its own CI workflow')
        jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
        required = list(spec['jobs'])
        if spec['path'] == '.github/workflows/opencode-security-validation.yml':
            rel = 'tests/deployment/opencode_validation/mutants.py'
            api.trusted_file(rel, b['base'])
            tree = ast.parse((api.root / rel).read_text())
            inventories = [node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == 'MUTATIONS' for t in node.targets)]
            require(len(inventories) == 1 and isinstance(inventories[0], ast.List), 'trusted mutation inventory unavailable')
            try:
                ids = [node.elts[0].value for node in inventories[0].elts]
            except (AttributeError, IndexError) as exc:
                raise Rejected('invalid trusted mutation inventory') from exc
            require(ids and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids), 'ambiguous mutation inventory')
            required += ['Mutant ' + ident for ident in ids]
        jobs_and_checks(run, jobs, checks, required, api.policy)
        evidence.append(source(run))
    return evidence


def evaluate(api, number):
    pr, b = context(api, number)
    anchor = api.request('/commits/' + b['base'])
    v = anchor.get('commit', {}).get('verification', {})
    require(anchor.get('sha') == b['base'] and v.get('verified') is True and v.get('reason') == 'valid', 'unsigned trusted base')
    independent(dict(api.policy, _binding=b), pr['user']['id'])
    compare_path = f"/compare/{b['base']}...{b['head']}"
    compare = api.request(compare_path + '?per_page=100&page=1')
    commits = api.pages(compare_path, 'commits', 'total_commits', limit=1000)
    signed_range(compare, commits, b)
    # Re-fetch signatures individually, not caller-supplied artifact claims.
    verified = [api.request('/commits/' + c['sha']) for c in commits]
    signed_range(compare, verified, b)
    ci = ci_evidence(api, b)
    require(all(t['isResolved'] for t in api.threads(number)), 'unresolved GitHub review threads')
    reviews = api.pages(f'/pulls/{number}/reviews')
    require(len({r['id'] for r in reviews}) == len(reviews), 'duplicate formal reviews')
    states = {}
    for r in sorted(reviews, key=lambda r: r['id']):
        if r.get('state') in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'PENDING'}:
            states[r['user']['id']] = r['state']
    require(not any(s in {'CHANGES_REQUESTED', 'PENDING'} for s in states.values()), 'unresolved formal review')
    runs = records(api, 'review', number)
    require(bool(runs), 'native structured review absent')
    newest = runs[0]  # Never fall back when a newer request is failed, pending or BLOCK.
    review, review_artifact = review_record(api, newest, number)
    same_binding(review['binding'], b)
    outstanding, _ = previous_findings(api, number)
    require(review['review']['verdict'] == 'ACCEPT' and not outstanding, 'independent review BLOCK/unresolved findings')
    owners = records(api, 'owner', number)
    require(bool(owners), 'explicit owner attestation absent')
    owner_run = owners[0]
    api.validate_run(owner_run, 'owner', b['base'])
    owner, owner_artifact = api.artifact(owner_run, record_name('owner', number, owner_run['run_attempt']))
    same_binding(owner.get('binding'), b)
    require(owner.get('kind') == 'owner' and owner.get('actor') == api.policy['owner']
            and owner.get('verdict') == 'AUTHORIZE_INITIAL_SECURITY_REVIEW', 'invalid owner attestation')
    owner_approval(api.request(f"/actions/runs/{owner_run['id']}/approvals"), api.policy['owner_environment'], api.policy)
    same_binding(binding(api.request(f'/pulls/{number}'), api.policy), b)
    return decision(b, 'ACCEPT', [], dict(review_run=source(newest), review_artifact=review_artifact,
        owner_run=source(owner_run), owner_artifact=owner_artifact, ci=ci,
        commits=[c['sha'] for c in verified], review_digest=digest(review)))


def publish(api, number):
    run, _ = self_run(api, 'gate')
    _, b = context(api, number)
    require(b['base'] == run['head_sha'], 'main changed after gate execution began')
    try:
        result = evaluate(api, number)
    except (Rejected, KeyError, TypeError, AttributeError, ValueError) as exc:
        result = decision(b, 'BLOCK', [str(exc)[:500]], {})
    same_binding(binding(api.request(f'/pulls/{number}'), api.policy), b)
    value = dict(kind='decision', binding=b, source=source(run), policy_digest=digest(api.policy), decision=result)
    external = f"dcim-acceptance:v1:{run['id']}:{run['run_attempt']}:{number}:" + digest(value)
    checks = api.pages('/commits/' + b['head'] + '/check-runs?filter=all', 'check_runs')
    prior = [c for c in checks if c.get('external_id') == external and c.get('app', {}).get('id') == 15368]
    require(len(prior) <= 1, 'duplicate decision publication')
    body = dict(name=CHECK, head_sha=b['head'], external_id=external, status='completed',
        conclusion='success' if result['verdict'] == 'ACCEPT' else 'failure',
        details_url=f"https://github.com/{api.policy['repository']}/actions/runs/{run['id']}",
        output=dict(title=result['verdict'], summary=json.dumps(result, sort_keys=True)))
    if prior:
        body.pop('head_sha')  # update-check-run has no mutable head SHA.
    check = api.request('/check-runs' + ('/' + str(prior[0]['id']) if prior else ''),
                        'PATCH' if prior else 'POST', body)
    require(check.get('app', {}).get('id') == 15368 and check['app'].get('slug') == 'github-actions', 'wrong acceptance issuer')
    write_record('sealed/evidence.json', value)
    print(json.dumps(dict(pr=number, head=b['head'], base=b['base'], verdict=result['verdict'], reasons=result['reasons'])))


def verify_check(api, number):
    """Consumers MUST call this, not trust the shared Actions App/name alone."""
    _, b = context(api, number)
    checks = api.pages('/commits/' + b['head'] + '/check-runs?filter=all', 'check_runs')
    require(len({c.get('id') for c in checks}) == len(checks), 'duplicate acceptance check identifiers')
    matches = [c for c in checks if c.get('name') == CHECK]
    require(bool(matches), 'acceptance check absent')
    check = max(matches, key=lambda c: c['id'])
    require(check.get('app', {}).get('id') == 15368 and check['app'].get('slug') == 'github-actions'
            and check.get('head_sha') == b['head'] and check.get('status') == 'completed'
            and check.get('conclusion') == 'success', 'forged/failed acceptance check')
    m = re.fullmatch(r'dcim-acceptance:v1:([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*):([0-9a-f]{64})', check.get('external_id', ''))
    require(m is not None and int(m[3]) == number, 'invalid check provenance reference')
    require(sum(c.get('external_id') == check['external_id'] for c in matches) == 1, 'duplicate acceptance evidence')
    run = api.request('/actions/runs/' + m[1])
    spec = api.policy['workflows']['gate']
    candidates = api.pages('/actions/workflows/' + str(api.workflow(spec['path']))
                          + '/runs?head_sha=' + b['base'] + '&branch=' + api.policy['default_branch'], 'workflow_runs')
    require(bool(candidates) and max(candidates, key=lambda r: (r['run_number'], r['id']))['id'] == run['id'],
            'newer gate execution supersedes this check')
    require(run.get('workflow_id') == api.workflow(spec['path']) and run.get('path') == spec['path']
            and run.get('head_sha') == b['base'] and run.get('head_branch') == api.policy['default_branch']
            and run.get('event') in {'workflow_dispatch', 'workflow_run', 'schedule'}
            and run.get('repository', {}).get('full_name') == api.policy['repository']
            and run.get('head_repository', {}).get('full_name') == api.policy['repository']
            and run.get('status') == 'completed' and run.get('conclusion') == 'success'
            and run.get('run_attempt') == int(m[2]), 'compromised gate workflow provenance')
    for path in (spec['path'], '.github/acceptance/policy.json', '.github/scripts/acceptance_core.py',
                 '.github/scripts/acceptance_api.py', '.github/scripts/acceptance_gate.py',
                 '.github/scripts/acceptance_provider_witness.py', '.github/scripts/acceptance_read_guard.py'):
        api.trusted_file(path, run['head_sha'])
    jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
    suite = api.pages(f"/check-suites/{run['check_suite_id']}/check-runs", 'check_runs')
    jobs_and_checks(run, jobs, suite, ['select', f'acceptance-{number}'], api.policy)
    value, _ = api.artifact(run, record_name('acceptance', number, run['run_attempt']))
    same_binding(value.get('binding'), b)
    require(digest(value) == m[4] and value.get('kind') == 'decision'
            and value.get('decision', {}).get('verdict') == 'ACCEPT', 'forged acceptance decision')
    # Even a genuine older ACCEPT cannot survive a new BLOCK, dismissed review,
    # changed base, unresolved finding or failed CI rerun.
    current = evaluate(api, number)
    require(current == value['decision'], 'acceptance evidence changed since publication')
    return current


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'seal', 'attest', 'select', 'publish', 'verify-check'])
    parser.add_argument('--pr', type=int)
    args = parser.parse_args()
    api = GitHub(load_policy())
    try:
        if args.mode == 'prepare': prepare_review(api)
        elif args.mode == 'seal': seal_review(api)
        elif args.mode == 'attest': attest_owner(api)
        elif args.mode == 'select':
            self_run(api, 'gate')
            prs = [args.pr] if args.pr else [p['number'] for p in api.pages('/pulls?state=open', limit=50)
                                            if str(p['number']) in api.policy['implementers']]
            require(len(prs) <= 50 and all(type(n) is int and n > 0 for n in prs), 'invalid/too many PRs')
            with open(os.environ['GITHUB_OUTPUT'], 'a') as out: out.write('prs=' + json.dumps(prs) + '\n')
        elif args.mode == 'publish': publish(api, args.pr)
        else: print(json.dumps(verify_check(api, args.pr), sort_keys=True))
    except (Rejected, KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        print('Acceptance failed closed: ' + str(exc)[:500], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
