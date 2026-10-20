#!/usr/bin/env python3
"""Package the agent's working-tree changes as inert data (collector v1.0).

Runs on the agent runner, which the agent can tamper with, so its output is
treated as untrusted by opencode_publish.py. It never executes repository code.

Usage: opencode_collect.py REPO_DIR BASE_SHA OUT_DIR
Writes OUT_DIR/manifest.json and OUT_DIR/files/<n> (regular-file contents only).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REGULAR = {'100644'}


def git(repo: str, *args: str, env: dict | None = None) -> bytes:
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          env={**os.environ, **(env or {})}).stdout


def parse_raw(raw: bytes) -> list[tuple[str, str, str, str]]:
    """Parse `git diff --raw -z` into (old_mode, new_mode, status, path)."""
    parts = raw.split(b'\0')
    entries = []
    index = 0
    while index + 1 < len(parts) and parts[index]:
        meta = parts[index].decode().lstrip(':').split(' ')
        entries.append((meta[0], meta[1], meta[4][0], parts[index + 1].decode('utf-8', 'surrogateescape')))
        index += 2
    return entries


def collect(repo: str, base_sha: str, out_dir: str) -> dict:
    files_dir = os.path.join(out_dir, 'files')
    os.makedirs(files_dir, exist_ok=True)
    manifest = {'version': 1, 'base_sha': base_sha, 'changes': [], 'unsupported': []}
    with tempfile.TemporaryDirectory() as tmp:
        env = {'GIT_INDEX_FILE': os.path.join(tmp, 'index')}
        git(repo, 'read-tree', base_sha, env=env)
        git(repo, 'add', '-A', env=env)
        raw = git(repo, 'diff', '--cached', '--raw', '-z', '--no-renames', '--no-abbrev', base_sha, env=env)
    for old_mode, new_mode, status, path in parse_raw(raw):
        if status == 'D':
            manifest['changes'].append({'path': path, 'op': 'delete'})
            continue
        exec_ok = status == 'M' and old_mode == new_mode == '100755'
        if new_mode not in REGULAR and not exec_ok:
            manifest['unsupported'].append({'path': path, 'reason': f'mode {old_mode}->{new_mode}'})
            continue
        if status == 'M' and old_mode != new_mode:
            manifest['unsupported'].append({'path': path, 'reason': 'file mode change'})
            continue
        full = os.path.join(repo, path)
        if os.path.islink(full) or not os.path.isfile(full):
            manifest['unsupported'].append({'path': path, 'reason': 'not a regular file'})
            continue
        blob = f'files/{len(manifest["changes"])}'
        shutil.copyfile(full, os.path.join(out_dir, blob))
        with open(full, 'rb') as handle:
            data = handle.read()
        manifest['changes'].append({
            'path': path, 'op': 'add' if status == 'A' else 'modify', 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'blob': blob,
        })
    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2)
    return manifest


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print('usage: opencode_collect.py REPO_DIR BASE_SHA OUT_DIR', file=sys.stderr)
        sys.exit(2)
    result = collect(*sys.argv[1:])
    print(f"collected {len(result['changes'])} change(s), {len(result['unsupported'])} unsupported")
