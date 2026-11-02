"""Immutable native execution provenance with current revocation policy.

An ancestor of current main is not automatically trusted. Its signed lineage,
runtime source manifest and historical policy must also remain authorized.
This preserves execution evidence, never approval for a changed task binding.
"""
import re

from acceptance_core import require, decode, digest

POLICY = '.github/agents/collaboration-policy.json'


def security_manifest(api, sha):
    tree = api.request('/git/trees/' + sha + '?recursive=1')
    require(tree.get('truncated') is False and isinstance(tree.get('tree'), list), 'incomplete source manifest')
    files = {}
    for item in tree['tree']:
        path = item.get('path', '')
        if path == POLICY or not path.startswith(('.github/', 'tooling/opencode/', 'tests/deployment/')):
            continue
        require(isinstance(path, str) and path not in files, 'ambiguous runtime manifest')
        require(item.get('type') in {'blob', 'tree'} and item.get('mode') in {'100644', '100755', '040000'},
                'unsupported runtime source type')
        if item['type'] == 'blob':
            require(re.fullmatch(r'[0-9a-f]{40}', item.get('sha', '')), 'invalid source object')
            files[path] = dict(sha=item['sha'], mode=item['mode'])
    require(bool(files), 'empty source manifest')
    return files


def operational_policy(policy):
    # Observation pointers and reviewed source authorizations can be refreshed
    # without changing who is trusted or what an execution is permitted to do.
    value = {k: v for k, v in policy.items() if k != 'execution_provenance'}
    value['providers'] = {name: {k: v for k, v in info.items() if k != 'proof'}
                          for name, info in policy['providers'].items()}
    return value


def verified_commit(api, sha):
    require(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha), 'invalid source revision')
    commit = api.request('/commits/' + sha)
    v = commit.get('commit', {}).get('verification', {})
    require(commit.get('sha') == sha and v.get('verified') is True and v.get('reason') == 'valid',
            'unverified workflow source commit')


def execution_policy(api, policy, source_sha, current_sha):
    control = policy.get('execution_provenance')
    require(isinstance(control, dict) and set(control) == {'revoked_revisions', 'approved_manifests'},
            'missing execution revocation policy')
    revoked, approved = control['revoked_revisions'], control['approved_manifests']
    require(isinstance(revoked, list) and len(set(revoked)) == len(revoked)
            and all(isinstance(s, str) and re.fullmatch(r'[0-9a-f]{40}', s) for s in revoked)
            and isinstance(approved, dict) and all(re.fullmatch(r'[0-9a-f]{40}', s)
            and isinstance(h, str) and re.fullmatch(r'[0-9a-f]{64}', h) for s, h in approved.items()),
            'invalid execution revocation policy')
    require(source_sha not in revoked, 'workflow source revision revoked')
    verified_commit(api, source_sha)
    if source_sha == current_sha:
        return policy
    verified_commit(api, current_sha)
    comparison = api.request('/compare/' + source_sha + '...' + current_sha)
    commits = comparison.get('commits')
    require(comparison.get('base_commit', {}).get('sha') == source_sha
            and comparison.get('merge_base_commit', {}).get('sha') == source_sha
            and comparison.get('status') == 'ahead' and comparison.get('behind_by') == 0
            and isinstance(commits, list) and 0 < len(commits) == comparison.get('total_commits') <= 250,
            'source is not a completely evidenced ancestor of current main')
    shas = [c.get('sha') for c in commits]
    require(len(set(shas)) == len(shas) and shas[-1] == current_sha, 'ambiguous source lineage')
    # Re-fetch signatures rather than relying on truncated comparison summaries.
    for sha in shas:
        verified_commit(api, sha)
    historical = decode(api.file(POLICY, source_sha))
    require(operational_policy(historical) == operational_policy(policy), 'execution authority changed; fresh proof required')
    original = security_manifest(api, source_sha)
    current = security_manifest(api, current_sha)
    require(original == current or approved.get(source_sha) == digest(original),
            'security-sensitive execution source changed; reviewed manifest approval required')
    return historical
