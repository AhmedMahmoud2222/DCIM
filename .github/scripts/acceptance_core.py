"""Strict evidence validation. No comment body or check name confers authority."""
import hashlib
import json
import re

SHA = re.compile(r'[0-9a-f]{40}\Z')
CHECK = 'DCIM independent acceptance'


class Rejected(ValueError):
    pass


def require(value, reason):
    if not value:
        raise Rejected(reason)


def unique(pairs):
    data = {}
    for key, value in pairs:
        require(key not in data, 'duplicate JSON key')
        data[key] = value
    return data


def decode(raw):
    require(isinstance(raw, (str, bytes)) and len(raw) <= 524288, 'oversized/missing evidence')
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Rejected('non-finite JSON')))
    except (TypeError, ValueError) as error:
        raise Rejected('invalid JSON evidence') from error


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def binding(pr, policy):
    require(type(pr.get('number')) is int and pr['number'] > 0, 'invalid PR')
    require(pr.get('state') == 'open' and pr.get('merged') is not True, 'PR is not open')
    for side in ('head', 'base'):
        obj = pr.get(side) or {}
        require(obj.get('repo', {}).get('full_name') == policy['repository'], 'untrusted PR repository')
        require(isinstance(obj.get('sha'), str) and SHA.fullmatch(obj['sha']), 'invalid commit binding')
    require(pr['base'].get('ref') == policy['default_branch'], 'unauthorized base branch')
    return dict(repository=policy['repository'], pr=pr['number'], head=pr['head']['sha'], base=pr['base']['sha'])


def same_binding(value, expected):
    require(isinstance(value, dict) and value == expected, 'wrong/stale PR HEAD or base binding')


def provider_identity(comment, provider):
    user, app = comment.get('user') or {}, comment.get('performed_via_github_app') or {}
    return (type(user.get('id')) is int and user.get('id') == provider['id']
            and user.get('login') == provider['login'] and user.get('type') == 'Bot'
            and type(app.get('id')) is int and app.get('id') == provider['app_id']
            and app.get('slug') == provider['app_slug'])


def independent(policy, pr_author_id):
    implementer = policy['implementers'].get(str(policy['_binding']['pr']))
    reviewer = policy['reviewer']
    require(implementer in policy['providers'] and reviewer in policy['providers'], 'unknown task implementer/reviewer')
    i, r = policy['providers'][implementer], policy['providers'][reviewer]
    require(implementer != reviewer and i['id'] != r['id'] and i['app_id'] != r['app_id'], 'self review')
    require(r['id'] != pr_author_id, 'PR author cannot independently accept')
    return implementer, reviewer


