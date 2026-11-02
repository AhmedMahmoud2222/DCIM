#!/usr/bin/env python3
"""Read the live OpenCode model catalog (catalog reader v1.0).

Tries the current inference endpoint first, then the legacy /zen route, which
OpenCode's own gateway still proxies (console app inference-proxy.ts maps
GET /zen/v1/models to the same service). Writes {"endpoint", "data": [{"id"}]}.
The key comes from OPENCODE_API_KEY and is never printed.

Usage: opencode_catalog.py OUT_JSON
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ENDPOINTS = ('https://opencode.ai/inference/openai/v1/models', 'https://opencode.ai/zen/v1/models')
ATTEMPTS = 2


def http_get(url: str, key: str) -> object:
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}',
                                                   'User-Agent': 'dcim-opencode-catalog'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read(2_000_000))


def ids_from(payload: object) -> list[str]:
    items = payload.get('data') if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        value = item.get('id') if isinstance(item, dict) else item
        if isinstance(value, str) and value:
            out.append(value)
    return out


def fetch(get, endpoints=ENDPOINTS, sleep=time.sleep) -> tuple[str, list[str]]:
    for endpoint in endpoints:
        for attempt in range(ATTEMPTS):
            try:
                ids = ids_from(get(endpoint))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                ids = []
            if ids:
                return endpoint, ids
            if attempt + 1 < ATTEMPTS:
                sleep(3)
    raise RuntimeError('no OpenCode model catalog endpoint returned models')


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: opencode_catalog.py OUT_JSON', file=sys.stderr)
        return 2
    key = os.environ.get('OPENCODE_API_KEY', '')
    if not key:
        print('::error::OPENCODE_API_KEY is not configured in the opencode-agent environment')
        return 1
    try:
        endpoint, ids = fetch(lambda url: http_get(url, key))
    except RuntimeError as error:
        print(f'::error::{error}; stopping before any model call')
        return 1
    with open(argv[1], 'w', encoding='utf-8') as handle:
        json.dump({'endpoint': endpoint, 'data': [{'id': i} for i in ids]}, handle)
    print(f'catalog: {len(ids)} models from {endpoint}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
