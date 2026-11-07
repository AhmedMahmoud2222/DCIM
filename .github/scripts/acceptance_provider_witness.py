"""Trusted SessionStart hook: authenticate native App token before revocation.

The model cannot invoke a write tool or supply the witness contents. This hook
is installed by the pinned workflow settings, not candidate code. Its comment is
identity/run evidence only and contains no review verdict or approval.
"""
import json
import os
from pathlib import Path
import re
import subprocess

from acceptance_api import GitHub
from acceptance_core import require, decode, same_binding, provider_identity
from acceptance_gate import load_policy, context


def witness_body(binding, run_id, attempt):
    return 'DCIM native provider identity witness; NOT acceptance.\n' + json.dumps(
        dict(version=1, binding=binding, run_id=run_id, attempt=attempt), sort_keys=True)


# actions/checkout v7 with persist-credentials writes its token to a file under RUNNER_TEMP and points at
# it from the repo-local config (include.path / includeIf.gitdir:<dir>.path). This is the name it uses.
CREDENTIALS_CONFIG = re.compile(r'git-credentials-[0-9a-f-]+\.config$', re.IGNORECASE)
EXTRAHEADER = 'http.https://github.com/.extraheader'


def git(root, *args, check=False):
    return subprocess.run(['git', '-C', str(root), *args], check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def include_paths(root):
    """(key, value) for every repo-local include.path / includeIf.*.path entry."""
    done = subprocess.run(['git', '-C', str(root), 'config', '--local', '-z', '--get-regexp',
                           r'^(include|includeif\..*)\.path$'], capture_output=True, text=True, check=False)
    pairs = []
    for record in done.stdout.split('\0') if done.returncode == 0 else []:
        key, _, value = record.partition('\n')
        if key and value:
            pairs.append((key, value))
    return pairs


def clear_git_credentials(root, repository):
    git(root, 'config', '--local', '--replace-all', 'remote.origin.url',
        'https://github.com/' + repository + '.git', check=True)
    git(root, 'config', '--local', '--unset-all', EXTRAHEADER)
    runner_temp = os.environ.get('RUNNER_TEMP', '')
    for key, value in include_paths(root):
        if CREDENTIALS_CONFIG.search(value):
            # Stop git from reading the token file, then delete it when it sits directly in RUNNER_TEMP.
            git(root, 'config', '--local', '--fixed-value', '--unset-all', key, value)
            target = Path(value)
            if runner_temp and target.is_absolute() and target.parent.resolve() == Path(runner_temp).resolve():
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            # Any other included file must not carry the checkout header either.
            git(root, 'config', '--file', value, '--unset-all', EXTRAHEADER)


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
