#!/usr/bin/env python3
"""Package the agent's working-tree changes as inert data (collector v3.0).

Runs as the runner user after a successful generation. The diff is computed against a
runner-owned baseline git dir, with the sandbox only as a work tree, so no config,
hook or filter inside the agent-writable directory is ever consulted or executed.

Usage: opencode_collect.py WORKTREE STATE_DIR OUT_DIR
STATE_DIR holds baseline.git and baseline.json written by opencode_sandbox.py prepare.
The manifest records the authorized SHA from baseline.json; the publisher compares it
with the gate's trusted value and the live branch, and never relies on it alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode_fsisolate as fsisolate  # noqa: E402
import opencode_sandbox as sandbox  # noqa: E402


def parse_raw(raw: bytes) -> list[tuple[str, str, str, str]]:
    """Parse `git diff --raw -z` into (old_mode, new_mode, status, path)."""
    parts = raw.split(b'\0')
    entries, index = [], 0
    while index + 1 < len(parts) and parts[index]:
        meta = parts[index].decode().lstrip(':').split(' ')
        entries.append((meta[0], meta[1], meta[4][0], parts[index + 1].decode('utf-8', 'surrogateescape')))
        index += 2
    return entries


def collect(worktree: str, state: str, out_dir: str) -> dict:
    # The work tree must be the real directory on a root-controlled path. If the agent could
    # swap it for a symlink, this runner-user process would read and publish unrelated files.
    if os.path.islink(worktree) or os.path.realpath(worktree) != os.path.abspath(worktree):
        raise sandbox.SandboxError('work tree path is a symlink or contains one')
    problems = (fsisolate.chain_problems(worktree, os.lstat(worktree).st_uid)
                if os.environ.get('OPENCODE_REQUIRE_ROOT_CHAIN') == '1' else [])  # set by the workflow layout
    if problems:
        raise sandbox.SandboxError('work tree path is not controlled by root: ' + '; '.join(problems[:3]))
    with open(os.path.join(state, 'baseline.json'), encoding='utf-8') as handle:
        meta = json.load(handle)
    gitdir, baseline = os.path.join(state, 'baseline.git'), meta['baseline_commit']
    files_dir = os.path.join(out_dir, 'files')
    os.makedirs(files_dir, exist_ok=True)
    manifest = {'version': 2, 'base_sha': meta['authorized_sha'], 'changes': [], 'unsupported': []}
    with tempfile.TemporaryDirectory() as tmp:
        env = {'GIT_INDEX_FILE': os.path.join(tmp, 'index')}
        sandbox.run_git(gitdir, worktree, 'read-tree', baseline, env=env)
        sandbox.run_git(gitdir, worktree, 'add', '-A', env=env)
        raw = sandbox.run_git(gitdir, worktree, 'diff', '--cached', '--raw', '-z', '--no-renames',
                              '--no-abbrev', baseline, env=env).encode('utf-8', 'surrogateescape')
    for old_mode, new_mode, status, path in parse_raw(raw):
        if status == 'D':
            manifest['changes'].append({'path': path, 'op': 'delete'})
            continue
        exec_ok = status == 'M' and old_mode == new_mode == '100755'
        if new_mode != '100644' and not exec_ok:
            manifest['unsupported'].append({'path': path, 'reason': f'mode {old_mode}->{new_mode}'})
            continue
        full = os.path.join(worktree, path)
        if os.path.islink(full) or not os.path.isfile(full):
            manifest['unsupported'].append({'path': path, 'reason': 'not a regular file'})
            continue
        blob = f'files/{len(manifest["changes"])}'
        shutil.copyfile(full, os.path.join(out_dir, blob))
        with open(os.path.join(out_dir, blob), 'rb') as handle:
            data = handle.read()
        manifest['changes'].append({
            'path': path, 'op': 'add' if status == 'A' else 'modify', 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'blob': blob})
    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2)
    return manifest


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    result = collect(*sys.argv[1:])
    print(f"collected {len(result['changes'])} change(s), {len(result['unsupported'])} unsupported")