def structured_review(value, expected, reviewer):
    require(isinstance(value, dict) and set(value) == {'binding', 'reviewer', 'verdict', 'findings', 'resolved_finding_ids'},
            'unknown/missing review fields')
    same_binding(value['binding'], expected)
    require(value['reviewer'] == reviewer, 'wrong reviewer')
    require(value['verdict'] in {'ACCEPT', 'BLOCK'}, 'missing explicit verdict')
    require(isinstance(value['findings'], list) and len(value['findings']) <= 100, 'invalid findings')
    ids = set()
    for f in value['findings']:
        require(isinstance(f, dict) and set(f) == {'id', 'blocking', 'message'}, 'invalid finding schema')
        require(isinstance(f['id'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', f['id']), 'invalid finding id')
        require(f['id'] not in ids and type(f['blocking']) is bool, 'duplicate/invalid finding')
        require(isinstance(f['message'], str) and 1 <= len(f['message']) <= 4000, 'invalid finding message')
        ids.add(f['id'])
    resolved = value['resolved_finding_ids']
    require(isinstance(resolved, list) and len(resolved) <= 1000
            and all(isinstance(i, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', i) for i in resolved), 'invalid resolutions')
    require(len(set(resolved)) == len(resolved) and not ids.intersection(resolved), 'duplicate/contradictory resolutions')
    require(value['verdict'] != 'ACCEPT' or not any(f['blocking'] for f in value['findings']), 'unresolved blocking findings')
    return value


def source_run(run, spec, workflow_id, policy, trust_sha, completed=True):
    require(run.get('workflow_id') == workflow_id and type(workflow_id) is int, 'wrong workflow id')
    require(run.get('path') == spec['path'], 'wrong workflow path')
    require(run.get('repository', {}).get('full_name') == policy['repository']
            and run.get('head_repository', {}).get('full_name') == policy['repository'], 'wrong workflow repository')
    require(run.get('head_branch') == policy['default_branch'] and run.get('head_sha') == trust_sha,
            'workflow did not execute trusted default-branch revision')
    require(run.get('event') == spec['event'], 'untrusted workflow trigger')
    for actor in ('actor', 'triggering_actor'):
        require(run.get(actor, {}).get('id') == policy['owner']['id']
                and run[actor].get('login') == policy['owner']['login']
                and run[actor].get('type') == 'User', 'unauthorized workflow actor')
    require(type(run.get('id')) is int and run['id'] > 0 and type(run.get('run_attempt')) is int
            and run['run_attempt'] > 0 and type(run.get('check_suite_id')) is int, 'invalid workflow attempt')
    if completed:
        require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'source execution failed/incomplete')


def jobs_and_checks(run, jobs, checks, required, policy):
    require(isinstance(jobs, list) and isinstance(checks, list), 'missing execution evidence')
    by_id = {c.get('id'): c for c in checks}
    require(len(by_id) == len(checks), 'duplicate checks')
    for name in required:
        matches = [j for j in jobs if j.get('name') == name]
        require(len(matches) == 1, 'missing/duplicate required job: ' + name)
        job = matches[0]
        match = re.fullmatch(r'https://api.github.com/repos/' + re.escape(policy['repository']) + r'/check-runs/(\d+)',
                             job.get('check_run_url', ''))
        check = by_id.get(int(match.group(1))) if match else None
        require(check is not None, 'job/check link missing')
        require(job.get('run_id') == run['id'] and job.get('run_attempt') == run['run_attempt'], 'stale job attempt')
        for obj in (job, check):
            require(obj.get('head_sha') == run['head_sha'] and obj.get('status') == 'completed'
                    and obj.get('conclusion') == 'success', 'failed/stale execution')
        require(check.get('name') == name and check.get('app', {}).get('id') == policy['actions_app']['id']
                and check.get('app', {}).get('slug') == policy['actions_app']['slug']
                and check.get('check_suite', {}).get('id') == run['check_suite_id'], 'forged App/check suite')


def signed_range(compare, commits, expected):
    require(compare.get('status') == 'ahead' and compare.get('behind_by') == 0
            and compare.get('merge_base_commit', {}).get('sha') == expected['base'], 'base is not an ancestor')
    require(type(compare.get('total_commits')) is int and 0 < compare['total_commits'] == len(commits),
            'incomplete commit range')
    shas = [c.get('sha') for c in commits]
    require(all(isinstance(s, str) and SHA.fullmatch(s) for s in shas) and len(set(shas)) == len(shas)
            and shas[-1] == expected['head'] and expected['base'] not in shas, 'wrong/duplicate commit range')
    for c in commits:
        v = c.get('commit', {}).get('verification', {})
        require(v.get('verified') is True and v.get('reason') == 'valid', 'invalid commit signature')


def owner_approval(approvals, environment, policy):
    require(isinstance(approvals, list), 'missing owner approval audit')
    approved = [a for a in approvals if any(e.get('name') == environment for e in a.get('environments', []))]
    require(len(approved) == 1 and approved[0].get('state') == 'approved', 'missing/ambiguous environment approval')
    user = approved[0].get('user') or {}
    require(user.get('id') == policy['owner']['id'] and user.get('login') == policy['owner']['login']
            and user.get('type') == 'User', 'owner did not attest')


def decision(expected, verdict, reasons, evidence):
    require(verdict in {'ACCEPT', 'BLOCK'}, 'invalid decision')
    return dict(version=1, binding=expected, verdict=verdict, reasons=reasons, evidence=evidence,
                merge_enabled=False, deployment_enabled=False)
