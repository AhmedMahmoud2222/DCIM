#!/usr/bin/env python3
"""Synthetic model-health diagnostic (diagnostic v1.4).

Reads the live OpenCode model catalog and performs minimal non-sensitive
synthetic inference on two allowlisted free models. Records sanitized status
evidence (model ID, status, error class, latency) — never keys, tokens,
headers, or private prompts.

Allowlist: glm-5.3-flash-free, deepseek-v4.1-flash-free (bare provider IDs,
as returned by the live catalog and used in direct API request bodies).
Hard-coded synthetic prompt. No arbitrary model or prompt channel.

This diagnostic is independent of the model registry's production-eligibility
gate: it never sets terms_verified, repo_code_approved, or
operational_status, never writes code, and never creates Draft PRs.

Usage: model_health_diagnostic.py OUT_JSON
Environment: OPENCODE_API_KEY (read from environment, never printed).

Exit code: 0 only when the catalog fetches successfully (catalog_status=ok),
both allowlisted models are present in the catalog, and both are healthy.
Otherwise exit 1.

Catalog errors are distinguished from per-model unavailability: a failed
catalog request sets catalog_status=error with catalog_error_class, and does
NOT overwrite inference results as not_in_catalog or assert model absence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Narrow allowlist: only these two bare provider model IDs may be probed.
# These are the IDs returned by the live catalog and expected in the
# inference request body.  The opencode/<id> form is only the CLI
# model-selector prefix and is never sent to the API.
ALLOWED_MODELS = frozenset({
    'glm-5.3-flash-free',
    'deepseek-v4.1-flash-free',
})

# Hard-coded non-sensitive synthetic prompt. Never accepts untrusted input.
SYNTHETIC_PROMPT = 'Respond with exactly the single word: OK'

# Official OpenCode endpoints per
# https://opencode.ai/v2/docs/console/models/ and
# https://dev.opencode.ai/v2/docs/console/api/inference:
#   GET catalog:      https://opencode.ai/inference/v1/models
#   POST chat:        https://opencode.ai/inference/openai/v1/chat/completions
# This path difference is expected, not a defect.
CATALOG_ENDPOINT = 'https://opencode.ai/inference/v1/models'
INFERENCE_ENDPOINT = 'https://opencode.ai/inference/openai/v1/chat/completions'
REQUEST_TIMEOUT = 30  # seconds
MAX_TOKENS = 5
READ_CAP = 2_000_000  # bytes; larger responses are treated as truncated

# Trusted host: only opencode.ai may receive the credential.
TRUSTED_HOST = 'opencode.ai'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that rejects every redirect to prevent credential leakage."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, f'redirect blocked to {newurl}', headers, fp
        )


def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener that rejects redirects and verifies TLS by default.

    ``urllib.request.build_opener(_NoRedirect())`` installs the no-redirect
    handler in place of the default ``HTTPRedirectHandler`` at construction
    time, so every 301/302/303/307/308 is rejected before the Authorization
    header can be copied to a redirect target.
    """
    return urllib.request.build_opener(_NoRedirect())


_OPENER = _build_opener()


def _assert_trusted(url: str) -> None:
    """Reject any URL that is not https://opencode.ai."""
    if not url.startswith('https://'):
        raise urllib.error.URLError(f'non-https scheme blocked: {url[:80]}')
    host = url[len('https://'):].split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    if host != TRUSTED_HOST:
        raise urllib.error.URLError(f'untrusted host blocked: {host[:80]}')


def http_get_json(url: str, key: str) -> object:
    _assert_trusted(url)
    request = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {key}',
                 'User-Agent': 'dcim-model-health-diagnostic'},
    )
    with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(READ_CAP + 1)
        if len(raw) > READ_CAP:
            raise urllib.error.HTTPError(
                url, 0, 'response body exceeds read cap', response.headers, None
            )
        return json.loads(raw.decode('utf-8'))


