#!/usr/bin/env python3
"""Fail when any commit OpenCode published lacks a GitHub-verified signature (v2.0).

Usage: opencode_verify_signatures.py REPO BASE_REF HEAD_REF [HEAD_REF ...]
       opencode_verify_signatures.py --commit REPO SHA EXPECTED_AUTHOR_LOGIN
The --commit mode checks one published commit: verified == true, reason valid,
and authored by the dedicated App bot (so a commit signed by anyone else fails).
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


def check_commit(data: dict, expect_login: str) -> str:
    """Return '' when the commit passes, else the failure reason."""
    verification = (data.get('commit') or {}).get('verification') or {}
    login = (data.get('author') or {}).get('login', '')
    if verification.get('verified') is not True or verification.get('reason') != 'valid':
        return f"not verified ({verification.get('reason', 'unknown')})"
    if login.lower() != expect_login.lower():
        return f'unexpected author {login or "unknown"}'
    return ''


def main_commit(argv: list[str]) -> int:
    if len(argv) != 5:
        print('usage: opencode_verify_signatures.py --commit REPO SHA EXPECTED_AUTHOR_LOGIN', file=sys.stderr)
        return 2
    try:
        out = subprocess.run(['gh', 'api', f'repos/{argv[2]}/commits/{argv[3]}'],
                             check=True, capture_output=True, text=True).stdout
        problem = check_commit(json.loads(out), argv[4])
    except (subprocess.CalledProcessError, ValueError):
        problem = 'commit lookup failed'
    if problem:
        print(f'::error::published commit {argv[3][:12]} failed verification: {problem}')
        return 1
    print(f'{argv[3][:12]}: verified and authored by {argv[4]}')
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == '--commit':
        return main_commit(argv)
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
