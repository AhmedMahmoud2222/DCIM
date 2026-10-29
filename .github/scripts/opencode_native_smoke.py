#!/usr/bin/env python3
"""Native OpenCode CLI connectivity smoke test (smoke v2.0, OpenCode v2 runtime).

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

Isolation: the CLI runs in an empty temporary working directory (process cwd
and $PWD; v2 has no --dir flag) with HOME/XDG_* reserved under a dedicated
directory, disconnected from the repository checkout (repo source is used
only to install the runtime, never loaded into model context). Project,
Claude Code and skill configuration discovery are disabled by environment
flags (v2 has no --pure). Permissions are fail closed in both config
dialects: the legacy `permission` map and the v2-native `permissions` rule
list, which opens with a catch-all deny because a headless v2 run
auto-approves any action that no rule matches. Only `read` is left as `ask`
(see PERMISSIONS for why, and for what is and is not verified about it);
`ask` cannot be answered headlessly, so v2 rejects every read call. The
transcript-level guard rejects any tool-use event (completed or error). The
workflow maps `OPENCODE_API_KEY` only in the secret-bearing step, after a
credential-free compatibility preflight has passed the same envelope with a
synthetic key.

Recording: sanitized evidence only — CLI version, model, ok/failed status,
reason class, provider HTTP status class, elapsed time and event counts.
Never the API key, prompt, URLs, response bodies, headers or stderr text.

Usage: opencode_native_smoke.py [--preflight] RESULT_JSON BINARY AGENT_HOME WORKDIR TIMEOUT_MIN
All three paths are canonicalized to absolute before any subprocess runs (the
inference run changes cwd to the workdir; a relative binary path with a slash
would otherwise resolve from the wrong directory). The binary must be the
checked-out, lockfile-pinned runtime; it is not user-configurable.

Bounding: v2 retries HTTP 429/5xx and empty or cut streams internally and
indefinitely, emitting nothing but step_start events. The hard timeout is
therefore the only bound; on expiry the whole CLI process group is killed
and reaped.

Preflight mode (--preflight): credential-free synthetic compatibility probe.
It sends the exact same fixed request envelope with a non-credential synthetic
key and passes only when the gateway reaches authentication (HTTP 401) instead
of rejecting the envelope at the policy layer (HTTP 403). It never reads
OPENCODE_API_KEY. This is the regression guard for the free-tier envelope
incompatibility that made the merged diagnostic a false negative, and it must
pass before the workflow maps the real secret.

Environment: OPENCODE_API_KEY (live mode only; read once, never printed, never
written; ignored entirely in preflight mode).

Exit code: 0 only when the CLI version matches the pin and the run met its
mode's objective (live: a coherent pure-text reply — v2 prints a reply
without tool calls as bare `text` events — or a step life cycle ending in
step_finish(reason="stop"); preflight: auth reached with 401). Any other
outcome writes a sanitized record and exits 1.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import stat
import subprocess
import sys
import time

SMOKE_VERSION = '2.0'

# One fixed model, in the CLI selector (provider/model) form. The selector is
# consumed by the CLI; the bare provider ID is never sent anywhere by us.
MODEL = 'opencode/glm-5.3-flash-free'

# The exact fixed synthetic prompt from the diagnostic mandate.
PROMPT = 'Respond with exactly OK'

# Preflight mode uses this fixed non-credential placeholder. It is not a secret,
# cannot authenticate, and proves only that the request envelope is accepted
# (auth layer reached) rather than policy-rejected before auth.
SYNTHETIC_KEY = 'synthetic-compatibility-probe-not-a-credential'

# Pinned runtime version; must match .github/opencode/runtime/package.json.
EXPECTED_OPENCODE_VERSION = '2.1.4'

# Reason codes form a fixed safe vocabulary: f-string suffixes are escaped to
# that vocabulary so raw provider text can never enter the artifact.
RE_REASON_ESCAPE = re.compile(r'[^a-z0-9_:-]')

# Only this provider may be enabled for the smoke run.
ENABLED_PROVIDERS = ('opencode',)

# The fixed prompt needs no tools, so every tool is denied before the
# transcript can ever show one — except `read`, which is `ask`.
#
# OpenCode v2 accepts two permission dialects and both are set, fail closed:
#
#   - PERMISSIONS: the legacy v1 `permission` map (still accepted, deprecated).
#     On its own it is NOT sufficient in v2: measured on the real 2.1.4 binary,
#     a config carrying only this map still offers the model `execute`, `glob`,
#     `grep`, `question`, `skill` and `websearch`, because v2 auto-approves any
#     action no rule matches and the v1 keys do not cover v2's new actions.
#   - NATIVE_PERMISSIONS: the v2 `permissions` rule list (last matching rule
#     wins). It opens with a catch-all deny so any action added by a future
#     release is denied until reviewed, re-opens only `read` as `ask`, and
#     re-denies every other known action afterwards, `external_directory` last.
#     Measured with this config, the only tool offered to the model is `read`.
#
# Why `read` is `ask` and not `deny` (2026-10-23 diagnostic, v1 evidence; see
# docs/diagnostics/opencode-free-tier-compatibility.md): on opencode-ai 1.19.3
# the free-tier gateway rejected a request whose tool list lacked `read` with a
# pre-auth HTTP 403. v2 behaves the same way on the request side — `read: deny`
# removes `read` from the offered tool list (measured), `ask` keeps it — but
# whether the v2-era gateway still rejects a tool list without `read` is
# UNVERIFIED: it cannot be measured without contacting the real provider. The
# conservative choice is therefore to keep the v1 envelope shape (`read`
# offered) and rely on the credential-free preflight to detect a 403.
#
# Headless `ask` in v2 (measured on the real 2.1.4 linux-x64 binary with a
# loopback stub, stdin /dev/null): the read call is NOT auto-approved. It ends
# as tool_use(read, status=error, "This non-interactive run cannot ask the user
# for permission, so the request was rejected"), no file content reaches the
# provider and the CLI exits 0. Paths outside the workdir (including
# /proc/self/environ) are refused by `external_directory` even under
# `read: allow`. evaluate() rejects every tool_use event regardless.
PERMISSIONS = {
    'read': 'ask',
    'edit': 'deny',
    'bash': 'deny',
    'glob': 'deny',
    'grep': 'deny',
    'list': 'deny',
    'webfetch': 'deny',
    'websearch': 'deny',
    'external_directory': 'deny',
    'task': 'deny',
    'todowrite': 'deny',
    'doom_loop': 'deny',
}

# Every v2 action other than `read`, re-denied after the `read` rule. The
# catch-all deny already covers them; listing them makes the intent explicit
# and keeps each one denied even if a broader rule is ever added above.
NATIVE_DENIED_ACTIONS = (
    'edit', 'shell', 'glob', 'grep', 'execute', 'subagent', 'skill',
    'question', 'webfetch', 'websearch', 'external_directory',
)


def _rule(action: str, resource: str, effect: str) -> dict:
    return {'action': action, 'resource': resource, 'effect': effect}


NATIVE_PERMISSIONS = (
    [_rule('*', '*', 'deny'), _rule('read', '*', 'ask')]
    + [_rule(action, '*', 'deny') for action in NATIVE_DENIED_ACTIONS]
)

# Small, allowlisted child env. Nothing else is inherited: no SHELL, no user
# configuration directories, no GitHub workflow environment, no CI tokens.
# PWD must be the workdir: v2 resolves relative paths from $PWD when it is
# set, so a stale value inherited from a parent would point elsewhere. The
# OPENCODE_DISABLE_* / OPENCODE_CONFIG_PROJECT_DISABLE flags replace v1's
# removed OPENCODE_PURE / --pure.
CHILD_ENV_KEYS = frozenset({
    'HOME', 'PWD', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
    'XDG_STATE_HOME', 'PATH', 'LANG', 'CI', 'OPENCODE_API_KEY',
    'OPENCODE_CONFIG_CONTENT', 'OPENCODE_DISABLE_AUTOUPDATE',
    'OPENCODE_DISABLE_MODELS_FETCH', 'OPENCODE_DISABLE_PROJECT_CONFIG',
    'OPENCODE_CONFIG_PROJECT_DISABLE', 'OPENCODE_DISABLE_CLAUDE_CODE',
    'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS', 'npm_config_offline',
})

RESULT_KEYS = frozenset({
    'smoke_version', 'mode', 'opencode_expected_version', 'opencode_version',
    'provider', 'model', 'effective_provider', 'effective_model',
    'status', 'reason', 'error_class', 'error_status_code',
    'exit_code', 'timed_out', 'event_count', 'stderr_lines',
    'setup_ms', 'auth_ms', 'inference_ms', 'total_ms', 'execution_count',
    'output_tokens',
})

RUNTIME_DIR = '.config/opencode'


class VersionProbeError(Exception):
    """Raised when the pinned CLI version cannot be verified."""


def smoke_config() -> dict:
    """Fixed config content: one provider, both models pinned, every tool denied.

    `model` and `small_model` are both pinned to the single MODEL (the legacy
    keys are still honoured by v2; small_model drives the session-title side
    request), so no auxiliary or title-generation request can pick a different
    model. No apiKey, baseURL or provider options are ever set: the key arrives
    only through the OPENCODE_API_KEY environment variable — the standard
    provider integration.
    """
    return {
        'autoupdate': False,
        'share': 'disabled',
        'enabled_providers': list(ENABLED_PROVIDERS),
        'model': MODEL,
        'small_model': MODEL,
        'permission': dict(PERMISSIONS),
        'permissions': [dict(rule) for rule in NATIVE_PERMISSIONS],
    }


def build_child_env(agent_home: str, workdir: str, key: str) -> dict:
    """Return the exact child environment; key='' omits the API key entirely."""
    if not agent_home:
        raise ValueError('agent_home is required')
    if not workdir:
        raise ValueError('workdir is required')
    env = {
        'HOME': agent_home,
        'PWD': workdir,
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
        'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
        'OPENCODE_CONFIG_PROJECT_DISABLE': '1',
        'OPENCODE_DISABLE_CLAUDE_CODE': '1',
        'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1',
        'npm_config_offline': 'true',
    }
    if key:
        env['OPENCODE_API_KEY'] = key
    return env


def build_command(binary: str) -> list[str]:
    """Exact fixed CLI invocation. No model or prompt parameter exists.

    v2 has no --dir: the working directory is the process cwd (plus $PWD in
    the child env). --standalone keeps the run off the shared background
    service, so nothing outlives the process group the runner reaps.
    """
    return [
        binary, 'run', '--standalone', '--format', 'json',
        '--model', MODEL, '--', PROMPT,
    ]


RUNTIME_ROOT_NAMES = ('config', 'account-opencode')


def _runtime_roots(agent_home: str) -> dict[str, str]:
    """Config roots the pinned CLI reads and writes at startup.

    XDG_CONFIG_HOME is honoured, so the agent-owned copy is used. The CLI also
    loads and writes `~/.opencode` from the account's passwd home, which HOME
    does not redirect (observed on the pinned linux-x64 binary), so that real
    path is sealed as well.
    """
    if os.name != 'posix':
        raise ValueError('runtime_seal_unsupported')
    import pwd
    # The override exists only so the test suite never seals a developer's real
    # home; the workflow does not set it.
    account_home = (os.environ.get('OPENCODE_SMOKE_ACCOUNT_HOME')
                    or pwd.getpwuid(os.getuid()).pw_dir)
    return {
        'config': os.path.join(agent_home, RUNTIME_DIR),
        'account-opencode': os.path.join(account_home, '.opencode'),
    }


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_tree(root: str):
    """Yield every directory and file under root; refuse symlinks."""
    yield root
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise ValueError('runtime_config_symlink')
            yield path


def _seal_root(name: str, root: str) -> None:
    """Make an existing or new config root read-only for the run identity.

    The agent copy must start empty. The account copy may hold only what the
    pinned runtime itself installs (its own `bin/` and `.gitignore`).
    """
    os.makedirs(root, exist_ok=True)
    if os.path.islink(root):
        raise ValueError('runtime_config_symlink')
    entries = set(os.listdir(root))
    allowed = {'bin', '.gitignore'} if name == 'account-opencode' else set()
    if not entries <= allowed:
        raise ValueError('runtime_config_not_empty')
    for path in _walk_tree(root):
        mode = os.stat(path, follow_symlinks=False).st_mode
        os.chmod(path, 0o555 if stat.S_ISDIR(mode) else 0o444)


def _verify_sealed(name: str, root: str) -> None:
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError('runtime_config_missing')
    entries = set(os.listdir(root))
    allowed = {'bin', '.gitignore'} if name == 'account-opencode' else set()
    if not entries <= allowed:
        raise ValueError('runtime_config_not_empty')
    for path in _walk_tree(root):
        if os.stat(path, follow_symlinks=False).st_mode & 0o222:
            raise ValueError('runtime_not_sealed')


def prepare_runtime(agent_home: str, manifest_path: str, binary: str) -> None:
    """Seal the runtime config roots and pin the installed CLI binary."""
    if os.path.islink(binary) or not os.path.isfile(binary):
        raise ValueError('runtime_binary_invalid')
    roots = _runtime_roots(agent_home)
    for name, root in roots.items():
        _seal_root(name, root)
    manifest = {
        'version': 1,
        'opencode_version': EXPECTED_OPENCODE_VERSION,
        'binary_sha256': _file_sha256(binary),
        'roots': sorted(roots),
    }
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, sort_keys=True, separators=(',', ':'))


def validate_runtime(agent_home: str, manifest_path: str, binary: str) -> None:
    """Re-verify the pinned binary and the sealed config roots before key use."""
    with open(manifest_path, encoding='utf-8') as f:
        manifest = json.load(f)
    roots = _runtime_roots(agent_home)
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or manifest.get('opencode_version') != EXPECTED_OPENCODE_VERSION
            or manifest.get('roots') != sorted(roots)
            or not isinstance(manifest.get('binary_sha256'), str)):
        raise ValueError('runtime_manifest_invalid')
    if os.path.islink(binary) or _file_sha256(binary) != manifest['binary_sha256']:
        raise ValueError('runtime_integrity_mismatch')
    for name, root in roots.items():
        _verify_sealed(name, root)


SEMVER_LINE = re.compile(r'^(?:opencode\s+)?v?(\d+\.\d+\.\d+)$')


def parse_version(text: str) -> str | None:
    """Return the bare version from `opencode --version` output, or None.

    Real pinned-runtime evidence: the @opencode/cli 2.1.4 linux-x64 binary
    prints `opencode v2.1.4` (v1 printed a bare `1.19.3`). The matcher stays
    bounded and tolerant at the edges: optional single `opencode ` banner,
    optional one `v`, full-line handling, and any second distinct version or
    unmatched line means "cannot determine version" — fail closed.
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
    """Pull only the sanitized HTTP status data out of an error event.

    v2 shape (measured on 2.1.4): {"type":"error","error":{"type":
    "provider.auth","message":...,"status":401,"response":{"body":...}}}.
    Note v2 labels both 401 and 403 `provider.auth`, so only the numeric
    `status` distinguishes auth reached (401) from policy rejection (403).
    Errors without a status (e.g. `output-limit` after a truncated reply) are
    unknown_provider_error. Message and body are never read.
    """
    error = event.get('error')
    code = error.get('status') if isinstance(error, dict) else None
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
    only when the CLI exited 0 and the transcript is a complete reply without
    tool use. v2 has two such shapes (both accepted by opencode_guard.py):
    a reply that makes no tool call is printed as bare `text` events with no
    step events at all, and a stepped life cycle must end in
    step_finish(reason='stop'). Precedence: timeout > provider_error >
    tool_use > lifecycle > exit code, so a clean HTTP status class survives
    even when the CLI exits nonzero.
    """
    if timed_out:
        return 'failed', 'timeout', None, None
    error_class: str | None = None
    error_status_code: int | None = None
    last_finish_reason: str | None = None
    saw_step = False
    nonempty_text = False
    sessions: set = set()
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
        sessions.add(event.get('sessionID'))
        part = event.get('part')
        if kind in ('step_start', 'step_finish'):
            saw_step = True
        if kind == 'step_finish':
            reason = part.get('reason') if isinstance(part, dict) else None
            if not isinstance(reason, str):
                return 'failed', 'malformed_event', error_class, error_status_code
            last_finish_reason = reason
        if kind == 'text':
            text = part.get('text') if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                nonempty_text = True
    if events == 0:
        return 'failed', 'no_events', error_class, error_status_code
    if len(sessions) != 1 or not isinstance(next(iter(sessions)), str):
        return 'failed', 'session_mismatch', error_class, error_status_code
    if not saw_step:
        # v2 bare-text reply: only text/reasoning events, at least one with
        # non-empty text, and a clean exit (checked below).
        if not nonempty_text:
            return 'failed', 'no_supported_termination', error_class, error_status_code
        if exit_code != 0:
            return 'failed', f'nonzero_exit:{exit_code}', error_class, error_status_code
        return _exact_ok(lines, error_class, error_status_code)
    if last_finish_reason is None:
        return 'failed', 'no_supported_termination', error_class, error_status_code
    if last_finish_reason != 'stop':
        return ('failed', f'incomplete_generation:{_esc_reason(last_finish_reason)}',
                error_class, error_status_code)
    if exit_code != 0:
        return 'failed', f'nonzero_exit:{exit_code}', error_class, error_status_code
    return _exact_ok(lines, error_class, error_status_code)


def _exact_ok(lines: list[str], error_class: str | None,
              error_status_code: int | None
              ) -> tuple[str, str, str | None, int | None]:
    """A clean run counts only if it answered exactly one `OK` text part."""
    text_parts = []
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get('type') == 'text':
            part = event.get('part')
            value = part.get('text') if isinstance(part, dict) else None
            if not isinstance(value, str):
                return 'failed', 'output_mismatch', error_class, error_status_code
            text_parts.append(value)
    if len(text_parts) != 1 or text_parts[0].strip() != 'OK':
        return 'failed', 'output_mismatch', error_class, error_status_code
    return 'ok', '', error_class, error_status_code


def resolve_key(mode: str, environ: dict) -> str:
    """Return the credential for this mode.

    Preflight never reads OPENCODE_API_KEY: it always uses the fixed synthetic
    placeholder, so a real secret cannot be exposed by the credential-free
    compatibility step even if the workflow environment leaked it.
    """
    if mode == 'preflight':
        return SYNTHETIC_KEY
    return environ.get('OPENCODE_API_KEY', '')


def probe_verdict(status: str, reason: str, error_class: str | None,
                  error_status_code: int | None) -> tuple[str, str]:
    """Map one synthetic-key run to the preflight objective.

    Compatible: the gateway answered 401 — the request envelope (config, tools,
    fixed model/prompt) reached authentication and only the fake credential was
    rejected. Anything else fails closed: 403 means the envelope was rejected
    at the policy layer (the exact free-tier regression this guards against),
    and any other outcome is unexpected for a synthetic key.
    """
    if (status == 'failed' and reason == 'provider_error'
            and error_class == 'auth_error' and error_status_code == 401):
        return 'ok', 'credential_rejected_as_expected'
    if status == 'failed' and error_class == 'forbidden':
        return 'failed', 'config_envelope_rejected'
    return 'failed', 'unexpected_probe_outcome'


def _write_result(path: str, *, mode: str = 'live', opencode_version: str | None,
                  status: str, reason: str,
                  error_class: str | None, error_status_code: int | None,
                  exit_code: int | None, timed_out: bool, event_count: int,
                  stderr_lines: int, setup_ms: int | None,
                  auth_ms: int | None, inference_ms: int | None,
                  total_ms: int | None, execution_count: int,
                  output_tokens: int | None) -> dict:
    """Write the sanitized record with a closed field list: nothing else enters.

    Returns the record so tests can assert on the exact persisted artifact.
    The record contains the fixed model and configured version, error/status
    classes, counts and elapsed time — never the key, prompt, URLs, response
    bodies, headers or stderr text.
    """
    record = {
        'smoke_version': SMOKE_VERSION,
        'mode': mode,
        'opencode_expected_version': EXPECTED_OPENCODE_VERSION,
        'opencode_version': opencode_version,
        'provider': ENABLED_PROVIDERS[0],
        'model': MODEL,
        # This pair is enforced by the fixed CLI selector and the one-provider
        # allowlist; no fallback provider or model is enabled.
        'effective_provider': ENABLED_PROVIDERS[0],
        'effective_model': MODEL,
        'status': status,
        'reason': reason,
        'error_class': error_class,
        'error_status_code': error_status_code,
        'exit_code': exit_code,
        'timed_out': timed_out,
        'event_count': event_count,
        'stderr_lines': stderr_lines,
        'setup_ms': setup_ms,
        'auth_ms': auth_ms,
        'inference_ms': inference_ms,
        'total_ms': total_ms,
        'execution_count': execution_count,
        'output_tokens': output_tokens,
    }
    if set(record) != RESULT_KEYS:
        raise ValueError('result_schema_mismatch')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(record, handle, indent=2)
    return record


def _summary(record: dict) -> str:
    """One sanitized status line for the run log (also artifact-consistent)."""
    return (' '.join([
        f"native smoke: mode={record.get('mode', 'live')} model={record['model']}",
        'status ok' if record['status'] == 'ok' else 'status failed',
        f"reason={record['reason'] or '-'}",
        f"error_class={record['error_class'] or '-'}",
        f"error_status_code={record['error_status_code'] if record['error_status_code'] is not None else '-'}",
        f"total_ms={record['total_ms'] if record['total_ms'] is not None else '-'}",
    ]))


def _phase_timings(lines: list[str], started_wall_ms: int,
                   total_ms: int, mode: str) -> tuple[int | None, int | None,
                                                        int | None]:
    first_start = None
    finish = None
    error = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        timestamp = event.get('timestamp')
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            continue
        if event.get('type') == 'step_start' and first_start is None:
            first_start = timestamp
        elif event.get('type') == 'step_finish':
            finish = timestamp
        elif event.get('type') == 'error':
            error = timestamp
    setup_ms = max(0, first_start - started_wall_ms) if first_start else None
    auth_ms = None
    inference_ms = None
    if first_start is not None:
        if mode == 'preflight' and error is not None:
            auth_ms = max(0, error - first_start)
        elif mode == 'live' and finish is not None:
            inference_ms = max(0, finish - first_start)
    return setup_ms, auth_ms, inference_ms


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Kill the whole CLI process tree without inheriting credential env."""
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    system_root = os.environ.get('SystemRoot', r'C:\Windows')
    subprocess.run(
        ['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env={'SystemRoot': system_root},
        check=False, timeout=10,
    )
    if process.poll() is None:
        process.kill()


def _output_tokens(lines: list[str]) -> int | None:
    total = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('type') != 'step_finish':
            continue
        part = event.get('part')
        tokens = part.get('tokens') if isinstance(part, dict) else None
        output = tokens.get('output') if isinstance(tokens, dict) else None
        if isinstance(output, int) and not isinstance(output, bool):
            total = (total or 0) + output
    return total


def _run_cli(command: list[str], env: dict, cwd: str,
             timeout_s: int) -> tuple[str, str, int | None, bool]:
    """Run the CLI headless in its own process group; bound it by `timeout_s`.

    stdin is /dev/null (an open idle stdin blocks a v2 run before any provider
    contact). On timeout the whole group is killed and the leader reaped. On
    POSIX the group is also killed after a normal exit, so nothing the CLI
    spawned can outlive the run. OSError from the launch propagates.
    """
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
        start_new_session=(os.name == 'posix'))
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return stdout or '', stderr or '', process.returncode, False
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        return stdout or '', stderr or '', None, True
    finally:
        if os.name == 'posix':
            _terminate_process_group(process)
        process.wait()


