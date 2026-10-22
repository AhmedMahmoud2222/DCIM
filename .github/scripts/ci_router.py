"""Detect the changed paths of a CI event and write the CI route.

The workflow copies this file and ci_policy.py from the PR base commit into a
directory outside the checkout and runs that copy. The PR head can therefore not
change the route of its own run.

Environment:
  CI_EVENT_NAME  github.event_name
  CI_BASE_SHA    github.event.pull_request.base.sha (pull_request only)
  CI_HEAD_SHA    github.event.pull_request.head.sha, or github.sha
  GITHUB_OUTPUT  the step output file (optional; stdout always gets the route)

Fail-closed rules:
* Every event except pull_request selects full.
* A malformed SHA, a missing commit, a failed merge-base or a failed diff
  selects full. The router never falls back to a smaller diff.
* git diff runs with --no-renames, so a rename reports the old path and the new
  path. A rename out of a high-risk path therefore still selects full.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_policy

SHA = re.compile(r"[0-9a-f]{40}")


class DetectionError(Exception):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DetectionError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def changed_paths(base: str, head: str) -> tuple[str, list[str]]:
    """Return (merge_base, paths) for base...head. Raise DetectionError on any doubt."""
    for name, sha in (("base", base), ("head", head)):
        if not SHA.fullmatch(sha or ""):
            raise DetectionError(f"{name} SHA is not 40 lowercase hex characters")
    merge_base = _git("merge-base", base, head).strip()
    if not SHA.fullmatch(merge_base):
        raise DetectionError("merge-base did not return one commit")
    output = _git("diff", "--no-renames", "--name-only", "-z", merge_base, head)
    if output and not output.endswith("\0"):
        raise DetectionError("diff output is not NUL-terminated")
    return merge_base, [path for path in output.split("\0") if path]


def build_route(event: str, base: str, head: str) -> dict:
    route = {
        "policy_version": ci_policy.POLICY_VERSION,
        "event": event,
        "base": base,
        "head": head,
        "merge_base": "",
        "changed_count": 0,
        "degraded": False,
    }
    if event != "pull_request":
        decision = ci_policy.full([f"event {event!r} always runs every worker"])
    else:
        try:
            merge_base, paths = changed_paths(base, head)
        except DetectionError as error:
            route["degraded"] = True
            decision = ci_policy.full([f"change detection failed: {error}"])
        else:
            route["merge_base"] = merge_base
            route["changed_count"] = len(paths)
            decision = ci_policy.classify(paths)
    route["level"] = decision["level"]
    route["jobs"] = decision["jobs"]
    route["reasons"] = decision["reasons"][:50]
    return route


def outputs(route: dict) -> dict:
    """The step outputs. The gate checks that they agree with the route JSON."""
    values = {"level": route["level"], "head-sha": route["head"], "route": json.dumps(route, sort_keys=True)}
    for job in ci_policy.WORKERS:
        values[f"run-{job}"] = "true" if route["jobs"][job] else "false"
    return values


def main() -> int:
    route = build_route(
        os.environ.get("CI_EVENT_NAME", ""),
        os.environ.get("CI_BASE_SHA", ""),
        os.environ.get("CI_HEAD_SHA", ""),
    )
    values = outputs(route)
    print(json.dumps(route, indent=2, sort_keys=True))
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
