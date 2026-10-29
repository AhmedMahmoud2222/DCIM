#!/usr/bin/env python3
"""Fail-closed PreToolUse file-tool boundary for the Claude agent (file guard v1.0).

--allowedTools is an auto-approval rule, not a filesystem boundary. This trusted hook, installed
from the workflow commit outside the workspace, permits only Read/Glob/Grep/Edit/Write whose
canonical (symlink-resolved) target stays inside the sanitized PR checkout and outside git and
agent/workflow configuration. Every other tool, argument, malformed event or error is denied.

Root: CLAUDE_FILE_GUARD_ROOT, else $GITHUB_WORKSPACE/pr.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = {'Edit', 'Write'}
KEYS = {
    'Read': {'file_path', 'offset', 'limit'},
    'Edit': {'file_path', 'old_string', 'new_string', 'replace_all'},
    'Write': {'file_path', 'content'},
    'Glob': {'pattern', 'path'},
    'Grep': {'pattern', 'path', 'glob', 'type', 'output_mode', '-i', '-n', '-A', '-B', '-C',
             'context', 'head_limit', 'offset', 'multiline'},
}
DENY_COMPONENTS = {'.git', '.claude', '.husky'}
DENY_ROOT_FILES = {'.mcp.json', '.claude.json', '.npmrc', 'bunfig.toml', '.ripgreprc', '.gitmodules'}
DENY_WRITE_NAMES = {'claude.md', 'claude.local.md'}


def root_dir() -> Path:
    explicit = os.environ.get('CLAUDE_FILE_GUARD_ROOT')
    if not explicit:
        workspace = os.environ.get('GITHUB_WORKSPACE')
        if not workspace:
            raise ValueError('no guard root')
        explicit = os.path.join(workspace, 'pr')
    return Path(explicit).resolve(strict=True)


def safe_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and '\x00' not in value and len(value) < 4096


def inside(path_value: object, root: Path, write: bool) -> bool:
    if not safe_text(path_value):
        return False
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    # realpath resolves symlinks in every existing component (writes may create the leaf).
    real = Path(os.path.realpath(path))
    try:
        rel = real.relative_to(root)
    except ValueError:
        return False
    parts = [p.lower() for p in rel.parts]
    if '..' in parts or set(parts) & DENY_COMPONENTS:
        return False
    if len(parts) == 1 and parts[0] in DENY_ROOT_FILES:
        return False
    if write and (parts and (parts[0] == '.github' or parts[-1] in DENY_WRITE_NAMES)):
        return False
    return True


def safe_pattern(value: object) -> bool:
    return safe_text(value) and not value.startswith(('/', '~')) and '\\' not in value \
        and '..' not in value.split('/')


def allowed(event: object, root: Path) -> bool:
    if not isinstance(event, dict) or event.get('hook_event_name', 'PreToolUse') != 'PreToolUse':
        return False
    tool, args = event.get('tool_name'), event.get('tool_input')
    if tool not in KEYS or not isinstance(args, dict) or set(args) - KEYS[tool]:
        return False
    if tool in ('Read', 'Edit', 'Write'):
        return inside(args.get('file_path'), root, tool in WRITE_TOOLS)
    # Glob and Grep must name their directory explicitly: the default is the workspace root.
    if not inside(args.get('path'), root, False):
        return False
    if not safe_pattern(args.get('pattern')) and tool == 'Glob':
        return False
    if tool == 'Grep' and 'glob' in args and not safe_pattern(args['glob']):
        return False
    return True


def main() -> None:
    try:
        raw = sys.stdin.read(1048577)
        permit = len(raw) <= 1048576 and allowed(json.loads(raw), root_dir())
    except Exception:
        permit = False
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow' if permit else 'deny',
        'permissionDecisionReason': 'DCIM claude PR-checkout file boundary'}}))


if __name__ == '__main__':
    main()
