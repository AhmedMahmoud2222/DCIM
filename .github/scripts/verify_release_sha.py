#!/usr/bin/env python3
"""Fail-closed verification of a release commit against trusted GitHub Actions runs."""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "AhmedMahmoud2222/DCIM"
API = "https://api.github.com/repos/" + REPOSITORY
WORKFLOWS = {
    "ci": (359845224, ".github/workflows/ci.yml", {
        "backend", "backend suite (Python 3.12)", "backend suite (Python 3.13)",
        "backend suite (Python 3.14)", "frontend", "browser-e2e", "edge-collector",
    }),
    "deployment-validation": (367510888, ".github/workflows/deployment-validation.yml", {
        "Compose smoke", "Deployment validation gate",
    }),
}


class VerificationError(Exception):
    pass


def api(path):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise VerificationError("GITHUB_TOKEN is required")
    request = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dcim-release-verifier",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise VerificationError("unexpected API status")
            return json.load(response)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        raise VerificationError("GitHub API request failed: " + path) from exc


def pages(path, key):
    """Fetch every page; reject missing totals, malformed pages and early truncation."""
    page, values, total = 1, [], None
    while True:
        data = api(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        if not isinstance(data, dict) or not isinstance(data.get("total_count"), int) or not isinstance(data.get(key), list):
            raise VerificationError("incomplete paginated response")
        if total is None:
            total = data["total_count"]
        if data["total_count"] != total or len(data[key]) != min(100, total - len(values)):
            raise VerificationError("truncated or changing paginated response")
        values.extend(data[key])
        if len(values) == total:
            return values
        page += 1
        if page > 100:
            raise VerificationError("pagination exceeded limit")


def verify(sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise VerificationError("release SHA must be 40 lowercase hexadecimal characters")
    commit = api("/commits/" + sha)
    if commit.get("sha") != sha:
        raise VerificationError("noncanonical commit")
    compare = api("/compare/" + sha + "...main")
    if compare.get("status") not in ("ahead", "identical") or compare.get("merge_base_commit", {}).get("sha") != sha:
        raise VerificationError("release commit is not an ancestor of authorized main")

    runs = pages("/actions/runs?head_sha=" + sha, "workflow_runs")
    checks = pages("/commits/" + sha + "/check-runs", "check_runs")
    checks_by_id = {check.get("id"): check for check in checks}
    if len(checks_by_id) != len(checks):
        raise VerificationError("duplicate check identifiers")
    evidence = {}
    for label, (workflow_id, path, required) in WORKFLOWS.items():
        candidates = [r for r in runs if r.get("workflow_id") == workflow_id]
        if not candidates:
            raise VerificationError("missing workflow: " + label)
        if any(r.get("path") != path or r.get("head_sha") != sha or
               r.get("repository", {}).get("full_name") != REPOSITORY or
               r.get("head_repository", {}).get("full_name") != REPOSITORY or
               r.get("event") != "push" or r.get("head_branch") != "main" for r in candidates):
            raise VerificationError("untrusted workflow identity: " + label)
        # The newest run is authoritative. Never fall back to an older passing run.
        newest = max(candidates, key=lambda r: (r.get("run_number", 0), r.get("id", 0)))
        if newest.get("status") != "completed" or newest.get("conclusion") != "success":
            raise VerificationError("latest workflow not successful: " + label)
        run_id, attempt, suite = newest.get("id"), newest.get("run_attempt"), newest.get("check_suite_id")
        if not all(isinstance(v, int) and v > 0 for v in (run_id, attempt, suite)):
            raise VerificationError("missing workflow attempt or suite: " + label)
        jobs = pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        names = {}
        for job in jobs:
            name = job.get("name")
            if name not in required:
                continue
            if name in names:
                raise VerificationError("duplicate required job: " + name)
            names[name] = job
            url = job.get("check_run_url", "")
            match = re.fullmatch(re.escape(API) + r"/check-runs/(\d+)", url)
            check = checks_by_id.get(int(match.group(1))) if match else None
            if (job.get("run_id") != run_id or job.get("run_attempt") != attempt or
                job.get("head_sha") != sha or job.get("status") != "completed" or
                job.get("conclusion") != "success" or not check or check.get("name") != name or
                check.get("head_sha") != sha or check.get("status") != "completed" or
                check.get("conclusion") != "success" or check.get("check_suite", {}).get("id") != suite or
                check.get("app", {}).get("slug") != "github-actions"):
                raise VerificationError("untrusted or failed job/check: " + name)
        if names.keys() != required:
            raise VerificationError("missing required jobs: " + ", ".join(sorted(required - names.keys())))
        evidence[label] = {"run_id": run_id, "attempt": attempt, "jobs": sorted(names)}
    return evidence


def main():
    try:
        if len(sys.argv) != 2:
            raise VerificationError("usage: verify_release_sha.py <full-release-sha>")
        evidence = verify(sys.argv[1])
        print(json.dumps({"sha": sys.argv[1], "verified": evidence}, sort_keys=True))
    except (VerificationError, KeyError, TypeError, AttributeError) as exc:
        print("Release verification failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
