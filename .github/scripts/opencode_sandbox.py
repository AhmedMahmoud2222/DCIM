#!/usr/bin/env python3
"""Build the confidentiality boundary for the sandboxed agent (sandbox v1.0).

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
  config MODEL PORT
      print the managed OpenCode configuration JSON.
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
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|'
    r'\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9]{32,}|\bxox[baprs]-[A-Za-z0-9-]{20,}|'
    r'\bAIza[0-9A-Za-z_-]{35}\b')
MAX_SCAN_BYTES = 2 * 1024 * 1024
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


def scan_for_secrets(root: str, files: list[str], allowlist: set[str]) -> list[str]:
    """Return paths whose content matches a high-confidence secret pattern (never the match)."""
    hits = []
    for rel in files:
        if rel in allowlist:
            continue
        with open(os.path.join(root, rel), 'rb') as handle:
            data = handle.read(MAX_SCAN_BYTES)
        if b'\0' in data[:2048]:
            continue
        if SECRET_RE.search(data.decode('utf-8', 'ignore')):
            hits.append(rel)
    return hits


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
    copied, excluded = [], []
    os.makedirs(dest, exist_ok=True)
    for raw in tracked:
        if not raw:
            continue
        rel = raw.decode('utf-8', 'surrogateescape')
        source = os.path.join(src, rel)
        if os.path.islink(source) or not os.path.isfile(source):
            excluded.append({'path': rel, 'reason': 'symlink or non-regular file'})
        elif is_untrusted_config(rel):
            excluded.append({'path': rel, 'reason': 'untrusted agent configuration'})
        elif is_sensitive_name(rel):
            excluded.append({'path': rel, 'reason': 'credential-like file name'})
        else:
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o755 if os.stat(source).st_mode & 0o111 else 0o644)
            copied.append(rel)
    hits = scan_for_secrets(dest, copied, allowlist)
    if hits:
        raise SandboxError('secret-like content found in ' + ', '.join(hits[:10]) +
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


def agent_config(model: str, port: int) -> dict:
    """Managed configuration. Written to /etc/opencode so project config cannot override it."""
    if not re.fullmatch(r'opencode/[a-z0-9.-]+-free', model):
        raise SandboxError('model is not an approved free OpenCode model identifier')
    return {
        '$schema': 'https://opencode.ai/config.json',
        'autoupdate': False,
        'share': 'disabled',
        'enabled_providers': ['opencode'],
        'model': model,
        'provider': {'opencode': {'options': {
            'baseURL': f'http://127.0.0.1:{int(port)}/inference/openai/v1', 'apiKey': 'sandbox-placeholder'}}},
        'permission': {
            'read': {'*': 'allow', '*.env': 'deny', '*.env.*': 'deny', '*.env.example': 'allow'},
            'edit': 'allow', 'glob': 'allow', 'grep': 'allow', 'list': 'allow', 'bash': 'allow',
            'task': 'deny', 'webfetch': 'deny', 'websearch': 'deny',
            'external_directory': 'deny', 'doom_loop': 'deny',
        },
    }


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
        if len(argv) == 4 and argv[1] == 'config':
            print(json.dumps(agent_config(argv[2], int(argv[3]))))
            return 0
    except (SandboxError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f'::error::sandbox preparation stopped: {error}')
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
