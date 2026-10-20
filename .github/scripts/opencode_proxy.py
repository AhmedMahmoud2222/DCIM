#!/usr/bin/env python3
"""Loopback credential proxy for the sandboxed agent (proxy v1.0).

The agent user never receives the real OpenCode key. It talks to this proxy on
127.0.0.1 with a placeholder; the proxy, running as a different OS user, replaces
the Authorization header and forwards only to the OpenCode inference host over
HTTPS, on allow-listed paths. Combined with a firewall rule that lets the agent
user reach loopback only, the agent has no route to any other host.

Usage: opencode_proxy.py PORT_FILE   (key in OPENCODE_API_KEY, removed from env after read)
Test hooks: OPENCODE_PROXY_UPSTREAM=http://host:port (default https://opencode.ai)
"""
from __future__ import annotations

import http.client
import http.server
import os
import sys
import urllib.parse

ALLOWED_PREFIXES = ('/inference/', '/zen/v1/')
ALLOWED_METHODS = ('GET', 'POST')
MAX_BODY = 20 * 1024 * 1024
FORWARD_HEADERS = ('content-type', 'accept', 'accept-encoding', 'user-agent', 'x-opencode-request-id',
                   'anthropic-version', 'anthropic-beta')
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length', 'proxy-authenticate',
       'proxy-authorization', 'te', 'trailers', 'upgrade'}


def allowed(method: str, path: str) -> bool:
    parsed = urllib.parse.urlsplit(path)
    clean = parsed.path
    return (method in ALLOWED_METHODS and clean.startswith(ALLOWED_PREFIXES) and '..' not in clean
            and '//' not in clean and '%' not in clean)


def make_handler(key: str, upstream: str):
    target = urllib.parse.urlsplit(upstream)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, fmt, *args):  # no request details in logs
            pass

        def _deny(self, code: int, text: str):
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(text.encode())

        def _forward(self):
            if not allowed(self.command, self.path):
                return self._deny(403, 'blocked by sandbox proxy')
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY or length < 0:
                return self._deny(413, 'request too large')
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in ((n, self.headers.get(n)) for n in FORWARD_HEADERS) if v}
            headers['Authorization'] = f'Bearer {key}'
            if target.scheme == 'https':
                conn = http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=300)
            else:
                conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=300)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                while chunk := response.read(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                try:
                    self._deny(502, 'upstream unavailable')
                except OSError:
                    pass
            finally:
                conn.close()

        do_GET = _forward
        do_POST = _forward

    return Handler


def serve(key: str, upstream: str, port_file: str | None = None):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), make_handler(key, upstream))
    if port_file:
        with open(port_file, 'w', encoding='utf-8') as handle:
            handle.write(str(server.server_address[1]))
    return server


if __name__ == '__main__':
    api_key = os.environ.pop('OPENCODE_API_KEY', '')
    if not api_key or len(sys.argv) != 2:
        print('::error::proxy needs OPENCODE_API_KEY and a port file argument')
        sys.exit(1)
    serve(api_key, os.environ.get('OPENCODE_PROXY_UPSTREAM', 'https://opencode.ai'), sys.argv[1]).serve_forever()
