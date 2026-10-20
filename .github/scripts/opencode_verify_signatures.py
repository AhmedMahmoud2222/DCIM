#!/usr/bin/env python3
"""Fail when any commit OpenCode published lacks a GitHub-verified signature (v1.0).

Usage: opencode_verify_signatures.py REPO BASE_REF HEAD_REF [HEAD_REF ...]
Compares each HEAD_REF against BASE_REF through the REST compare API and checks
commit.verification.verified for every commit in the range. Uses `gh api`, so
GH_TOKEN must be set by the caller. Prints SHAs and reasons only, never tokens.
"""
from __future__ import annotations

import json
import subprocess
import sys


def unverified(commits: list[dict]) -> list[str]:
    bad = []
    for commit in commits:
        verification = (commit.get('commit') or {}).get('verification') or {}
        if verification.get('verified') is not True:
            bad.append(f"{commit.get('sha', '?')[:12]} ({verification.get('reason', 'unknown')})")
    return bad


def compare(repo: str, base: str, head: str) -> list[dict]:
    out = subprocess.run(
        ['gh', 'api', '--paginate', '--slurp', f'repos/{repo}/compare/{base}...{head}'],
        check=True, capture_output=True, text=True,
    ).stdout
    commits: list[dict] = []
    for page in json.loads(out):
        commits.extend(page.get('commits', []))
    return commits


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print('usage: opencode_verify_signatures.py REPO BASE_REF HEAD_REF...', file=sys.stderr)
        return 2
    repo, base, heads = argv[1], argv[2], argv[3:]
    failed = False
    for head in heads:
        try:
            bad = unverified(compare(repo, base, head))
        except (subprocess.CalledProcessError, ValueError):
            print(f'::error::could not verify signatures for {head}')
            failed = True
            continue
        if bad:
            failed = True
            print(f'::error::unsigned or unverified commits on {head}: ' + ', '.join(bad))
        else:
            print(f'{head}: all commits verified')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
