#!/usr/bin/env python3
"""Run the sandboxed agent and decide whether generation truly succeeded (guard v4.0, OpenCode v2).

Uses `opencode run --standalone --format json`, which only generates in the working tree: it
makes no commit, push, PR or comment. Generation counts as successful only when ALL hold:
  - the process exited 0 and did not time out
  - every stdout line parsed as a JSON event
  - no `error` event was emitted
  - the transcript is a coherent lifecycle: strictly nested steps ending in a `step_finish` with reason
    "stop", or ending in the bare reply text that v2 prints after its last tool-calls step (or alone)
  - no process of the agent account is left once OpenCode has exited
Anything else (provider error, timeout, crash, truncated or tool-call-pending output, or an
agent process that survives) is a failure, and the caller must not publish.

OpenCode v2 retries a rejected provider request (HTTP 401/403/429/5xx, a cut or empty stream)
internally and indefinitely, emitting no `error` event until it is killed, so the transcript alone
cannot fail fast. The trusted proxy therefore appends `provider_rejection` records to the file named
by OPENCODE_PROXY_LOG; the guard polls it while the agent runs and, once a rejection reaches its
threshold, stops the agent and reports a sanitized category (provider_auth_rejected,
provider_http_403, provider_http_429, provider_unavailable, ...). No response content is read.

After every run, successful or not, every process of the agent account is killed and the
guard proves none is left before it reports. A detached process started by an agent tool
could otherwise keep writing to the working tree while the next step collects it.

This script runs as the runner user; the agent runs as a different OS user and cannot
modify it or its result file.

Usage: opencode_guard.py RESULT_JSON MODEL CONFIG_JSON AGENT_HOME WORKDIR TIMEOUT_MIN
Task text is read from the TASK environment variable. GUARD_MODE=review selects the
read-only reviewer preface and GUARD_TEXT_OUT receives the final assistant text.
The agent session id is recorded so implementer and reviewer sessions can be compared.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

PREFACE = (
    'You are working in a sandboxed copy of a repository. Make the requested change by editing '
    'files in the working directory only. Do not run git commit or git push, do not try to reach '
    'the network, and do not read or write outside the working directory. Stop when the change '
    'is complete and describe what you changed. Task:\n'
)
REVIEW_PREFACE = (
    'You are an independent code reviewer in a read-only sandbox. The change under review is in '
    'REVIEW/diff.patch; inspect it, the surrounding source, the tests and the security '
    'implications yourself. Do not modify any file and do not try to reach the network. Check '
    'correctness, security, database and transaction safety, test quality and cross-module '
    'effects. Finish with ONE fenced json block of this shape and nothing after it: '
    '{"verdict": "no_blocking_findings" | "blocking_findings", "summary": "...", "findings": '
    '[{"severity": "critical|high|medium|low|info", "title": "...", "files": ["path"], '
    '"evidence": "reproduction or code evidence", "recommendation": "..."}], '
    '"verification": [{"command": "...", "result": "..."}]}. Never approve the change. Focus:\n'
)
PROBE_PREFACE = (
    'You are running a bounded, nonpublishing connectivity probe. Do not use tools, inspect files, '
    'modify a repository, review code, or publish anything. Reply exactly PROBE_OK. Task: '
)
AGENT_USER = 'ocagent'
REAP_ATTEMPTS = 10
REAP_PAUSE = 0.5
SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{6,80}$')


def extract_session(lines: list[str]) -> str:
    for line in lines:
        try:
            value = json.loads(line).get('sessionID')
        except (ValueError, AttributeError):
            continue
        if isinstance(value, str) and SESSION_RE.match(value):
            return value
    return ''


def extract_final_text(lines: list[str]) -> str:
    texts = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'text':
            text = (event.get('part') or {}).get('text')
            if isinstance(text, str):
                texts.append(text)
    return texts[-1] if texts else ''


def probe_acknowledged(lines: list[str]) -> bool:
    """The fixed probe succeeds only when the final assistant text is the expected token."""
    return extract_final_text(lines).strip() == 'PROBE_OK'


EVENT_PART_TYPE = {'step_start': 'step-start', 'step_finish': 'step-finish', 'text': 'text',
                   'reasoning': 'reasoning', 'tool_use': 'tool'}
STEP_BODY = ('text', 'reasoning', 'tool_use')
SESSION_EVENT_RE = re.compile(r'^ses_[A-Za-z0-9]{8,64}$')
MESSAGE_RE = re.compile(r'^msg_[A-Za-z0-9]{8,64}$')


def fail(code: str) -> tuple[str, str]:
    return 'failed', code


def evaluate(lines: list[str], exit_code: int | None, timed_out: bool) -> tuple[str, str]:
    """Return ('success', '') or ('failed', code). Pure and exhaustively testable.

    The event format below was captured from the real OpenCode 1.19.3 binary
    (tests/deployment/fixtures/opencode_1_19_3). Success needs a coherent lifecycle, not
    just a terminal event: one session, strictly nested steps, a supported termination,
    and no contradictory failure signal. A valid transcript proves the run ended normally;
    it says nothing about whether the change is correct.
    """
    if timed_out:
        return fail('timeout')
    if exit_code != 0:
        return fail(f'nonzero_exit:{exit_code}')
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return fail('unparseable_output')
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return fail('unparseable_output')
        events.append(event)
    if not events:
        return fail('no_events')
    session = events[0].get('sessionID')
    if not isinstance(session, str) or not SESSION_EVENT_RE.match(session):
        return fail('session_identity_missing')
    # OpenCode v2 prints the closing reply of a run as bare text events, with no step_start/step_finish of their
    # own: alone for a reply that needs no tool call, or after the last tool-calls step (measured on 2.1.4, the
    # shape is stable). Such text is accepted only there, last, and non-empty. A truncated reply is never bare
    # (v2 emits step_finish(length) and an `error` event) and an interrupted run fails the exit code, timeout and
    # error checks above and below.
    closing = False           # bare reply text has started: no step may follow
    texts = 0
    open_step = None          # (messageID, tool_uses, saw_body)
    finished = []             # (reason, tool_uses)
    last_stamp = 0
    for event in events:
        kind = event['type']
        if kind == 'error':
            return fail('agent_reported_error')
        if kind not in EVENT_PART_TYPE:
            return fail('unknown_event_type')
        if event.get('sessionID') != session:
            return fail('session_mismatch')
        stamp = event.get('timestamp')
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < last_stamp:
            return fail('timestamp_order')
        last_stamp = stamp
        part = event.get('part')
        if not isinstance(part, dict) or part.get('type') != EVENT_PART_TYPE[kind] \
                or part.get('sessionID') != session or not isinstance(part.get('messageID'), str) \
                or not MESSAGE_RE.match(part['messageID']):
            return fail('malformed_part')
        if kind == 'step_start':
            if closing:
                return fail('event_ordering')
            if open_step is not None or (finished and finished[-1][0] == 'stop'):
                return fail('event_ordering')
            open_step = [part['messageID'], 0]
        elif kind in STEP_BODY:
            if open_step is None and kind != 'tool_use' and (not finished or finished[-1][0] == 'tool-calls'):
                closing = True
                texts += kind == 'text' and isinstance(part.get('text'), str) and bool(part['text'].strip())
                continue
            if open_step is None or part['messageID'] != open_step[0]:
                return fail('event_ordering')
            if kind == 'tool_use':
                if (part.get('state') or {}).get('status') not in ('completed', 'error'):
                    return fail('tool_state_unfinished')
                open_step[1] += 1
        else:  # step_finish
            if open_step is None or part['messageID'] != open_step[0]:
                return fail('unmatched_step_finish')
            reason = part.get('reason')
            if reason == 'stop' and open_step[1]:
                return fail('stop_with_pending_tools')
            if reason == 'tool-calls' and not open_step[1]:
                return fail('tool_calls_without_tools')
            if reason not in ('stop', 'tool-calls'):
                return fail('incomplete_generation:' + re.sub(r'[^a-z_-]', '', str(reason).lower())[:30])
            finished.append((reason, open_step[1]))
            open_step = None
    if open_step is not None:
        return fail('unfinished_step')
    if closing:
        return ('success', '') if texts else fail('no_supported_termination')
    if not finished or finished[-1][0] != 'stop':
        return fail('no_supported_termination')
    return 'success', ''


def reap_agent_processes() -> str:
    """Kill every process of the agent account and prove none is left: '' when none is, else the failure reason.

    The account exists only for this job on a disposable runner, so matching by UID cannot reach another
    user's process. pgrep runs through sudo so that a restricted /proc cannot hide a survivor."""
    for _ in range(REAP_ATTEMPTS):
        subprocess.run(['sudo', '-n', 'pkill', '-KILL', '-u', AGENT_USER], check=False, capture_output=True)
        left = subprocess.run(['sudo', '-n', 'pgrep', '-u', AGENT_USER], check=False, capture_output=True, text=True)
        if left.returncode == 1:                     # pgrep: no process matched
            return ''
        if left.returncode != 0:
            return f'agent_process_check_failed:{left.returncode}'
        time.sleep(REAP_PAUSE)
    return 'agent_processes_survived'


POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 30
REJECTION_CATEGORY_RE = re.compile(r'^provider_[a-z0-9_]{1,40}$')


def read_new_rejection(path: str, offset: int) -> tuple[str, int]:
    """Return (category, new_offset): category is non-empty once the proxy logged a terminal rejection.

    The log is written by the trusted proxy as the runner user; the agent account cannot reach it. Only the
    fixed `provider_*` category token is taken from a record, never a message, header or body."""
    try:
        with open(path, 'rb') as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return '', offset
    complete = data.rfind(b'\n') + 1
    for raw in data[:complete].splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('event') != 'provider_rejection' or record.get('terminal') is not True:
            continue
        category = record.get('category')
        if isinstance(category, str) and REJECTION_CATEGORY_RE.match(category):
            return category, offset + complete
    return '', offset + complete


def kill_agent_account(_process=None) -> None:
    subprocess.run(['sudo', '-n', 'pkill', '-KILL', '-u', AGENT_USER], check=False, capture_output=True)


def run_agent(command: list[str], workdir: str, wall_timeout: int, proxy_log: str,
              stop=kill_agent_account) -> tuple[str, str, int, str]:
    """Run the agent; stop it as soon as the proxy reports a terminal provider rejection.

    Returns (stdout, stderr, exit_code, rejection_category). `stop(process)` kills the agent's own processes (the
    command runs through sudo, which the runner user cannot signal directly). Raises subprocess.TimeoutExpired
    when the wall clock limit passes first, unless a rejection was already recorded: that is reported instead."""
    try:
        offset = os.path.getsize(proxy_log) if proxy_log else 0
    except OSError:
        offset = 0
    process = subprocess.Popen(command, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + wall_timeout
    rejection = ''
    while True:
        try:
            stdout, stderr = process.communicate(timeout=POLL_SECONDS)
            if proxy_log and not rejection:           # the agent can exit (v2 does on a 401) between two polls
                rejection, offset = read_new_rejection(proxy_log, offset)
            return stdout, stderr, process.returncode, rejection
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            stop(process)
            process.kill()
            stdout, stderr = process.communicate()
            if rejection:
                return stdout, stderr, process.returncode, rejection
            raise subprocess.TimeoutExpired(command, wall_timeout)
        if proxy_log and not rejection:
            rejection, offset = read_new_rejection(proxy_log, offset)
            if rejection:
                stop(process)
                deadline = min(deadline, time.monotonic() + STOP_GRACE_SECONDS)


def build_command(model: str, config: str, home: str, workdir: str, timeout_min: int, task: str,
                  mode: str = 'implement') -> list[str]:
    if mode == 'probe':
        task = 'Reply exactly PROBE_OK.'
    env = {
        'HOME': home, 'XDG_CONFIG_HOME': f'{home}/.config', 'XDG_DATA_HOME': f'{home}/.local/share',
        'XDG_CACHE_HOME': f'{home}/.cache', 'XDG_STATE_HOME': f'{home}/.local/state',
        'PATH': '/opt/opencode/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'CI': 'true',
        'OPENCODE_CONFIG_CONTENT': config, 'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
        'OPENCODE_CONFIG_PROJECT_DISABLE': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
        'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
        'OPENCODE_DISABLE_AUTOUPDATE': '1',
        # v2 resolves relative paths against $PWD when it is set; it must be the sandbox work tree.
        'PWD': workdir,
    }
    assignments = [f'{key}={value}' for key, value in env.items()]
    timeout_arg = f'{timeout_min}s' if mode == 'probe' else f'{timeout_min}m'
    timeout_flags = ['--signal=KILL'] if mode == 'probe' else ['--kill-after=30s']
    return ['sudo', '-n', '-u', AGENT_USER, 'timeout', *timeout_flags, timeout_arg,
            'env', '-i', *assignments,
            'opencode', 'run', '--standalone', '--format', 'json', '--model', model, '--',
            (REVIEW_PREFACE if mode == 'review' else PROBE_PREFACE if mode == 'probe' else PREFACE) + task]


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        print(__doc__, file=sys.stderr)
        return 2
    result_path, model, config_path, home, workdir, timeout_min = argv[1:]
    with open(config_path, encoding='utf-8') as handle:
        config = handle.read().strip()
    mode = os.environ.get('GUARD_MODE', 'implement')
    task = os.environ.get('TASK', '')
    command = build_command(model, config, home, workdir, int(timeout_min), task, mode)
    lines: list[str] = []
    timed_out = False
    rejection = ''
    try:
        wrapper_timeout = int(timeout_min) + 5 if mode == 'probe' else int(timeout_min) * 60 + 90
        stdout, stderr, exit_code, rejection = run_agent(command, workdir, wrapper_timeout,
                                                         os.environ.get('OPENCODE_PROXY_LOG', ''))
        lines = stdout.splitlines()
        timed_out = exit_code in (124, 137)
        if mode != 'probe' and stderr.strip():
            print('agent stderr (first 2000 chars):', stderr[:2000])
    except subprocess.TimeoutExpired:
        exit_code, timed_out = None, True
    survivor = reap_agent_processes()                  # on every path, before anything is reported or collected
    status, reason = evaluate(lines, exit_code, timed_out)
    if rejection and status == 'failed':               # the proxy saw the provider refuse: name that, not the symptom
        reason = rejection
    if mode == 'probe' and status == 'success' and not probe_acknowledged(lines):
        status, reason = 'failed', 'probe_acknowledgement_missing'
    if survivor:
        status, reason = 'failed', survivor
    session = extract_session(lines) if status == 'success' else ''
    if status == 'success' and not session:
        status, reason = 'failed', 'no_session_identity'
    if status == 'success' and os.environ.get('GUARD_TEXT_OUT'):
        with open(os.environ['GUARD_TEXT_OUT'], 'w', encoding='utf-8') as handle:
            handle.write(extract_final_text(lines))
    with open(result_path, 'w', encoding='utf-8') as handle:
        json.dump({'status': status, 'reason': reason, 'events': len(lines), 'session_id': session}, handle)
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as handle:
            handle.write(f'generation={status}\ngeneration_reason={reason}\nsession_id={session}\n')
    print(f'opencode guard: generation={status} reason={reason or "-"} events={len(lines)}')
    return 0 if status == 'success' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
