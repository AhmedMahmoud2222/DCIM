#!/usr/bin/env python3
"""Synthetic model-health diagnostic (diagnostic v1.0).

Reads the live OpenCode model catalog and performs minimal non-sensitive
synthetic inference on two allowlisted free models. Records sanitized status
evidence (model ID, status, error class, latency) — never keys, tokens,
headers, or private prompts.

Allowlist: opencode/glm-5.3-flash-free, opencode/deepseek-v4.1-flash-free.
Hard-coded synthetic prompt. No arbitrary model or prompt channel.

This diagnostic is independent of the model registry's production-eligibility
gate: it never sets terms_verified, repo_code_approved, or
operational_status, never writes code, and never creates Draft PRs.

Usage: model_health_diagnostic.py OUT_JSON
Environment: OPENCODE_API_KEY (read from environment, never printed).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Narrow allowlist: only these two model IDs may be probed.
ALLOWED_MODELS = frozenset({
    'opencode/glm-5.3-flash-free',
    'opencode/deepseek-v4.1-flash-free',
})

# Hard-coded non-sensitive synthetic prompt. Never accepts untrusted input.
SYNTHETIC_PROMPT = 'Respond with exactly the single word: OK'

CATALOG_ENDPOINT = 'https://opencode.ai/inference/openai/v1/models'
INFERENCE_ENDPOINT = 'https://opencode.ai/inference/openai/v1/chat/completions'
REQUEST_TIMEOUT = 30  # seconds
MAX_TOKENS = 5


def http_get_json(url: str, key: str) -> object:
    request = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {key}',
                 'User-Agent': 'dcim-model-health-diagnostic'},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read(2_000_000))


def http_post_json(url: str, key: str, model: str, prompt: str) -> dict:
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': MAX_TOKENS,
    }).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'User-Agent': 'dcim-model-health-diagnostic',
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(2_000_000).decode('utf-8')
    return json.loads(raw)


def classify_error(error: Exception) -> str:
    """Map an exception to a sanitized error class. Never includes the prompt or key."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 401:
            return 'auth_error'
        if error.code == 403:
            return 'forbidden'
        if error.code == 429:
            return 'rate_limit'
        if error.code == 404:
            return 'model_not_available'
        if 500 <= error.code < 600:
            return 'upstream_error'
        return f'http_{error.code}'
    if isinstance(error, urllib.error.URLError):
        return 'network_error'
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, OSError):
        return 'connection_error'
    return 'unknown_error'


def normalize_id(value: str) -> str:
    return value if value.startswith('opencode/') else f'opencode/{value}'


def extract_ids(payload: object) -> set[str]:
    """Extract model IDs from a catalog payload. Pure function, no network."""
    items = payload.get('data') if isinstance(payload, dict) else payload
    ids = set()
    for item in items if isinstance(items, list) else []:
        value = item.get('id') if isinstance(item, dict) else item
        if isinstance(value, str) and value:
            ids.add(normalize_id(value))
    return ids


def fetch_catalog(key: str, get=http_get_json) -> set[str]:
    """Return the set of model IDs from the live catalog."""
    payload = get(CATALOG_ENDPOINT, key)
    return extract_ids(payload)


def probe_model(key: str, model: str) -> dict:
    """Run one synthetic inference probe. Returns a sanitized status record."""
    record = {
        'model': model,
        'allowed': model in ALLOWED_MODELS,
        'catalog_present': False,
        'status': 'not_tested',
        'error_class': None,
        'latency_ms': None,
    }
    if model not in ALLOWED_MODELS:
        record['status'] = 'not_allowlisted'
        return record

    start = time.monotonic()
    try:
        response = http_post_json(INFERENCE_ENDPOINT, key, model, SYNTHETIC_PROMPT)
        latency_ms = int((time.monotonic() - start) * 1000)
        record['status'] = 'healthy'
        record['latency_ms'] = latency_ms
        # Sanitized evidence only: a short hash of the response id, never content.
        response_id = (response.get('id') or '') if isinstance(response, dict) else ''
        if response_id:
            record['response_id_hash'] = hashlib.sha256(response_id.encode()).hexdigest()[:12]
    except Exception as error:  # noqa: BLE001 — classify any failure path
        latency_ms = int((time.monotonic() - start) * 1000)
        record['status'] = 'unhealthy'
        record['error_class'] = classify_error(error)
        record['latency_ms'] = latency_ms
    return record


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: model_health_diagnostic.py OUT_JSON', file=sys.stderr)
        return 2
    key = os.environ.get('OPENCODE_API_KEY', '')
    if not key:
        print('::error::OPENCODE_API_KEY is not configured', file=sys.stderr)
        return 1
    try:
        catalog_ids = fetch_catalog(key)
    except Exception as error:  # noqa: BLE001 — catalog fetch failure is a diagnostic result
        print(f'::error::catalog fetch failed: {classify_error(error)}', file=sys.stderr)
        catalog_ids = set()

    results = []
    for model in sorted(ALLOWED_MODELS):
        record = probe_model(key, model)
        record['catalog_present'] = model in catalog_ids
        # If the model is not in the catalog, override to not_tested even if probe ran.
        if not record['catalog_present'] and record['status'] == 'healthy':
            record['status'] = 'not_in_catalog'
        results.append(record)

    report = {
        'diagnostic_version': '1.0',
        'catalog_endpoint': CATALOG_ENDPOINT,
        'catalog_model_count': len(catalog_ids),
        'models_tested': len(results),
        'results': results,
    }
    with open(argv[1], 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
    print(f'diagnostic: {len(results)} model(s) probed, '
          f'{sum(1 for r in results if r["status"] == "healthy")} healthy, '
          f'{sum(1 for r in results if r["status"] == "unhealthy")} unhealthy')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
