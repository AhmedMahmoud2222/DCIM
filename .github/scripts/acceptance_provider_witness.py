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
# it from the repo-local config (include.path / includeIf.gitdir:<dir>.path). This is the whole basename.
CREDENTIALS_CONFIG = re.compile(r'git-credentials-[0-9a-f-]+\.config', re.IGNORECASE)
EXTRAHEADER = 'http.https://github.com/.extraheader'
INCLUDE_KEYS = r'^(include|includeif\..*)\.path$'
MAX_INCLUDE_DEPTH = 10  # git refuses to read a deeper include chain (config.c MAX_INCLUDE_DEPTH)
ABSENT = 5  # git config --unset-all exits 5 when the key is not set; any other nonzero code is a real failure


def git(root, *args, ok=(0,)):
    done = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False)
    require(done.returncode in ok, f'credential cleanup failed: git config exit {done.returncode}')


def include_pointers(root, config_file=None):
    """(key, value) for every include.path / includeIf.*.path entry in the repo-local config or one file."""
    where = ['--file', str(config_file)] if config_file else ['--local']
    done = subprocess.run(['git', '-C', str(root), 'config', *where, '-z', '--get-regexp', INCLUDE_KEYS],
                          capture_output=True, text=True, check=False)
    require(done.returncode in (0, 1), 'credential cleanup could not read include entries')  # 1: no match
    pairs = []
    for record in done.stdout.split('\0') if done.returncode == 0 else []:
        key, _, value = record.partition('\n')
        if key and value:
            pairs.append((key, value))
    return pairs


def git_common_dir(root):
    done = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                          capture_output=True, text=True, check=False)
    require(done.returncode == 0 and done.stdout.strip(), 'credential cleanup could not locate the git directory')
    return Path(done.stdout.strip())


def resolve_include(value, base_dir):
    """Git's include-path semantics: ~ expands to the home directory, a relative path is relative to the
    directory of the config file that contains the include."""
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else Path(base_dir) / path


def scrub_includes(root, config_file, base_dir, runner_temp, seen, depth):
    for key, value in include_pointers(root, config_file):
        target = resolve_include(value, base_dir)
        owner = ['--file', str(config_file)] if config_file else ['--local']
        if CREDENTIALS_CONFIG.fullmatch(target.name):
            # Stop git from reading the token file, then delete it when it sits directly in RUNNER_TEMP.
            git(root, 'config', *owner, '--fixed-value', '--unset-all', key, value, ok=(0, ABSENT))
            if runner_temp and target.is_absolute() and target.parent.resolve() == Path(runner_temp).resolve():
                target.unlink(missing_ok=True)  # a failed delete leaves the token on disk: let it raise
        elif target.is_file():
            require(depth < MAX_INCLUDE_DEPTH, 'credential cleanup cannot inspect an include chain this deep')
            real = target.resolve()
            if real in seen:
                continue
            seen.add(real)
            # Any other included file must not carry the checkout header either, nor may files it includes.
            git(root, 'config', '--file', str(target), '--unset-all', EXTRAHEADER, ok=(0, ABSENT))
            scrub_includes(root, target, target.parent, runner_temp, seen, depth + 1)


def clear_git_credentials(root, repository):
    git(root, 'config', '--local', '--replace-all', 'remote.origin.url',
        'https://github.com/' + repository + '.git')
    git(root, 'config', '--local', '--unset-all', EXTRAHEADER, ok=(0, ABSENT))
    scrub_includes(root, None, git_common_dir(root), os.environ.get('RUNNER_TEMP', ''), set(), 0)


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