def _record_failure(result_path: str, mode: str, version: str | None,
                    reason: str, start: float, execution_count: int = 0) -> dict:
    total_ms = int((time.monotonic() - start) * 1000)
    return _write_result(
        result_path, mode=mode, opencode_version=version, status='failed',
        reason=reason, error_class=None, error_status_code=None,
        exit_code=None, timed_out=False, event_count=0, stderr_lines=0,
        setup_ms=None, auth_ms=None, inference_ms=None, total_ms=total_ms,
        execution_count=execution_count, output_tokens=None)


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    preflight = bool(args) and args[0] == '--preflight'
    if preflight:
        args = args[1:]
    if len(args) != 5:
        print('usage: opencode_native_smoke.py [--preflight] RESULT_JSON BINARY '
              'AGENT_HOME WORKDIR TIMEOUT_MIN', file=sys.stderr)
        return 2
    mode = 'preflight' if preflight else 'live'
    result_path, binary, agent_home, workdir = args[0], args[1], args[2], args[3]
    try:
        timeout_min = int(args[4])
    except ValueError:
        print('timeout must be an integer number of minutes', file=sys.stderr)
        return 2

    # Canonicalize before any subprocess runs. The inference run later changes
    # cwd to the smoke workdir; a relative binary containing '/' would then be
    # resolved from that new cwd and raise ENOENT on POSIX. Pinned to the
    # checked-out runtime by the caller — never user-configurable at runtime.
    binary = os.path.abspath(binary)
    agent_home = os.path.abspath(agent_home)
    workdir = os.path.abspath(workdir)
    manifest_path = result_path + '.runtime.json'
    key = resolve_key(mode, os.environ)

    start = time.monotonic()
    started_wall_ms = int(time.time() * 1000)

    if not key:
        _record_failure(result_path, mode, None, 'missing_api_key', start)
        print(f'native smoke: mode={mode} status failed reason=missing_api_key')
        return 1

    # No credential below this line until the inference env is built: the
    # version probe runs with an env that does not contain the key at all.
    home_env = build_child_env(agent_home, workdir, '')
    for directory in (agent_home, workdir,
                      home_env['XDG_CONFIG_HOME'], home_env['XDG_DATA_HOME'],
                      home_env['XDG_CACHE_HOME'], home_env['XDG_STATE_HOME']):
        os.makedirs(directory, exist_ok=True)

    try:
        prepare_runtime(agent_home, manifest_path, binary)
        validate_runtime(agent_home, manifest_path, binary)
    except (ValueError, OSError) as error:
        reason = str(error) if (isinstance(error, ValueError)
                                and str(error).startswith('runtime_')) \
            else 'runtime_seal_failed'
        _record_failure(result_path, mode, None, reason, start)
        print(f'native smoke: mode={mode} status failed reason={reason}')
        return 1

    try:
        version = read_version(binary, home_env)
    except (VersionProbeError, OSError, ValueError) as error:
        reason = _esc_reason(error) or 'opencode_version_unverified'
        _record_failure(result_path, mode, None, reason, start)
        print(f'native smoke: mode={mode} status failed reason={reason}')
        return 1

    if version != EXPECTED_OPENCODE_VERSION:
        _record_failure(result_path, mode, version,
                        'opencode_version_mismatch', start)
        print(f'native smoke: mode={mode} status failed reason=opencode_version_mismatch')
        return 1

    try:
        validate_runtime(agent_home, manifest_path, binary)
    except (ValueError, OSError) as error:
        _record_failure(result_path, mode, version, 'runtime_mutated_before_run',
                        start)
        print(f'native smoke: mode={mode} status failed reason=runtime_mutated_before_run')
        return 1

    env = build_child_env(agent_home, workdir, key)
    command = build_command(binary)
    try:
        stdout, stderr, returncode, timed_out = _run_cli(
            command, env, workdir, timeout_min * 60 + 90)
    except OSError:
        _record_failure(result_path, mode, version, 'cli_launch_error', start,
                        execution_count=1)
        print(f'native smoke: mode={mode} status failed reason=cli_launch_error')
        return 1
    lines = stdout.splitlines()
    stderr_lines = len(stderr.splitlines())
    exit_code = None if timed_out else returncode

    status, reason, error_class, error_status_code = evaluate(
        lines, exit_code, timed_out)
    try:
        validate_runtime(agent_home, manifest_path, binary)
    except (ValueError, OSError):
        status, reason = 'failed', 'runtime_mutated_during_run'
    if mode == 'preflight' and reason != 'runtime_mutated_during_run':
        # The objective is envelope compatibility, not inference: a synthetic
        # key must reach auth (401) rather than be policy-rejected (403).
        status, reason = probe_verdict(status, reason, error_class,
                                       error_status_code)
    total_ms = int((time.monotonic() - start) * 1000)
    setup_ms, auth_ms, inference_ms = _phase_timings(
        lines, started_wall_ms, total_ms, mode)
    record = _write_result(result_path, mode=mode, opencode_version=version,
                           status=status, reason=reason, error_class=error_class,
                           error_status_code=error_status_code,
                           exit_code=exit_code, timed_out=timed_out,
                           event_count=len(lines), stderr_lines=stderr_lines,
                           setup_ms=setup_ms, auth_ms=auth_ms,
                           inference_ms=inference_ms, total_ms=total_ms,
                           execution_count=1,
                           output_tokens=_output_tokens(lines))
    print(_summary(record))
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
