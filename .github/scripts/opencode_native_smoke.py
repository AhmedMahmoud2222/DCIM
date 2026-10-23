#!/usr/bin/env python3
"""Native OpenCode CLI connectivity smoke test (smoke v1.0).

Diagnostic experiment, not production activation. It complements the direct
synthetic diagnostic (model_health_diagnostic.py): that script builds direct
HTTP requests to opencode.ai and reported HTTP 403 on GitHub runners, while
the official OpenCode client succeeded elsewhere. This script removes the two
most likely divergence sources by exercising the pinned OpenCode CLI with its
standard provider integration:

  - the real `opencode run` runtime pinned by .github/opencode/runtime,
    never a custom HTTP client;
  - only the OPENCODE_API_KEY environment variable as the credential — no
    DCIM model proxy, no catalog client, no hand-written auth headers.

Fixed scope: exactly one model (opencode/glm-5.3-flash-free) and one
hard-coded synthetic prompt (Respond with exactly OK). No argument, parameter
or environment variable can select a different model, prompt or endpoint: the
diagnostic cannot become an arbitrary channel.

Isolation: the CLI runs in an empty temporary working directory with
HOME/XDG_* reserved under a dedicated directory, disconnected from the
repository checkout (repo source is used only to install the runtime, never
loaded into model context). Every tool the pinned runtime exposes is denied
at the config level except `read`, which is set to `ask` (headless-safe) to
maintain free-tier compatibility with the live opencode.ai gateway: `read:
ask` is not auto-approved in headless mode (validated against the real pinned
CLI, exits 0 with `tool_use(read,error)` and no file-content leakage) and
does not change the request envelope to trigger a pre-auth 403. The
transcript-level guard rejects any completed read tool use, and all other
tools remain denied. No real credential is used by this diagnostic until
isolation is independently validated; the workflow uses `OPENCODE_API_KEY`
only in the secret-bearing step after the fail-closed guard.

Recording: sanitized evidence only — CLI version, model, ok/failed status,
reason class, provider HTTP status class, elapsed time and event counts.
Never the API key, prompt, URLs, response bodies, headers or stderr text.

Usage: opencode_native_smoke.py RESULT_JSON BINARY AGENT_HOME WORKDIR TIMEOUT_MIN
All three paths are canonicalized to absolute before any subprocess runs (the
inference run changes cwd to the workdir; a relative binary path with a slash
would otherwise resolve from the wrong directory). The binary must be the
checked-out, lockfile-pinned runtime; it is not user-configurable.

Environment: OPENCODE_API_KEY (read once, never printed, never written).

Exit code: 0 only when the CLI version matches the pin, exited 0, produced a
coherent pure-text life cycle ending in step_finish(reason="stop"), and the
run was marked ok. Any other outcome writes a sanitized record and exits 1.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

SMOKE_VERSION = '1.0'

# One fixed model, in the CLI selector (provider/model) form. The selector is
# consumed by the CLI; the bare provider ID is never sent anywhere by us.
MODEL = 'opencode/glm-5.3-flash-free'

# The exact fixed synthetic prompt from the diagnostic mandate.
PROMPT = 'Respond with exactly OK'

# Pinned runtime version; must match .github/opencode/runtime/package.json.
EXPECTED_OPENCODE_VERSION = '1.19.3'

# Reason codes form a fixed safe vocabulary: f-string suffixes are escaped to
# that vocabulary so raw provider text can never enter the artifact.
RE_REASON_ESCAPE = re.compile(r'[^a-z0-9_:-]')

# Only this provider may be enabled for the smoke run.
ENABLED_PROVIDERS = ('opencode',)

# The fixed prompt needs no tools, so every tool the pinned runtime exposes is
# denied before the transcript can ever show one. All seven keys below are
# recognized permission kinds in opencode-ai 1.19.3 (validated against the
# pinned binary's embedded code: read/edit/bash/webfetch plus the delegation
# kinds external_directory, task and todowrite).
#
# IMPORTANT (diagnostic compatibility, 2026-10-23): on OpenCode 1.19.3 the
# free-tier gateway treats `read: 'deny'` as changing the outbound tool envelope
# and returns HTTP 403 (policy rejection) before auth; the run fails with
# `provider_error`/`forbidden` even though the network/key path is valid.
# Headless validation with the real pinned binary + scripted stub (no TTY)
# shows `read: 'ask'` does NOT auto-approve filesystem reads and does NOT hang:
# it produces `tool_use(read,error)` and stops (`step_finish(reason=tool-calls)`)
# without leaking file contents, and exits 0. On the live gateway with a
# synthetic key, `read: 'ask'` yields HTTP 401 (auth envelope reached), same as
# `read: 'allow'`. The runner's transcript-level `unexpected_tool_use` check
# and `evaluate()` still reject any completed read; `read` remains not
# 'allow'. This is a diagnostic-only change: it reconciles the native-CLI
# smoke with free-tier compatibility without granting uncontrolled filesystem
# access or expanding the credential surface.
PERMISSIONS = {
    'edit': 'deny',
    'read': 'ask',
    'bash': 'deny',
    'webfetch': 'deny',
    'external_directory': 'deny',
    'task': 'deny',
    'todowrite': 'deny',
}

# Small, allowlisted child env. Nothing else is inherited: no SHELL, no user
# configuration directories, no GitHub workflow environment, no CI tokens.
CHILD_ENV_KEYS = frozenset({
    'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
    'XDG_STATE_HOME', 'PATH', 'LANG', 'CI', 'OPENCODE_API_KEY',
    'OPENCODE_CONFIG_CONTENT', 'OPENCODE_DISABLE_AUTOUPDATE',
    'OPENCODE_DISABLE_MODELS_FETCH', 'OPENCODE_PURE',
})

RESULT_KEYS = frozenset({
    'smoke_version', 'opencode_expected_version', 'opencode_version',
    'model', 'status', 'reason', 'error_class', 'error_status_code',
    'exit_code', 'timed_out', 'event_count', 'stderr_lines', 'elapsed_ms',
})


class VersionProbeError(Exception):
    """Raised when the pinned CLI version cannot be verified."""


def smoke_config() -> dict:
    """Fixed config content: one provider, both models pinned, every tool denied.

    `model` and `small_model` are both pinned to the single MODEL (validated
    config key in opencode-ai 1.19.3: Provider.getSmallModel reads
    config.small_model in providerID/modelID form), so no auxiliary or
    title-generation request can pick a different model. No apiKey, baseURL or
    provider options are ever set: the key arrives only through the
    OPENCODE_API_KEY environment variable — the standard provider integration.
    """
    return {
        'autoupdate': False,
        'share': 'disabled',
        'enabled_providers': list(ENABLED_PROVIDERS),
        'model': MODEL,
        'small_model': MODEL,
        'permission': dict(PERMISSIONS),
    }


def build_child_env(agent_home: str, key: str) -> dict:
    """Return the exact child environment; key='' omits the API key entirely."""
    if not agent_home:
        raise ValueError('agent_home is required')
    env = {
        'HOME': agent_home,
        'XDG_CONFIG_HOME': f'{agent_home}/.config',
        'XDG_DATA_HOME': f'{agent_home}/.local/share',
        'XDG_CACHE_HOME': f'{agent_home}/.cache',
        'XDG_STATE_HOME': f'{agent_home}/.local/state',
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'CI': 'true',
        'OPENCODE_CONFIG_CONTENT': json.dumps(smoke_config()),
        'OPENCODE_DISABLE_AUTOUPDATE': '1',
        'OPENCODE_DISABLE_MODELS_FETCH': '1',
        'OPENCODE_PURE': '1',
    }
    if key:
        env['OPENCODE_API_KEY'] = key
    return env


def build_command(binary: str, workdir: str) -> list[str]:
    """Exact fixed CLI invocation. No model or prompt parameter exists."""
    return [
        binary, 'run', '--pure', '--format', 'json',
        '--model', MODEL, '--dir', workdir, '--', PROMPT,
    ]


SEMVER_LINE = re.compile(r'^(?:opencode\s+)?v?(\d+\.\d+\.\d+)$')


def parse_version(text: str) -> str | None:
    """Return the bare version from `opencode --version` output, or None.

    Real pinned-runtime evidence: opencode 1.19.3 prints bare `1.19.3`
    (captured from the registry-published windows-x64 npm tarball; the
    linux-x64 build matches, proven by the workflow install step's
    bare-equality assertion passing on CI). The matcher stays bounded and
    tolerant at the edges: optional single `opencode ` banner, optional one
    `v`, full-line handling, and any second distinct version or unmatched
    line means "cannot determine version" — fail closed.
    """
    versions: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = SEMVER_LINE.match(line)
        if not match:
            return None
        versions.add(match.group(1))
    if len(versions) != 1:
        return None
    return next(iter(versions))


def read_version(binary: str, env: dict) -> str:
    """Run `opencode --version` and return the bare version string."""
    completed = subprocess.run(
        [binary, '--version'], capture_output=True, text=True, timeout=60, env=env,
    )
    if completed.returncode != 0:
        raise VersionProbeError('version probe exited nonzero')
    version = parse_version(completed.stdout)
    if not version:
        raise VersionProbeError('version output unparsable')
    return version


def classify_provider_error(status_code: int | None) -> str:
    """Map a provider HTTP status to a sanitized error class.

    Same vocabulary the direct synthetic diagnostic uses so both artifacts
    can be compared side by side.
    """
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return 'unknown_provider_error'
    if status_code == 401:
        return 'auth_error'
    if status_code == 403:
        return 'forbidden'
    if status_code == 429:
        return 'rate_limit'
    if status_code == 404:
        return 'model_not_available'
    if 500 <= status_code < 600:
        return 'upstream_error'
    return f'http_{status_code}'


def _extract_error(event: dict) -> tuple[str, int | None]:
    """Pull only the sanitized HTTP status data out of an error event."""
    error = event.get('error')
    data = error.get('data') if isinstance(error, dict) else None
    if not isinstance(data, dict):
        return 'unknown_provider_error', None
    code = data.get('statusCode')
    if isinstance(code, bool) or not isinstance(code, int):
        return 'unknown_provider_error', None
    return classify_provider_error(code), code


def _esc_reason(value: object) -> str:
    """Fold free text into the safe reason vocabulary: collapse whitespace to
    '-', drop everything outside the vocabulary first, and cap the length."""
    return RE_REASON_ESCAPE.sub('', re.sub(r'\s+', '-', str(value).lower()))[:30]


def evaluate(lines: list[str], exit_code: int | None, timed_out: bool
             ) -> tuple[str, str, str | None, int | None]:
    """Classify one smoke transcript. Pure function.

    Returns (status, reason, error_class, error_status_code); status 'ok'
    only when the CLI exited 0 and the life cycle ended reason 'stop'.
    Precedence: timeout > provider_error > tool_use > lifecycle > exit code,
    so a clean HTTP status class survives even when the CLI exits nonzero.
    """
    if timed_out:
        return 'failed', 'timeout', None, None
    error_class: str | None = None
    error_status_code: int | None = None
    last_finish_reason: str | None = None
    events = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return 'failed', 'unparseable_output', error_class, error_status_code
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return 'failed', 'unparseable_output', error_class, error_status_code
        events += 1
        kind = event['type']
        if kind == 'error':
            if error_class is None:
                error_class, error_status_code = _extract_error(event)
            return 'failed', 'provider_error', error_class, error_status_code
        if kind == 'tool_use':
            return 'failed', 'unexpected_tool_use', error_class, error_status_code
        if kind not in ('step_start', 'step_finish', 'text', 'reasoning'):
            return 'failed', 'unknown_event_type', error_class, error_status_code
        if kind == 'step_finish':
            part = event.get('part')
            reason = part.get('reason') if isinstance(part, dict) else None
            if not isinstance(reason, str):
                return 'failed', 'malformed_event', error_class, error_status_code
            last_finish_reason = reason
    if events == 0:
        return 'failed', 'no_events', error_class, error_status_code
    if last_finish_reason is None:
        return 'failed', 'no_supported_termination', error_class, error_status_code
    if last_finish_reason != 'stop':
        return ('failed', f'incomplete_generation:{_esc_reason(last_finish_reason)}',
                error_class, error_status_code)
    if exit_code != 0:
        return 'failed', f'nonzero_exit:{exit_code}', error_class, error_status_code
    return 'ok', '', error_class, error_status_code


def _write_result(path: str, *, opencode_version: str | None, status: str, reason: str,
                  error_class: str | None, error_status_code: int | None,
                  exit_code: int | None, timed_out: bool, event_count: int,
                  stderr_lines: int, elapsed_ms: int | None) -> dict:
    """Write the sanitized record with a closed field list: nothing else enters.

    Returns the record so tests can assert on the exact persisted artifact.
    The record contains the fixed model and configured version, error/status
    classes, counts and elapsed time — never the key, prompt, URLs, response
    bodies, headers or stderr text.
    """
    record = {
        'smoke_version': SMOKE_VERSION,
        'opencode_expected_version': EXPECTED_OPENCODE_VERSION,
        'opencode_version': opencode_version,
        'model': MODEL,
        'status': status,
        'reason': reason,
        'error_class': error_class,
        'error_status_code': error_status_code,
        'exit_code': exit_code,
        'timed_out': timed_out,
        'event_count': event_count,
        'stderr_lines': stderr_lines,
        'elapsed_ms': elapsed_ms,
    }
    assert set(record) == RESULT_KEYS
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(record, handle, indent=2)
    return record


def _summary(record: dict) -> str:
    """One sanitized status line for the run log (also artifact-consistent)."""
    return (' '.join([
        f'native smoke: model={record["model"]}',
        'status ok' if record['status'] == 'ok' else 'status failed',
        f"reason={record['reason'] or '-'}",
        f"error_class={record['error_class'] or '-'}",
        f"error_status_code={record['error_status_code'] if record['error_status_code'] is not None else '-'}",
        f"elapsed_ms={record['elapsed_ms'] if record['elapsed_ms'] is not None else '-'}",
    ]))


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print('usage: opencode_native_smoke.py RESULT_JSON BINARY AGENT_HOME WORKDIR TIMEOUT_MIN',
              file=sys.stderr)
        return 2
    result_path, binary, agent_home, workdir, timeout_min = argv[1], argv[2], argv[3], argv[4], int(argv[5])

    # Canonicalize before any subprocess runs. The inference run later changes
    # cwd to the smoke workdir; a relative binary containing '/' would then be
    # resolved from that new cwd and raise ENOENT on POSIX. Pinned to the
    # checked-out runtime by the caller — never user-configurable at runtime.
    binary = os.path.abspath(binary)
    agent_home = os.path.abspath(agent_home)
    workdir = os.path.abspath(workdir)
    key = os.environ.get('OPENCODE_API_KEY', '')

    start = time.monotonic()

    if not key:
        _write_result(result_path, opencode_version=None, status='failed',
                      reason='missing_api_key', error_class=None,
                      error_status_code=None, exit_code=None, timed_out=False,
                      event_count=0, stderr_lines=0, elapsed_ms=None)
        print('native smoke: status failed reason=missing_api_key')
        return 1

    # No credential below this line until the inference env is built: the
    # version probe runs with an env that does not contain the key at all.
    home_env = build_child_env(agent_home, '')
    for directory in (agent_home, workdir,
                      home_env['XDG_CONFIG_HOME'], home_env['XDG_DATA_HOME'],
                      home_env['XDG_CACHE_HOME'], home_env['XDG_STATE_HOME']):
        os.makedirs(directory, exist_ok=True)

    try:
        version = read_version(binary, home_env)
    except (VersionProbeError, OSError, ValueError) as error:
        reason = _esc_reason(error) or 'opencode_version_unverified'
        _write_result(result_path, opencode_version=None, status='failed',
                      reason=reason, error_class=None, error_status_code=None,
                      exit_code=None, timed_out=False, event_count=0,
                      stderr_lines=0, elapsed_ms=None)
        print(f'native smoke: status failed reason={reason}')
        return 1

    if version != EXPECTED_OPENCODE_VERSION:
        _write_result(result_path, opencode_version=version, status='failed',
                      reason='opencode_version_mismatch', error_class=None,
                      error_status_code=None, exit_code=None, timed_out=False,
                      event_count=0, stderr_lines=0, elapsed_ms=None)
        print('native smoke: status failed reason=opencode_version_mismatch')
        return 1

    env = build_child_env(agent_home, key)
    command = build_command(binary, workdir)
    timed_out = False
    lines: list[str] = []
    stderr_lines = 0
    exit_code: int | None = None
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   env=env, cwd=workdir,
                                   timeout=timeout_min * 60 + 90)
        lines = completed.stdout.splitlines()
        exit_code = completed.returncode
        if completed.returncode in (124, 137):
            timed_out = True
        stderr_lines = len(completed.stderr.splitlines())
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _write_result(result_path, opencode_version=version, status='failed',
                      reason='cli_launch_error', error_class=None,
                      error_status_code=None, exit_code=None, timed_out=False,
                      event_count=0, stderr_lines=0, elapsed_ms=elapsed_ms)
        print('native smoke: status failed reason=cli_launch_error')
        return 1
    elapsed_ms = int((time.monotonic() - start) * 1000)

    status, reason, error_class, error_status_code = evaluate(
        lines, exit_code, timed_out)
    record = _write_result(result_path, opencode_version=version,
                           status=status, reason=reason, error_class=error_class,
                           error_status_code=error_status_code,
                           exit_code=exit_code, timed_out=timed_out,
                           event_count=len(lines), stderr_lines=stderr_lines,
                           elapsed_ms=elapsed_ms)
    print(_summary(record))
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
