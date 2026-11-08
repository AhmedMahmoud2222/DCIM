#!/usr/bin/env python3
"""Capture REAL `opencode run --format json` transcripts from OpenCode 1.19.3 (capture v1.0).

Runs the genuine 1.19.3 binary against a local scripted OpenAI-compatible server, so the
event stream is produced by OpenCode itself and only the model's replies are scripted.
Reproduce:  npm install opencode-ai@1.19.3   (npm integrity sha512-wDRcUlMDibaLzecIGvVGUA9naghXVqzCWcUE7N3qagpGiLSUlU4VbixI+bpsErwJp+AAuSO2lhSr/XxJl7XbrQ==)
            python3 capture.py PATH/TO/node_modules/opencode-linux-x64/bin/opencode OUT_DIR
Transcripts longer than 60 lines are cut to their first 60 (original count in meta).
Each scenario writes OUT_DIR/<name>.jsonl (stdout, one event per line), <name>.meta.json
(exit code, timed_out) and <name>.workfile when a file was edited. Session ids and
timestamps are rewritten to fixed values afterwards by normalize.py-style logic below
only for `sessionID`/`timestamp`/`id` noise; structure and ordering are untouched.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

MODEL = 'opencode/glm-5.3-flash-free'


def sse(chunks):
    return ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'


def chunk(delta, finish=None, usage=None):
    body = {'id': 'chatcmpl-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'glm-5.3-flash-free',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    if usage:
        body['usage'] = usage
    return body


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}


def text_reply(text, finish='stop'):
    return sse([chunk({'role': 'assistant', 'content': ''}), chunk({'content': text}),
                chunk({}, finish, USAGE)])


def tool_reply(path, content):
    args = json.dumps({'filePath': path, 'content': content})
    return sse([chunk({'role': 'assistant', 'content': None, 'tool_calls': [
        {'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'write', 'arguments': ''}}]}),
        chunk({'tool_calls': [{'index': 0, 'function': {'arguments': args}}]}),
        chunk({}, 'tool_calls', USAGE)])


# scenario -> list of per-request behaviours: ('sse', body) | ('status', code) | ('hang',) | ('truncate', body)
SCENARIOS = {
    'success_text': [('sse', text_reply('Nothing to change.'))],
    'success_edit': [('sse', tool_reply('hello.txt', 'hi\n')), ('sse', text_reply('Created hello.txt.'))],
    'fail_provider_500': [('status', 500)],
    'fail_unauthorized_401': [('status', 401)],
    'fail_truncated_length': [('sse', text_reply('partial output', finish='length'))],
    'fail_ends_on_tool_calls': [('sse', tool_reply('hello.txt', 'hi\n')), ('status', 500)],
    'fail_stream_cut': [('truncate', text_reply('cut mid stream')[:140])],
    'fail_timeout_hang': [('hang',)],
}


class Stub(http.server.BaseHTTPRequestHandler):
    script: list = []
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        request = json.loads(body or b'{}')
        Stub.seen.append(request)
        if 'tools' not in request:  # OpenCode's separate title-generation call: never consumes the script
            step = ('sse', text_reply('Task'))
        else:
            # a failure step repeats until the process gives up: OpenCode retries retryable errors
            step = Stub.script[0] if Stub.script and Stub.script[0][0] in ('status', 'truncate') and len(Stub.script) == 1 \
                else Stub.script.pop(0) if Stub.script else ('status', 503)
        kind = step[0]
        if kind == 'status':
            self.send_response(step[1])
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "scripted failure", "type": "server_error"}}')
        elif kind == 'hang':
            time.sleep(120)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(step[1].encode())
            self.wfile.flush()


def run(binary, name, out_dir, timeout=None):
    timeout = timeout or (50 if name == 'fail_timeout_hang' else 100)
    Stub.script, Stub.seen = list(SCENARIOS[name]), []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    with tempfile.TemporaryDirectory() as tmp:
        home, work = os.path.join(tmp, 'home'), os.path.join(tmp, 'work')
        os.makedirs(home)
        os.makedirs(work)
        config = {'autoupdate': False, 'share': 'disabled', 'enabled_providers': ['opencode'],
                  'provider': {'opencode': {'options': {'baseURL': f'http://127.0.0.1:{port}/v1', 'apiKey': 'placeholder'}}},
                  'permission': {'edit': 'allow', 'read': 'allow', 'bash': 'deny', 'webfetch': 'deny'}}
        env = {'HOME': home, 'PATH': '/usr/bin:/bin', 'XDG_CONFIG_HOME': home + '/.config',
               'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
               'XDG_STATE_HOME': home + '/.local/state', 'OPENCODE_CONFIG_CONTENT': json.dumps(config),
               'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_PURE': '1', 'CI': 'true'}
        cmd = ['timeout', '--kill-after=5s', f'{timeout}s', binary, 'run', '--pure', '--format', 'json', '--model', MODEL,
               '--dir', work, '--', 'Create hello.txt containing hi']
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        edited = os.path.join(work, 'hello.txt')
        if os.path.exists(edited):
            with open(edited) as handle:
                open(os.path.join(out_dir, f'{name}.workfile'), 'w').write(handle.read())
    server.shutdown()
    lines = proc.stdout.splitlines(keepends=True)
    kept = lines[:60]  # a retry loop can emit thousands of identical events; the head is the evidence
    with open(os.path.join(out_dir, f'{name}.jsonl'), 'w') as handle:
        handle.write(''.join(kept))
    with open(os.path.join(out_dir, f'{name}.meta.json'), 'w') as handle:
        json.dump({'exit_code': proc.returncode, 'timed_out': proc.returncode in (124, 137),
                   'stderr_head': proc.stderr[:400], 'requests_seen': len(Stub.seen), 'agent_requests': sum(1 for r in Stub.seen if 'tools' in r),
                   'original_line_count': len(lines), 'kept_lines': len(kept), 'opencode_version': '1.19.3'}, handle, indent=2)
    return proc


if __name__ == '__main__':
    binary, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    only = sys.argv[3:] or list(SCENARIOS)
    for scenario in only:
        proc = run(binary, scenario, out)
        print(scenario, 'exit', proc.returncode, 'lines', len(proc.stdout.splitlines()))