def http_post_json(url: str, key: str, model: str, prompt: str) -> dict:
    _assert_trusted(url)
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
    with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(READ_CAP + 1)
        if len(raw) > READ_CAP:
            raise urllib.error.HTTPError(
                url, 0, 'response body exceeds read cap', response.headers, None
            )
        return json.loads(raw.decode('utf-8'))


def classify_error(error: Exception) -> str:
    """Map an inference exception to a sanitized error class. Never includes the prompt or key."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 401:
            return 'auth_error'
        if error.code == 403:
            return 'forbidden'
        if error.code == 429:
            return 'rate_limit'
        if error.code == 404:
            return 'model_not_available'
        if 500 <= error.code < 600 or error.code == 0:
            return 'upstream_error'
        return f'http_{error.code}'
    if isinstance(error, urllib.error.URLError):
        return 'network_error'
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, OSError):
        return 'connection_error'
    return 'unknown_error'


def classify_catalog_error(error: Exception) -> str:
    """Map a catalog-level exception to a sanitized error class.

    Distinct from classify_error: a catalog HTTP 404 is catalog_http_404,
    never model_not_available (which is reserved for per-model inference
    404s).  All other catalog failures are catalog_unavailable.
    """
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return 'catalog_http_404'
        return 'catalog_unavailable'
    if isinstance(error, urllib.error.URLError):
        return 'catalog_unavailable'
    if isinstance(error, TimeoutError):
        return 'catalog_unavailable'
    if isinstance(error, OSError):
        return 'catalog_unavailable'
    return 'catalog_unavailable'


def normalize_id(value: str) -> str:
    """Return the bare provider model ID unchanged.

    The live catalog returns bare IDs (e.g. glm-5.3-flash-free).  The
    opencode/<id> prefix is only the CLI model-selector form and is never
    part of the catalog or direct API payload.
    """
    return value


def extract_ids(payload: object) -> set[str]:
    """Extract model IDs from a catalog payload. Pure function, no network."""
    items = payload.get('data') if isinstance(payload, dict) else payload
    ids = set()
    for item in items if isinstance(items, list) else []:
        value = item.get('id') if isinstance(item, dict) else item
        if isinstance(value, str) and value:
            ids.add(normalize_id(value))
    return ids


def fetch_catalog(key: str, get=http_get_json) -> dict:
    """Fetch the live catalog.

    Returns {'ids': set[str], 'status': 'ok'|'error', 'error_class': str|None}.
    A failed catalog request is reported at the catalog level; it is never
    interpreted as per-model unavailability.
    """
    try:
        payload = get(CATALOG_ENDPOINT, key)
        ids = extract_ids(payload)
        return {'ids': ids, 'status': 'ok', 'error_class': None}
    except Exception as error:
        return {'ids': set(), 'status': 'error',
                'error_class': classify_catalog_error(error)}


def _validate_healthy_response(response: dict, model: str) -> None:
    """Require a well-formed completion response before marking healthy.

    Raises ValueError with a sanitized message if the response is not a valid
    completion for the requested model.
    """
    if not isinstance(response, dict):
        raise ValueError('response is not a JSON object')
    response_id = response.get('id')
    if not isinstance(response_id, str) or not response_id:
        raise ValueError('missing response id')
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) == 0:
        raise ValueError('missing or empty choices')
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError('choices[0] is not an object')
    message = first.get('message')
    if not isinstance(message, dict):
        raise ValueError('missing message object')
    content = message.get('content')
    if not isinstance(content, str) or not content:
        raise ValueError('missing or empty message content')
    response_model = response.get('model')
    if response_model != model:
        raise ValueError(f'response model mismatch: expected {model}')


def probe_model(key: str, model: str) -> dict:
    """Run one synthetic inference probe. Returns a sanitized status record."""
    record = {
        'model': model,
        'allowed': model in ALLOWED_MODELS,
        'catalog_present': False,
        'status': 'not_tested',
        'error_class': None,
        'latency_ms': None,
        'response_model_matches': None,
    }
    if model not in ALLOWED_MODELS:
        record['status'] = 'not_allowlisted'
        return record

    start = time.monotonic()
    try:
        response = http_post_json(INFERENCE_ENDPOINT, key, model, SYNTHETIC_PROMPT)
        _validate_healthy_response(response, model)
        latency_ms = int((time.monotonic() - start) * 1000)
        record['status'] = 'healthy'
        record['latency_ms'] = latency_ms
        record['response_model_matches'] = True
        # Sanitized evidence only: a short hash of the response id, never content.
        response_id = response.get('id') or ''
        record['response_id_hash'] = hashlib.sha256(response_id.encode()).hexdigest()[:12]
    except ValueError as error:
        # ValueError from json.loads (malformed JSON) or _validate_healthy_response
        # (missing/empty fields, model mismatch). Both are invalid responses.
        latency_ms = int((time.monotonic() - start) * 1000)
        record['status'] = 'unhealthy'
        record['error_class'] = 'invalid_response'
        record['latency_ms'] = latency_ms
        record['response_model_matches'] = False
    except Exception as error:  # noqa: BLE001 — classify any other failure path
        latency_ms = int((time.monotonic() - start) * 1000)
        record['status'] = 'unhealthy'
        record['error_class'] = classify_error(error)
        record['latency_ms'] = latency_ms
        record['response_model_matches'] = False
    return record


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: model_health_diagnostic.py OUT_JSON', file=sys.stderr)
        return 2
    key = os.environ.get('OPENCODE_API_KEY', '')
    if not key:
        # Emit a sanitized artifact on missing-key failure so the failure
        # mode is recorded even without a live run.
        report = {
            'diagnostic_version': '1.4',
            'catalog_endpoint': CATALOG_ENDPOINT,
            'inference_endpoint': INFERENCE_ENDPOINT,
            'catalog_status': 'error',
            'catalog_error_class': 'missing_api_key',
            'catalog_model_count': 0,
            'models_tested': 0,
            'healthy_count': 0,
            'overall': 'unhealthy',
            'results': [],
        }
        with open(argv[1], 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2)
        print('diagnostic: missing API key, overall=unhealthy')
        return 1

    catalog = fetch_catalog(key)
    catalog_ids = catalog['ids']
    catalog_status = catalog['status']
    catalog_error_class = catalog['error_class']

    results = []
    for model in sorted(ALLOWED_MODELS):
        record = probe_model(key, model)
        # Only set catalog_present when the catalog succeeded.  When the
        # catalog failed, we do not assert model absence and we do not
        # overwrite inference results as not_in_catalog.
        if catalog_status == 'ok':
            record['catalog_present'] = model in catalog_ids
        results.append(record)

    healthy_count = sum(1 for r in results if r['status'] == 'healthy')
    all_healthy = healthy_count == len(results)
    all_in_catalog = all(r['catalog_present'] for r in results)
    # Overall is unhealthy when the catalog failed, any model is absent from
    # the catalog, or any model is unhealthy.
    overall = 'healthy' if (catalog_status == 'ok' and all_in_catalog and all_healthy) else 'unhealthy'

    report = {
        'diagnostic_version': '1.4',
        'catalog_endpoint': CATALOG_ENDPOINT,
        'inference_endpoint': INFERENCE_ENDPOINT,
        'catalog_status': catalog_status,
        'catalog_error_class': catalog_error_class,
        'catalog_model_count': len(catalog_ids),
        'models_tested': len(results),
        'healthy_count': healthy_count,
        'overall': overall,
        'results': results,
    }
    with open(argv[1], 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
    print(f'diagnostic: catalog={catalog_status} '
          f'{healthy_count}/{len(results)} model(s) healthy, overall={overall}')
    return 0 if overall == 'healthy' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
