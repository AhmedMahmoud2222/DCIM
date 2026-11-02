#!/usr/bin/env python3
"""Capture REAL `opencode run --format json` transcripts from OpenCode 2.1.4 (capture v1.0).

Runs the genuine 2.1.4 binary against a loopback scripted provider and a synthetic key, so the event
stream is produced by OpenCode itself and only the model's replies are scripted.
Reproduce:  cd .github/opencode/runtime && npm ci --ignore-scripts
            python3 capture.py .github/opencode/runtime/node_modules/@opencode/cli-linux-x64/bin/opencode OUT_DIR
Each scenario writes OUT_DIR/<name>.jsonl (stdout, one event per line) and <name>.meta.json
(exit code, timed_out). Hanging scenarios (v2 retries a rejected request forever) are killed after
HANG_SECONDS and cut to their first MAX_LINES events, as the 1.19.3 fixtures were.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import opencode_sandbox as sandbox  # noqa: E402

MODEL = 'opencode/glm-5.3-flash-free'
HANG_SECONDS = 20
MAX_LINES = 12
USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}


def chunk(delta, finish=None, usage=None):
    body = {'id': 'chatcmpl-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'glm-5.3-flash-free',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    if usage:
        body['usage'] = usage
    return body


def sse(chunks, done=True):
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + ('data: [DONE]\n\n' if done else '')


def text_reply(text, finish='stop'):
    return ('sse', sse([chunk({'role': 'assistant', 'content': ''}), chunk({'content': text}), chunk({}, finish, USAGE)]))


def tool_reply(path, content):
    call = {'index': 0, 'id': 'call_1', 'type': 'function',
            'function': {'name': 'write', 'arguments': json.dumps({'path': path, 'content': content})}}
    return ('sse', sse([chunk({'role': 'assistant', 'tool_calls': [call]}), chunk({}, 'tool_calls', USAGE)]))


# scenario -> (per-request behaviour list; the last one repeats, run timeout in seconds)
SCENARIOS = {
    'success_text': ([text_reply('Nothing to change.')], 60),
    'success_edit': ([tool_reply('hello.txt', 'hi'), text_reply('Created hello.txt.')], 60),
    'fail_truncated_length': ([text_reply('Partial answ', finish='length')], 60),
    'fail_unauthorized_401': ([('status', 401)], 60),
    'fail_rate_limited_429': ([('status', 429)], HANG_SECONDS),
    'fail_provider_503': ([('status', 503)], HANG_SECONDS),
    'fail_stream_cut': ([('sse', sse([chunk({'role': 'assistant', 'content': 'Partial'})], done=False))], HANG_SECONDS),
}


def serve(behaviours):
    state = {'n': 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            kind, value = behaviours[min(state['n'], len(behaviours) - 1)]
            state['n'] += 1
            if kind == 'status':
                body = b'{"error": {"message": "synthetic"}}'
                self.send_response(value)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(value.encode())

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(binary, name, behaviours, seconds, out_dir):
    server = serve(behaviours)
    config = sandbox.agent_config(MODEL, server.server_address[1], 'implement')
    with tempfile.TemporaryDirectory() as tmp:
        home, work = os.path.join(tmp, 'home'), os.path.join(tmp, 'work')
        os.makedirs(home)
        os.makedirs(work)
        env = {'HOME': home, 'PATH': '/usr/bin:/bin', 'PWD': work, 'XDG_CONFIG_HOME': home + '/.config',
               'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
               'XDG_STATE_HOME': home + '/.local/state', 'OPENCODE_CONFIG_CONTENT': json.dumps(config),
               'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_CONFIG_PROJECT_DISABLE': '1',
               'OPENCODE_DISABLE_CLAUDE_CODE': '1', 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1',
               'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1', 'CI': 'true'}
        timed_out = False
        try:
            done = subprocess.run([binary, 'run', '--standalone', '--format', 'json', '--model', MODEL, '--', 'Make the change.'],
                                  cwd=work, env=env, capture_output=True, text=True, timeout=seconds, stdin=subprocess.DEVNULL)
            stdout, code = done.stdout, done.returncode
        except subprocess.TimeoutExpired as expired:
            stdout = expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or '')
            code, timed_out = 124, True
    server.shutdown()
    lines = [line for line in stdout.splitlines() if line.strip()]
    kept = lines[:MAX_LINES]
    (out_dir / f'{name}.jsonl').write_text('\n'.join(kept) + '\n', encoding='utf-8')
    (out_dir / f'{name}.meta.json').write_text(json.dumps({
        'exit_code': code, 'timed_out': timed_out, 'original_line_count': len(lines), 'kept_lines': len(kept),
        'opencode_version': '2.1.4'}, indent=2) + '\n', encoding='utf-8')
    print(f'{name}: exit={code} timed_out={timed_out} events={len(lines)}')


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    out_dir = Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (behaviours, seconds) in SCENARIOS.items():
        run(argv[1], name, behaviours, seconds, out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
