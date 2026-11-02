"""Exclusive model-tool boundary; allow only prepared inert review data.

--allowedTools alone is an auto-approval rule, not a filesystem boundary. This
trusted PreToolUse hook denies every other tool/path, including .git/config,
process environments, credentials, network tools and shell execution.
"""
import json
from pathlib import Path
import sys


def allowed(event, root):
    tool = event.get('tool_name')
    if tool == 'StructuredOutput':
        return True  # Native schema result transport; no IO or command execution.
    if tool != 'Read':
        return False
    value = event.get('tool_input') or {}
    if not isinstance(value, dict) or set(value) - {'file_path', 'offset', 'limit'}:
        return False
    raw = value.get('file_path')
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        return False
    target = Path(root) / 'review-input/context.json'
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        return (not target.is_symlink() and not target.parent.is_symlink()
                and target.is_file() and target.stat().st_size <= 524288
                and path.resolve(strict=True) == target.resolve(strict=True))
    except (OSError, ValueError):
        return False


def main():
    try:
        raw = sys.stdin.read(1048577)
        event = json.loads(raw) if len(raw) <= 1048576 else {}
        permit = isinstance(event, dict) and allowed(event, Path(__file__).resolve().parents[2])
    except Exception:
        permit = False
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow' if permit else 'deny',
        'permissionDecisionReason': 'DCIM immutable review-context boundary'}}))


if __name__ == '__main__':
    main()
