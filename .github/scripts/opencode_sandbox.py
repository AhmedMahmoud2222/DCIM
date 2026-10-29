#!/usr/bin/env python3
"""Build the confidentiality boundary for the sandboxed agent (sandbox v3.0).

The model can only see (a) the fixed instruction plus the authorized command text,
and (b) files the agent reads from a sanitized copy of the repository. This module
creates that copy and the agent's managed configuration. It runs as the unprivileged
runner user, never executes repository code, and never prints file contents.

  prepare SRC DEST STATE_DIR AUTHORIZED_SHA ALLOWLIST_FILE
      copy tracked regular files from SRC at the checked-out commit into DEST, dropping
      symlinks, untrusted agent configuration and credential-like files, then fail closed
      when high-confidence secrets remain. Writes STATE_DIR/baseline.git (runner-owned
      git dir used later to diff DEST without trusting any .git inside DEST) and
      STATE_DIR/baseline.json.
  config MODEL PORT [MODE]
      print the managed OpenCode configuration JSON (MODE implement|review|probe).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys

# Agent configuration, skills and commands inside the repo are untrusted input.
UNTRUSTED_CONFIG = ('.opencode', 'opencode.json', 'opencode.jsonc', '.claude', '.agents', '.cursor')
# Credential-like or data-dump files: never copied, never publishable.
SENSITIVE_NAMES = (
    '.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*.jks', '*.kdbx', 'id_rsa*', 'id_ed25519*',
    'id_ecdsa*', '*.sqlite', '*.sqlite3', '*.db', '*.dump', '*.sql.gz', '*.bak', '.netrc', '.npmrc',
    '.pypirc', '*.tfstate', '*.tfstate.*', 'credentials.json', 'service-account*.json', 'kubeconfig',
    '.git-credentials', '*.age', '*.gpg',
)
SENSITIVE_ALLOWED = ('.env.example', '.env.sample', '.env.template')
SECRET_RE = re.compile(
    r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|gh[pousr]_[A-Za-z0-9]{30,}|'
    r'github_pat_[A-Za-z0-9_]{30,}|(?:AKIA|ASIA)[0-9A-Z]{16}|\bsk-[A-Za-z0-9_-]{32,}|'
    r'xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{35}|[sr]k_live_[A-Za-z0-9]{20,}|'
    r'glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|'
    r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|'
    r'AccountKey=[A-Za-z0-9+/=]{40,}')
# Explicit limits on what may reach the model. Anything that cannot be fully read, decoded
# and scanned inside these limits is NOT copied into the sandbox.
MAX_FILE_BYTES = 1024 * 1024          # per file; larger files are excluded
MAX_TOTAL_BYTES = 100 * 1024 * 1024   # whole sandbox copy; exceeding it fails the run
MAX_FILES = 20000
GIT_SAFE = ['-c', 'safe.directory=*', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.attributesFile=/dev/null', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=opencode-sandbox', '-c', 'user.email=sandbox@invalid']


class SandboxError(Exception):
    pass


def is_sensitive_name(path: str) -> bool:
    base = os.path.basename(path).lower()
    if base in SENSITIVE_ALLOWED:
        return False
    return any(fnmatch.fnmatch(base, pattern) for pattern in SENSITIVE_NAMES)


def is_untrusted_config(path: str) -> bool:
    first = path.split('/', 1)[0]
    return first in UNTRUSTED_CONFIG or os.path.basename(path) in ('opencode.json', 'opencode.jsonc')


def classify_file(path: str) -> tuple[str, str]:
    """Return ('ok', '') for a fully scanned UTF-8 text file within limits, ('secret', '') when it
    holds a secret pattern, else ('excluded', reason). The whole file is read: there is no
    scan window, so content after any offset or after NUL bytes is covered or excluded."""
    try:
        size = os.stat(path).st_size
        if size > MAX_FILE_BYTES:
            return 'excluded', 'larger than the per-file limit'
        with open(path, 'rb') as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return 'excluded', 'unreadable'
    if len(data) > MAX_FILE_BYTES:
        return 'excluded', 'larger than the per-file limit'
    if b'\0' in data:
        return 'excluded', 'binary content'
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return 'excluded', 'not valid UTF-8'
    if SECRET_RE.search(text):
        return 'secret', ''
    return 'ok', ''


def scan_for_secrets(root: str, files: list[str], allowlist: set[str]) -> list[str]:
    """Paths among `files` (already copied under root) that contain a secret pattern."""
    return [rel for rel in files if rel not in allowlist
            and classify_file(os.path.join(root, rel))[0] == 'secret']


def run_git(gitdir: str, worktree: str | None, *args: str, env: dict | None = None) -> str:
    cmd = ['git', '--git-dir', gitdir, *(['--work-tree', worktree] if worktree else []), *GIT_SAFE, *args]
    clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    clean.update({'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
                  'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'})
    clean.update(env or {})
    return subprocess.run(cmd, check=True, capture_output=True, env=clean).stdout.decode()


def prepare(src: str, dest: str, state: str, authorized_sha: str, allowlist: set[str]) -> dict:
    tracked = subprocess.run(['git', '-C', src, 'ls-files', '-z'], check=True,
                             capture_output=True).stdout.split(b'\0')
    copied, excluded, secret_hits, total = [], [], [], 0
    os.makedirs(dest, exist_ok=True)
    for raw in tracked:
        if not raw:
            continue
        rel = raw.decode('utf-8', 'surrogateescape')
        source = os.path.join(src, rel)
        if os.path.islink(source) or not os.path.isfile(source):
            excluded.append({'path': rel, 'reason': 'symlink or non-regular file'})
            continue
        if is_untrusted_config(rel):
            excluded.append({'path': rel, 'reason': 'untrusted agent configuration'})
            continue
        if is_sensitive_name(rel):
            excluded.append({'path': rel, 'reason': 'credential-like file name'})
            continue
        verdict, reason = classify_file(source)
        if verdict == 'secret':
            if rel not in allowlist:
                secret_hits.append(rel)
                continue
        elif verdict == 'excluded':
            excluded.append({'path': rel, 'reason': reason})
            continue
        total += os.path.getsize(source)
        if len(copied) + 1 > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise SandboxError('repository exceeds the sandbox size limits; input cannot be bounded, '
                               'stopping before any model call')
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o755 if os.stat(source).st_mode & 0o111 else 0o644)
        copied.append(rel)
    if secret_hits:
        raise SandboxError('secret-like content found in ' + ', '.join(secret_hits[:10]) +
                           '; confidentiality cannot be established, stopping before any model call')
    gitdir = os.path.join(state, 'baseline.git')
    os.makedirs(state, exist_ok=True)
    subprocess.run(['git', 'init', '--bare', '-q', gitdir], check=True, capture_output=True)
    index = os.path.join(state, 'baseline.index')
    run_git(gitdir, dest, 'add', '-A', '-f', env={'GIT_INDEX_FILE': index})
    run_git(gitdir, dest, 'commit', '-q', '-m', 'sanitized baseline', '--allow-empty',
            env={'GIT_INDEX_FILE': index})
    baseline = run_git(gitdir, None, 'rev-parse', 'HEAD').strip()
    meta = {'authorized_sha': authorized_sha, 'baseline_commit': baseline, 'copied': len(copied),
            'excluded': excluded}
    with open(os.path.join(state, 'baseline.json'), 'w', encoding='utf-8') as handle:
        json.dump(meta, handle, indent=2)
    return meta


def agent_config(model: str, port: int, mode: str = 'implement') -> dict:
    """Managed configuration. Written to /etc/opencode so project config cannot override it.

    Review mode denies every editing tool; the work tree is also made read-only at the OS level.
    """
    if mode not in ('implement', 'review', 'probe'):
        raise SandboxError('unknown agent mode')
    if not re.fullmatch(r'opencode/[a-z0-9.-]+-free', model):
        raise SandboxError('model is not an approved free OpenCode model identifier')
    permission = {
        'read': 'deny' if mode == 'probe' else {'*': 'allow', '*.env': 'deny', '*.env.*': 'deny', '*.env.example': 'allow'},
        'edit': 'deny' if mode in ('review', 'probe') else 'allow',
        'glob': 'deny' if mode == 'probe' else 'allow',
        'grep': 'deny' if mode == 'probe' else 'allow',
        'list': 'deny' if mode == 'probe' else 'allow',
        'bash': 'deny' if mode == 'probe' else 'allow',
        'task': 'deny', 'webfetch': 'deny', 'websearch': 'deny',
        'external_directory': 'deny', 'doom_loop': 'deny',
    }
    return {
        '$schema': 'https://opencode.ai/config.json',
        'autoupdate': False,
        'update': 'disable',
        'share': 'disabled',
        'enabled_providers': ['opencode'],
        'model': model,
        # OpenCode makes side requests (session titles) with its `small_model`. Without this it picks a
        # different model on a different API, which the model-bound proxy would have to refuse.
        'small_model': model,
        'provider': {'opencode': {'options': {
            'baseURL': f'http://127.0.0.1:{int(port)}/inference/openai/v1', 'apiKey': 'sandbox-placeholder'}}},
        'permission': permission,
        'permissions': v2_permissions(mode),
    }


def v2_permissions(mode: str) -> list[dict]:
    """OpenCode v2 rule list (last matching rule wins; evaluated by the real binary in the e2e tests).

    v2 headless runs auto-approve any action with no matching rule, and its Code Mode `execute` tool,
    `subagent`, `skill` and `question` actions are not covered by the v1-style `permission` keys. The list
    therefore opens with a catch-all deny so an action added by a future release is denied until reviewed,
    then allows only what the mode needs. `external_directory` is denied last so no allow can reopen it."""
    def rule(action: str, resource: str, effect: str) -> dict:
        return {'action': action, 'resource': resource, 'effect': effect}

    rules = [rule('*', '*', 'deny')]
    if mode != 'probe':
        rules += [rule('read', '*', 'allow'), rule('read', '*.env', 'deny'), rule('read', '*.env.*', 'deny'),
                  rule('read', '*.env.example', 'allow'), rule('glob', '*', 'allow'), rule('grep', '*', 'allow'),
                  rule('shell', '*', 'allow')]
    if mode == 'implement':
        rules.append(rule('edit', '*', 'allow'))
    rules += [rule(action, '*', 'deny') for action in
              ('execute', 'subagent', 'skill', 'question', 'webfetch', 'websearch', 'external_directory')]
    return rules


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 7 and argv[1] == 'prepare':
            allowlist = set()
            if os.path.isfile(argv[6]):
                with open(argv[6], encoding='utf-8') as handle:
                    allowlist = {line.strip() for line in handle if line.strip() and not line.startswith('#')}
            meta = prepare(argv[2], argv[3], argv[4], argv[5], allowlist)
            print(f"sandbox: copied {meta['copied']} files, excluded {len(meta['excluded'])}")
            return 0
        if len(argv) in (4, 5) and argv[1] == 'config':
            print(json.dumps(agent_config(argv[2], int(argv[3]), argv[4] if len(argv) == 5 else 'implement')))
            return 0
    except (SandboxError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f'::error::sandbox preparation stopped: {error}')
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
