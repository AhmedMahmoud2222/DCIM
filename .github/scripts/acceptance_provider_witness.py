"""Trusted SessionStart hook: authenticate native App token before revocation.

The model cannot invoke a write tool or supply the witness contents. This hook
is installed by the pinned workflow settings, not candidate code. Its comment is
identity/run evidence only and contains no review verdict or approval.
"""
import json
import os
from pathlib import Path
import subprocess

from acceptance_api import GitHub
from acceptance_core import require, decode, same_binding, provider_identity
from acceptance_gate import load_policy, context


def witness_body(binding, run_id, attempt):
    return 'DCIM native provider identity witness; NOT acceptance.\n' + json.dumps(
        dict(version=1, binding=binding, run_id=run_id, attempt=attempt), sort_keys=True)


def clear_git_credentials(root, repository):
    subprocess.run(['git', '-C', str(root), 'config', '--local', '--replace-all',
                    'remote.origin.url', 'https://github.com/' + repository + '.git'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['git', '-C', str(root), 'config', '--local', '--unset-all',
                    'http.https://github.com/.extraheader'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    api = GitHub(load_policy())
    # Do this before any network operation that could fail non-fatally as a hook.
    clear_git_credentials(api.root, api.policy['repository'])
    trust_sha = api.runtime(api.policy['workflows']['review']['path'])
    b = decode(os.environ['REVIEW_BINDING'])
    _, current = context(api, b['pr'])
    same_binding(b, current)
    require(b['base'] == trust_sha, 'stale provider witness')
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    require(run_id > 0 and attempt > 0, 'invalid provider execution')
    path = Path(os.environ['RUNNER_TEMP']) / f'acceptance-witness-{run_id}-{attempt}.json'
    expected = witness_body(b, run_id, attempt)
    provider = api.policy['providers'][api.policy['reviewer']]
    # Native prepareAgentMode persists its App token in origin. Remove it before
    # the first model turn. PreToolUse separately denies ALL reads outside input.
    if path.exists():
        saved = decode(path.read_bytes())
        comment = api.request('/issues/comments/' + str(saved['id']))
        require(comment.get('body') == expected and provider_identity(comment, provider), 'invalid prior witness')
        return 0
    # Consumed before POST. An ambiguous failure never retries a duplicate write.
    with path.open('x') as handle:
        os.chmod(path, 0o600)
        handle.write('{}')
    comment = api.request(f"/issues/{b['pr']}/comments", 'POST', dict(body=expected))
    require(provider_identity(comment, provider), 'native token is not the pinned provider App/actor')
    require(comment.get('body') == expected and type(comment.get('id')) is int, 'invalid native witness response')
    path.write_text(json.dumps(dict(id=comment['id'])))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # Hooks may be non-fatal to the CLI; seal independently requires the saved
        # authenticated witness. Never log exception text or credential values.
        raise SystemExit('Native provider witness failed closed') from None
