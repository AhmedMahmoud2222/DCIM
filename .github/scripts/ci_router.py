#!/usr/bin/env python3
"""Deterministic CI change router (router v1.0).

Computes the set of changed files between a base and a head commit, classifies
each path through :mod:`ci_policy`, and writes the CI levels/components to run
to ``GITHUB_OUTPUT``.

The router never fails the CI job: any change-detection problem (unresolvable
revisions, missing diff, unexpected error) is treated as ``detection-failed``
and forces Full CI (fail-closed). Nothing unknown ever routes to Fast CI.

Usage:
    python ci_router.py --base <sha> --head <sha>

Environment fallbacks: CI_BASE_SHA, CI_HEAD_SHA. When neither argument nor
environment provides a base, the router diffs HEAD against its first parent
(HEAD~1); when even that is unavailable it reports detection-failed.

Outputs (written to GITHUB_OUTPUT):
    risk-level             fast | component | full
    run-full-ci            true | false
    run-edge-collector     true | false
    run-database-contracts true | false
    run-opencode           true | false
    detection-failed       true | false
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Make ci_policy importable when the script is run from any working directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import ci_policy  # noqa: E402  (import after path insertion)

TRUE = "true"
FALSE = "false"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _run_git(args: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _lines(output: str) -> List[str]:
    return [line for line in output.splitlines() if line.strip()]


def changed_files(base: Optional[str], head: Optional[str]) -> Tuple[List[str], bool]:
    """Return (changed_file_paths, detection_failed).

    detection_failed is True when the changed set cannot be determined
    reliably. The caller must escalate to Full CI in that case.
    """
    resolved_base = base or _env("CI_BASE_SHA") or _env("GITHUB_EVENT_BEFORE")
    resolved_head = head or _env("CI_HEAD_SHA") or _env("GITHUB_SHA")

    # Try an explicit base..head diff first (pull request range).
    if resolved_base and resolved_head:
        result = _run_git(["diff", "--name-only", f"{resolved_base}...{resolved_head}"])
        if result.returncode == 0:
            return _lines(result.stdout), False
        # Three-dot (merge-base) diff can fail when the base is not fetched;
        # try two-dot as a fallback before giving up.
        result = _run_git(["diff", "--name-only", f"{resolved_base}..{resolved_head}"])
        if result.returncode == 0:
            return _lines(result.stdout), False

    # Fall back to HEAD vs its first parent (single-commit push / local test).
    result = _run_git(["diff", "--name-only", "HEAD~1..HEAD"])
    if result.returncode == 0:
        return _lines(result.stdout), False

    # Last resort: every tracked file (treat as a full change -> Full CI).
    result = _run_git(["ls-files"])
    if result.returncode == 0:
        return _lines(result.stdout), True  # degraded detection: escalate

    return [], True


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    print(f"{name}={value}")


def route(files: List[str], detection_failed: bool = False) -> dict:
    """Classify a concrete changed-file list and return the CI routing decision.

    Pure function: no git, no environment, no GITHUB_OUTPUT. Used by tests and
    by :func:`main`. Never raises — classification failure escalates to Full CI.
    """
    try:
        level = ci_policy.component_level(files)
        full = ci_policy.any_full_risk(files) or detection_failed
        if full:
            level = ci_policy.FULL
        components = ci_policy.affected_components(files)
    except Exception as error:  # fail-closed: classification itself must not block Full CI
        print(f"::error::ci router classification failed closed: {error}")
        level = ci_policy.FULL
        full = True
        components = []
        detection_failed = True

    run_full = TRUE if full else FALSE
    # Component jobs only run when Full CI is not already comprehensive.
    run_edge = TRUE if (ci_policy.EDGE_COLLECTOR in components and not full) else FALSE
    run_database = TRUE if (ci_policy.DATABASE_CONTRACTS in components and not full) else FALSE
    run_opencode = TRUE if (ci_policy.OPENCODE in components and not full) else FALSE

    return {
        "risk-level": level,
        "run-full-ci": run_full,
        "run-edge-collector": run_edge,
        "run-database-contracts": run_database,
        "run-opencode": run_opencode,
        "detection-failed": TRUE if detection_failed else FALSE,
        "files": files,
    }


def write_route(route_decision: dict) -> None:
    """Write a routing decision (from :func:`route`) to GITHUB_OUTPUT and the log."""
    _write_output("risk-level", route_decision["risk-level"])
    _write_output("run-full-ci", route_decision["run-full-ci"])
    _write_output("run-edge-collector", route_decision["run-edge-collector"])
    _write_output("run-database-contracts", route_decision["run-database-contracts"])
    _write_output("run-opencode", route_decision["run-opencode"])
    _write_output("detection-failed", route_decision["detection-failed"])
    print(
        f"ci router: risk={route_decision['risk-level']} full={route_decision['run-full-ci']} "
        f"edge={route_decision['run-edge-collector']} "
        f"database={route_decision['run-database-contracts']} "
        f"opencode={route_decision['run-opencode']} "
        f"detection_failed={route_decision['detection-failed']} files={len(route_decision['files'])}"
    )
    if route_decision["files"]:
        print("changed files: " + " ".join(route_decision["files"]))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=None, help="Base commit SHA")
    parser.add_argument("--head", default=None, help="Head commit SHA")
    args = parser.parse_args(argv)

    try:
        files, degraded = changed_files(args.base, args.head)
    except Exception as error:  # fail-closed: git itself is unusable
        print(f"::error::ci router failed closed: {error}")
        write_route(route([], detection_failed=True))
        return 0

    write_route(route(files, detection_failed=degraded))
    return 0


if __name__ == "__main__":
    sys.exit(main())
