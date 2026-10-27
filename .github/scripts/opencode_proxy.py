#!/usr/bin/env python3
"""Loopback credential proxy for the sandboxed agent (proxy v2.0).

The agent user never receives the real OpenCode key. It talks to this proxy on
127.0.0.1 with a placeholder; the proxy validates every request itself, replaces the
credential, and forwards only to the OpenCode inference host over HTTPS. Combined with a
firewall rule that lets the agent user reach loopback only, the agent has no route to any
other host. Enforcement happens here; nothing relies on the upstream provider.

Each proxy instance is bound to ONE model chosen by the trusted router. A request is
forwarded only if all of these hold:
  - method and path are on the exact allow-list (chat completions POST, model-list GET)
  - no query string, no Transfer-Encoding, no Content-Encoding (the body must be inspectable)
  - the body is a JSON object with no duplicate keys, only allow-listed top-level fields,
    and `model` equal to the bound model
At startup the bound model must be one of the six approved free models, must pass every
registry requirement (terms verified, approved for repository code, healthy), and must not
be a model the reviewer is forbidden to use.

Rejections are counted and logged as {code, method, path} only: never headers, bodies,
prompts or credentials.

Usage: opencode_proxy.py PORT_FILE MODEL REGISTRY_JSON [DENY_MODELS_JSON]
       (key in OPENCODE_API_KEY, removed from the environment after it is read)
Test hook: OPENCODE_PROXY_UPSTREAM=http://host:port (default https://opencode.ai)
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode_router as router  # noqa: E402

POST_PATHS = ('/inference/openai/v1/chat/completions',)
GET_PATHS = ('/inference/openai/v1/models', '/zen/v1/models')
MAX_BODY = 20 * 1024 * 1024
BODY_KEYS = frozenset({
    'model', 'messages', 'stream', 'stream_options', 'tools', 'tool_choice', 'parallel_tool_calls',
    'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'stop', 'presence_penalty',
    'frequency_penalty', 'seed', 'n', 'response_format', 'reasoning_effort', 'user'})
FORWARD_HEADERS = ('content-type', 'accept', 'user-agent', 'x-opencode-request-id')
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length', 'proxy-authenticate',
       'proxy-authorization', 'te', 'trailers', 'upgrade'}


class PolicyError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def bare(model: str) -> str:
    return model.split('/', 1)[1] if model.startswith('opencode/') else model


def validate_bound_model(model: str, registry: dict, denied: set[str]) -> str:
    """Return the bare provider model id, or raise PolicyError for any ineligible model."""
    full = router.normalize(model)
    router.load_registry(registry)
    if full not in router.APPROVED_MODELS or not full.endswith('-free'):
        raise PolicyError('model_not_approved_free')
    if full in {router.normalize(m) for m in denied}:
        raise PolicyError('model_denied_for_this_role')
    if router.blockers(registry['models'][full]):
        raise PolicyError('model_disabled_or_unhealthy')
    return bare(full)


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise PolicyError('duplicate_json_key')
        seen[key] = value
    return seen


def _reject_constant(_name):
    raise PolicyError('non_standard_json')


def check_request(method: str, raw_path: str, headers, body: bytes | None, bound: str) -> None:
    """Raise PolicyError(code) unless the request may be forwarded with the real credential."""
    if '?' in raw_path or '#' in raw_path or '%' in raw_path or '\\' in raw_path or '//' in raw_path \
            or '..' in raw_path:
        raise PolicyError('malformed_path')
    if headers.get('Transfer-Encoding') is not None:
        raise PolicyError('transfer_encoding_not_supported')
    encoding = (headers.get('Content-Encoding') or 'identity').strip().lower()
    if encoding != 'identity':
        raise PolicyError('content_encoding_not_supported')
    if method == 'GET':
        if raw_path not in GET_PATHS:
            raise PolicyError('endpoint_not_allowed')
        if body:
            raise PolicyError('unexpected_body')
        return
    if method != 'POST':
        raise PolicyError('method_not_allowed')
    if raw_path not in POST_PATHS:
        raise PolicyError('endpoint_not_allowed')
    if not (headers.get('Content-Type') or '').lower().startswith('application/json'):
        raise PolicyError('content_type_not_json')
    if not body:
        raise PolicyError('empty_body')
    try:
        data = json.loads(body.decode('utf-8'), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except PolicyError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PolicyError('malformed_json') from None
    if not isinstance(data, dict):
        raise PolicyError('body_not_object')
    extra = set(data) - BODY_KEYS
    if extra:
        raise PolicyError('unsupported_field')
    if 'model' not in data:
        raise PolicyError('model_missing')
    if not isinstance(data['model'], str) or data['model'] != bound:
        raise PolicyError('model_mismatch')
    if not isinstance(data.get('messages'), list) or not data['messages']:
        raise PolicyError('messages_invalid')


class Proxy:
    def __init__(self, key: str, upstream: str, bound: str, log_path: str | None = None):
        self.key, self.upstream, self.bound, self.log_path = key, upstream, bound, log_path
        self.rejections: list[dict] = []
        self.forwarded = 0
        self._lock = threading.Lock()

    def record_rejection(self, code: str, method: str, path: str) -> None:
        entry = {'rejected': code, 'method': re.sub(r'[^A-Z]', '', method.upper())[:8],
                 'path': re.sub(r'[^A-Za-z0-9/._-]', '?', path.split('?')[0])[:80]}
        with self._lock:
            self.rejections.append(entry)
            if self.log_path:
                with open(self.log_path, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(entry) + '\n')
        print('proxy rejected request: ' + json.dumps(entry), file=sys.stderr, flush=True)


def make_handler(proxy: Proxy):
    target_scheme, _, rest = proxy.upstream.partition('://')
    host, _, port = rest.partition(':')

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, fmt, *args):  # no request details in logs
            pass

        def _send(self, code: int, text: str):
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(text.encode())

        def _forward(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                proxy.record_rejection('body_too_large_or_invalid_length', self.command, self.path)
                return self._send(413, 'rejected by sandbox proxy')
            body = self.rfile.read(length) if length else None
            try:
                check_request(self.command, self.path, self.headers, body, proxy.bound)
            except PolicyError as error:
                proxy.record_rejection(error.code, self.command, self.path)
                return self._send(403, f'rejected by sandbox proxy: {error.code}')
            headers = {k: v for k, v in ((n, self.headers.get(n)) for n in FORWARD_HEADERS) if v}
            headers['Authorization'] = f'Bearer {proxy.key}'
            if target_scheme == 'https':
                conn = http.client.HTTPSConnection(host, int(port or 443), timeout=300)
            else:
                conn = http.client.HTTPConnection(host, int(port or 80), timeout=300)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                proxy.forwarded += 1
                # read() waits for the requested byte count or EOF, buffering the
                # provider's SSE stream before OpenCode can emit generation events.
                while chunk := response.read1(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                try:
                    self._send(502, 'upstream unavailable')
                except OSError:
                    pass
            finally:
                conn.close()

        def _reject_other(self):
            proxy.record_rejection('method_not_allowed', self.command, self.path)
            self._send(405, 'rejected by sandbox proxy')

        do_GET = _forward
        do_POST = _forward
        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _reject_other

    return Handler


def serve(key: str, upstream: str, bound: str, port_file: str | None = None,
          log_path: str | None = None) -> tuple[http.server.ThreadingHTTPServer, Proxy]:
    proxy = Proxy(key, upstream, bound, log_path)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), make_handler(proxy))
    if port_file:
        with open(port_file, 'w', encoding='utf-8') as handle:
            handle.write(str(server.server_address[1]))
    return server, proxy


def main(argv: list[str]) -> int:
    api_key = os.environ.pop('OPENCODE_API_KEY', '')
    if not api_key or len(argv) not in (4, 5):
        print('::error::proxy needs OPENCODE_API_KEY, PORT_FILE, MODEL and REGISTRY_JSON')
        return 1
    try:
        with open(argv[3], encoding='utf-8') as handle:
            registry = json.load(handle)
        denied = set()
        if len(argv) == 5:
            with open(argv[4], encoding='utf-8') as handle:
                denied = {r['model'] for r in json.load(handle)}
        bound = validate_bound_model(argv[2], registry, denied)
    except (PolicyError, router.RoutingError, OSError, ValueError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, PolicyError) else type(error).__name__
        print(f'::error::proxy refused to start: {code}')
        return 1
    server, _proxy = serve(api_key, os.environ.get('OPENCODE_PROXY_UPSTREAM', 'https://opencode.ai'), bound,
                           argv[1], os.environ.get('OPENCODE_PROXY_LOG'))
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
