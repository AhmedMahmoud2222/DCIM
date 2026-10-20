#!/usr/bin/env python3
"""Deterministic OpenCode model router (router v1.0).

Selects one model from the version-controlled registry. No LLM is consulted and
nothing in the task text can name a provider or model: text is only matched
against fixed keyword tables. The result is written to GITHUB_OUTPUT as
model / category / reason / sensitive, or the script exits 1 with an
actionable message and no model.

Usage: opencode_router.py --registry PATH --available PATH [--hint-path P ...]
Task text arrives through the TASK environment variable, never argv or shell.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# The only models this router can ever emit. Changing this set needs a code
# change, a test change and owner review (CODEOWNERS), not just a registry edit.
APPROVED_MODELS = (
    'opencode/minimax-m3.1-flash-free',
    'opencode/mimo-v2.6-flash-free',
    'opencode/deepseek-v4.1-flash-free',
    'opencode/glm-5.3-flash-free',
    'opencode/nemotron-3.5-lightning-free',
    'opencode/nemotron-3-ultra-free',
)
CATEGORIES = ('small_fix', 'general', 'backend', 'frontend', 'analysis_large', 'triage')
# Tie-break order when two categories score equally.
PRIORITY = ('analysis_large', 'backend', 'frontend', 'small_fix', 'triage', 'general')
KEYWORDS = {
    'analysis_large': ('architecture', 'cross-module', 'cross module', 'across modules',
                       'whole repo', 'entire repo', 'audit', 'dependency graph'),
    'backend': ('backend', 'api', 'endpoint', 'fastapi', 'sqlalchemy', 'alembic', 'migration',
                'database', 'postgres', 'sql', 'schema', 'pytest'),
    'frontend': ('frontend', 'ui', 'react', 'component', 'css', 'tailwind', 'page', 'button',
                 'typescript', 'vite'),
    'small_fix': ('typo', 'format', 'formatting', 'lint', 'readme', 'docs', 'documentation',
                  'comment', 'rename', 'wording'),
    'triage': ('triage', 'explain', 'summarize', 'summarise', 'investigate', 'why does',
               'what does'),
}
PATH_HINTS = (('backend/', 'backend'), ('frontend/', 'frontend'), ('docs/', 'small_fix'),
              ('edge_collector/', 'backend'), ('tests/', 'backend'))
# Material that must never reach a free third-party endpoint.
SENSITIVE_RE = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|'
    r'\bsk-[A-Za-z0-9]{20,}|(?i:\b(password|passwd|secret|api[_ -]?key|token)\s*[:=]\s*\S{6,})|'
    r'(?i:\b(production data|customer data|credentials?|pii|personal data)\b)'
)


class RoutingError(Exception):
    pass


def classify(task: str, hint_paths: list[str] | None = None) -> tuple[str, str]:
    text = (task or '').lower()
    scores = {name: 0 for name in CATEGORIES}
    for category, words in KEYWORDS.items():
        for word in words:
            if re.search(r'(?<![a-z0-9])' + re.escape(word) + r'(?![a-z0-9])', text):
                scores[category] += 1
    for path in hint_paths or []:
        for prefix, category in PATH_HINTS:
            if path.startswith(prefix):
                scores[category] += 2
    best = max(scores.values())
    if best == 0:
        return 'general', 'no category keywords matched; default category'
    winner = next(name for name in PRIORITY if scores[name] == best)
    return winner, f'highest keyword/path score {best} for {winner}'


def normalize(model_id: str) -> str:
    return model_id if model_id.startswith('opencode/') else f'opencode/{model_id}'


def load_registry(registry: dict) -> dict:
    models = registry.get('models') or {}
    for model_id in models:
        if model_id not in APPROVED_MODELS or not model_id.endswith('-free'):
            raise RoutingError(f'registry lists a model outside the approved free allowlist: {model_id}')
    routes = registry.get('routes') or {}
    if set(routes) != set(CATEGORIES):
        raise RoutingError('registry routes must cover exactly the defined categories')
    if registry.get('default_category') not in CATEGORIES:
        raise RoutingError('registry default_category is invalid')
    for category, chain in routes.items():
        for model_id in chain:
            if model_id not in models:
                raise RoutingError(f'route {category} references unregistered model {model_id}')
    return registry


def select(registry: dict, category: str, available: set[str]) -> tuple[str, list[str], str]:
    """Return (model, ordered_candidates, reason). Raise RoutingError when none qualify."""
    registry = load_registry(registry)
    chain = registry['routes'][category]
    limit = int(registry.get('max_candidates', 3))
    eligible = [m for m in chain if registry['models'][m].get('repo_code_approved') is True]
    skipped = [m for m in chain if m not in eligible]
    candidates = eligible[:limit]
    if not candidates:
        raise RoutingError(f'no repo-approved model for category {category}; owner data review needed')
    live = [m for m in candidates if m in available]
    if not live:
        raise RoutingError('every approved candidate is unavailable: ' + ', '.join(candidates))
    model = live[0]
    reason = f'category {category}'
    if skipped:
        reason += f'; skipped data-policy-blocked: {", ".join(skipped)}'
    if model != candidates[0]:
        reason += f'; fell back from {candidates[0]} (unavailable)'
    return model, candidates, reason


def route(task: str, registry: dict, available: set[str],
          hint_paths: list[str] | None = None) -> dict:
    if SENSITIVE_RE.search(task or ''):
        raise RoutingError('task text looks like secret or confidential material; '
                           'free third-party models are not permitted for it')
    category, why = classify(task, hint_paths)
    model, candidates, reason = select(registry, category, available)
    return {'model': model, 'category': category, 'candidates': candidates,
            'reason': f'{reason}; {why}'}


def parse_available(raw: object) -> set[str]:
    items = raw.get('data', raw) if isinstance(raw, dict) else raw
    ids = set()
    for item in items or []:
        value = item.get('id') if isinstance(item, dict) else item
        if isinstance(value, str) and value:
            ids.add(normalize(value))
    return ids


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry', required=True)
    parser.add_argument('--available', required=True)
    parser.add_argument('--hint-path', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        with open(args.registry, encoding='utf-8') as handle:
            registry = json.load(handle)
        with open(args.available, encoding='utf-8') as handle:
            available = parse_available(json.load(handle))
        result = route(os.environ.get('TASK', ''), registry, available, args.hint_path)
    except (OSError, ValueError, RoutingError) as error:
        print(f'::error::OpenCode routing stopped safely: {error}')
        return 1
    print(f"opencode router: model={result['model']} category={result['category']} "
          f"reason={result['reason']}")
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as handle:
            handle.write(f"model={result['model']}\ncategory={result['category']}\n")
            handle.write(f"reason={result['reason']}\n")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
