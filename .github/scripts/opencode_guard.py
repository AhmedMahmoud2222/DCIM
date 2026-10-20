#!/usr/bin/env python3
"""Run the sandboxed agent and decide whether generation truly succeeded (guard v1.0).

Uses `opencode run --format json`, which only generates in the working tree: it makes
no commit, push, PR or comment. Generation counts as successful only when ALL hold:
  - the process exited 0 and did not time out
  - every stdout line parsed as a JSON event
  - no `error` event was emitted
  - at least one `step_finish` event arrived and the last one has reason "stop"
Anything else (provider error, timeout, crash, truncated or tool-call-pending output)
is a failure, and the caller must not publish.

This script runs as the runner user; the agent runs as a different OS user and cannot
modify it or its result file.

Usage: opencode_guard.py RESULT_JSON MODEL CONFIG_JSON AGENT_HOME WORKDIR TIMEOUT_MIN
Task text is read from the TASK environment variable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PREFACE = (
    'You are working in a sandboxed copy of a repository. Make the requested change by editing '
    'files in the working directory only. Do not run git commit or git push, do not try to reach '
    'the network, and do not read or write outside the working directory. Stop when the change '
    'is complete and describe what you changed. Task:\n'
)
AGENT_USER = 'ocagent'


def evaluate(lines: list[str], exit_code: int | None, timed_out: bool) -> tuple[str, str]:
    """Return ('success', '') or ('failed', reason). Pure and exhaustively testable."""
    if timed_out:
        return 'failed', 'timeout'
    if exit_code != 0:
        return 'failed', f'nonzero_exit:{exit_code}'
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return 'failed', 'unparseable_output'
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return 'failed', 'unparseable_output'
        events.append(event)
    if any(event['type'] == 'error' for event in events):
        return 'failed', 'agent_reported_error'
    finishes = [e for e in events if e['type'] == 'step_finish']
    if not finishes:
        return 'failed', 'no_completion_event'
    reason = ((finishes[-1].get('part') or {}).get('reason'))
    if reason != 'stop':
        return 'failed', 'incomplete_generation:' + re.sub(r'[^a-z_-]', '', str(reason).lower())[:30]
    return 'success', ''


def build_command(model: str, config: str, home: str, workdir: str, timeout_min: int, task: str) -> list[str]:
    env = {
        'HOME': home, 'XDG_CONFIG_HOME': f'{home}/.config', 'XDG_DATA_HOME': f'{home}/.local/share',
        'XDG_CACHE_HOME': f'{home}/.cache', 'XDG_STATE_HOME': f'{home}/.local/state',
        'PATH': '/opt/opencode/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'CI': 'true',
        'OPENCODE_CONFIG_CONTENT': config, 'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
        'OPENCODE_DISABLE_EXTERNAL_SKILLS': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1',
    }
    assignments = [f'{key}={value}' for key, value in env.items()]
    return ['sudo', '-n', '-u', AGENT_USER, 'timeout', '--kill-after=30s', f'{timeout_min}m',
            'env', '-i', *assignments,
            'opencode', 'run', '--format', 'json', '--model', model, '--dir', workdir, '--', PREFACE + task]


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        print(__doc__, file=sys.stderr)
        return 2
    result_path, model, config_path, home, workdir, timeout_min = argv[1:]
    with open(config_path, encoding='utf-8') as handle:
        config = handle.read().strip()
    command = build_command(model, config, home, workdir, int(timeout_min), os.environ.get('TASK', ''))
    lines: list[str] = []
    timed_out = False
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_min) * 60 + 90)
        lines, exit_code = completed.stdout.splitlines(), completed.returncode
        timed_out = exit_code in (124, 137)
        if completed.stderr.strip():
            print('agent stderr (first 2000 chars):', completed.stderr[:2000])
    except subprocess.TimeoutExpired:
        subprocess.run(['sudo', '-n', 'pkill', '-KILL', '-u', AGENT_USER], check=False)
        exit_code, timed_out = None, True
    status, reason = evaluate(lines, exit_code, timed_out)
    with open(result_path, 'w', encoding='utf-8') as handle:
        json.dump({'status': status, 'reason': reason, 'events': len(lines)}, handle)
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as handle:
            handle.write(f'generation={status}\ngeneration_reason={reason}\n')
    print(f'opencode guard: generation={status} reason={reason or "-"} events={len(lines)}')
    return 0 if status == 'success' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
