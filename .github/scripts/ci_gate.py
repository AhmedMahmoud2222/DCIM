"""Fail-closed CI gate for the required checks `backend` and `frontend`.

The gate jobs run with `if: always()` and read `toJSON(needs)`. A gate passes only
when all of these are true:
* The route job succeeded and its outputs are a valid route for the expected
  head SHA. A route below full must come from a pull_request diff that did not
  degrade.
* The ci-policy job succeeded.
* Each worker that the route selects finished with result `success`.
* Each worker that the route does not select is `skipped` or `success`.
* The gate `needs` list is exactly the scope below: no worker is missing or
  unaudited.

A worker that the base policy does not know (a new job) is always required.
Results `failure`, `cancelled`, a missing result and any unknown value fail.

Independent of the route, the gate computes the PR diff itself. A route below
full fails when the diff changes a trust path (TRUST_PREFIXES) or when the gate
cannot compute the diff. A PR that changes the CI controls always needs full.

Environment: NEEDS_JSON (toJSON(needs)), EXPECTED_HEAD_SHA, CI_BASE_SHA.
Usage: ci_gate.py backend|frontend
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

LEVELS = ("fast", "component", "full")
CONTROL_JOBS = ("route", "ci-policy")
GATES = {
    "backend": CONTROL_JOBS + (
        "backend-suite",
        "frontend-suite",
        "backend-runtime-matrix",
        "edge-collector",
        "edge-interop",
        "browser-e2e",
    ),
    "frontend": CONTROL_JOBS + ("frontend-suite", "browser-e2e"),
}
SHA = re.compile(r"[0-9a-f]{40}")
# The CI controls: the workflows, the route, policy and gate scripts, and their tests.
TRUST_PREFIXES = (".github/", "tests/ci/")


def evaluate(gate: str, needs: object, expected_head: str) -> list[str]:
    """Return the list of gate errors. An empty list means the gate passes."""
    if gate not in GATES:
        return [f"unknown gate {gate!r}"]
    if not isinstance(needs, dict):
        return ["needs is not an object"]
    scope = GATES[gate]
    errors = []
    for job in scope:
        if job not in needs:
            errors.append(f"{job}: missing from needs")
    for job in needs:
        if job not in scope:
            errors.append(f"{job}: in needs but not in the {gate} gate scope")
    if errors:
        return errors

    def result(job: str) -> object:
        entry = needs[job]
        return entry.get("result") if isinstance(entry, dict) else None

    for job in CONTROL_JOBS:
        if result(job) != "success":
            errors.append(f"{job}: result {result(job)!r}, expected 'success'")
    if errors:
        return errors

    if not SHA.fullmatch(expected_head or ""):
        return ["EXPECTED_HEAD_SHA is not a full SHA"]
    outputs = needs["route"].get("outputs")
    if not isinstance(outputs, dict):
        return ["route: outputs missing"]
    try:
        route = json.loads(outputs.get("route", ""))
    except (TypeError, ValueError):
        return ["route: route output is not valid JSON"]
    if not isinstance(route, dict) or not isinstance(route.get("jobs"), dict):
        return ["route: route JSON has no jobs object"]
    level = route.get("level")
    if level not in LEVELS or outputs.get("level") != level:
        return [f"route: invalid or inconsistent level {level!r}/{outputs.get('level')!r}"]
    if route.get("head") != expected_head or outputs.get("head-sha") != expected_head:
        return [f"route: head {route.get('head')!r} is not the expected head {expected_head}"]
    if level != "full" and (route.get("event") != "pull_request" or route.get("degraded") is not False):
        return [f"route: level {level} is allowed only for a clean pull_request detection"]
    jobs = route["jobs"]
    if any(not isinstance(value, bool) for value in jobs.values()):
        return ["route: a job flag is not a boolean"]
    if level == "full" and not all(jobs.values()):
        return ["route: level full does not select every worker"]
    if level == "fast" and any(jobs.values()):
        return ["route: level fast selects a worker"]
    if level == "component" and not any(jobs.values()):
        return ["route: level component selects no worker"]

    for job in scope:
        if job in CONTROL_JOBS:
            continue
        flag = outputs.get(f"run-{job}")
        if job in jobs and flag != ("true" if jobs[job] else "false"):
            errors.append(f"{job}: output run-{job}={flag!r} disagrees with the route JSON")
            continue
        applicable = level == "full" or job not in jobs or jobs[job]
        status = result(job)
        if applicable and status != "success":
            errors.append(f"{job}: required by route, result {status!r}")
        elif not applicable and status not in ("skipped", "success"):
            errors.append(f"{job}: not required by route, but result {status!r}")
    return errors


def pr_paths(base: str, head: str) -> list[str] | None:
    """The paths of merge-base(base, head)..head, or None when git cannot tell."""
    merge_base = subprocess.run(["git", "merge-base", base, head], capture_output=True, text=True, check=False)
    if merge_base.returncode != 0 or not SHA.fullmatch(merge_base.stdout.strip()):
        return None
    diff = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", merge_base.stdout.strip(), head],
                          capture_output=True, text=True, check=False)
    if diff.returncode != 0 or (diff.stdout and not diff.stdout.endswith("\0")):
        return None
    return [path for path in diff.stdout.split("\0") if path]


def scope_errors(level: str, paths: list[str] | None) -> list[str]:
    """Reject a route below full for a PR that changes the CI controls."""
    if level == "full":
        return []
    if paths is None:
        return [f"route: level {level} but the gate cannot compute the PR diff"]
    trusted = sorted(path for path in paths if path.lower().startswith(TRUST_PREFIXES))
    if trusted:
        return [f"route: level {level} but the PR changes CI control files: {', '.join(trusted[:5])}"]
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ci_gate.py backend|frontend", file=sys.stderr)
        return 2
    try:
        needs = json.loads(os.environ.get("NEEDS_JSON", ""))
    except ValueError:
        needs = None
    head = os.environ.get("EXPECTED_HEAD_SHA", "")
    errors = evaluate(argv[1], needs, head)
    if not errors:
        level = json.loads(needs["route"]["outputs"]["route"])["level"]
        paths = None if level == "full" else pr_paths(os.environ.get("CI_BASE_SHA", ""), head)
        errors = scope_errors(level, paths)
    for job, entry in sorted(needs.items()) if isinstance(needs, dict) else ():
        print(f"{job}: {entry.get('result') if isinstance(entry, dict) else entry!r}")
    if errors:
        for error in errors:
            print(f"::error::{argv[1]} gate: {error}")
        return 1
    print(f"{argv[1]} gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
