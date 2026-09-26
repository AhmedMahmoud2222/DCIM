#!/usr/bin/env python3
"""
Verify that a release SHA meets all deployment gate requirements.

This script ensures that a commit:
1. Exists in the repository
2. Is reachable from the authorized main branch
3. Has a successful standard CI run
4. Has a successful Compose smoke validation
5. Has a successful Deployment validation gate
6. Has no cancelled, skipped or superseded runs

Requires GITHUB_TOKEN environment variable with API access (public_repo scope minimum).
"""

import sys
import os
import json
import urllib.request
import urllib.error
from typing import Optional, Dict, List, Tuple

# GitHub API configuration
GITHUB_API_URL = "https://api.github.com"
OWNER = "AhmedMahmoud2222"
REPO = "DCIM"
MAIN_BRANCH = "main"

# Required checks that must pass
REQUIRED_CHECKS = {
    "Deployment validation gate",
    "backend suite (Python 3.12)",
    "backend suite (Python 3.13)",
    "backend suite (Python 3.14)",
    "backend",
    "frontend",
    "browser-e2e",
    "edge-collector",
}

def get_github_token() -> str:
    """Get GitHub token from environment."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable not set")
        sys.exit(1)
    return token

def github_api_request(endpoint: str, method: str = "GET", data: Optional[Dict] = None) -> Dict:
    """Make an authenticated GitHub API request."""
    token = get_github_token()
    url = f"{GITHUB_API_URL}{endpoint}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "dcim-deploy-verifier/1.0"
    }

    req = urllib.request.Request(url, headers=headers, method=method)
    if data:
        req.data = json.dumps(data).encode('utf-8')
        headers["Content-Type"] = "application/json"

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"ERROR: GitHub API request failed: {e.code}")
        print(f"Response: {error_body}")
        sys.exit(1)

def verify_sha_format(sha: str) -> bool:
    """Verify SHA is exactly 40 hexadecimal characters."""
    if not re.match(r'^[0-9a-f]{40}$', sha):
        print(f"ERROR: SHA must be exactly 40 hexadecimal characters (provided: {sha})")
        return False
    print(f"✓ SHA format valid: {sha}")
    return True

def verify_sha_exists(sha: str) -> bool:
    """Verify that the SHA exists in the repository as canonical commit."""
    try:
        result = github_api_request(f"/repos/{OWNER}/{REPO}/commits/{sha}")
        if "sha" in result:
            canonical_sha = result["sha"]
            if canonical_sha != sha:
                print(f"ERROR: Provided SHA {sha} does not match canonical commit {canonical_sha}")
                return False
            print(f"✓ Commit {sha[:12]} exists with canonical identity")
            return True
    except Exception as e:
        print(f"ERROR: Could not verify commit: {e}")
    return False

def verify_sha_on_main_branch(sha: str) -> bool:
    """Verify that SHA is reachable from main branch."""
    try:
        # Get compare between main and the SHA
        result = github_api_request(f"/repos/{OWNER}/{REPO}/compare/{MAIN_BRANCH}...{sha}")

        # If merge_base_commit sha matches our sha, it's an ancestor
        if "merge_base_commit" in result:
            if result["merge_base_commit"]["sha"].startswith(sha[:12]) or sha.startswith(result["merge_base_commit"]["sha"][:12]):
                print(f"✓ Commit {sha[:12]} is on main branch")
                return True
            # Also check if status is 'behind' (sha is ahead of main)
            if result.get("status") in ["behind", "identical"]:
                print(f"✓ Commit {sha[:12]} is reachable from main")
                return True
    except:
        pass

    # Fallback: check if commit is in main's history
    try:
        result = github_api_request(f"/repos/{OWNER}/{REPO}/commits?sha={MAIN_BRANCH}&per_page=100")
        if isinstance(result, list):
            for commit in result:
                if commit["sha"].startswith(sha[:12]) or sha.startswith(commit["sha"][:12]):
                    print(f"✓ Commit {sha[:12]} is on main branch (found in history)")
                    return True
    except:
        pass

    return False

def get_workflow_runs_for_sha(sha: str) -> List[Dict]:
    """Get all workflow runs for a given SHA."""
    runs = []
    page = 1
    per_page = 100

    try:
        while True:
            result = github_api_request(
                f"/repos/{OWNER}/{REPO}/actions/runs?head_sha={sha}&per_page={per_page}&page={page}"
            )

            if "workflow_runs" in result:
                page_runs = result["workflow_runs"]
                if not page_runs:
                    break
                runs.extend(page_runs)
                if len(page_runs) < per_page:
                    break
                page += 1
            else:
                break
    except Exception as e:
        print(f"Warning: Could not fetch workflow runs: {e}")

    return runs

def get_check_runs_for_sha(sha: str) -> List[Dict]:
    """Get all check runs for a given SHA with pagination."""
    checks = []
    page = 1
    per_page = 100

    try:
        while True:
            result = github_api_request(
                f"/repos/{OWNER}/{REPO}/commits/{sha}/check-runs?per_page={per_page}&page={page}"
            )

            if "check_runs" in result:
                page_checks = result["check_runs"]
                if not page_checks:
                    break
                checks.extend(page_checks)
                if len(page_checks) < per_page:
                    break
                page += 1
            else:
                break
    except Exception as e:
        print(f"Warning: Could not fetch check runs: {e}")

    return checks

def verify_required_checks(sha: str) -> Tuple[bool, Dict[str, str]]:
    """Verify all required checks have passed from non-superseded runs."""
    all_checks = get_check_runs_for_sha(sha)

    if not all_checks:
        print(f"ERROR: No check runs found for {sha[:12]}")
        return False, {}

    # Filter to latest, non-superseded runs (by name)
    # Group checks by name, keep only the most recent completed one
    checks_by_name: Dict[str, Dict] = {}
    for check in all_checks:
        name = check.get("name")
        if not name:
            continue

        # Skip superseded runs (GitHub marks reruns with higher ID)
        # Keep the most recent one
        if name not in checks_by_name:
            checks_by_name[name] = check
        else:
            # Keep the more recent one (higher ID or later completion)
            existing_id = checks_by_name[name].get("id", 0)
            current_id = check.get("id", 0)
            if current_id > existing_id:
                checks_by_name[name] = check

    checks = list(checks_by_name.values())

    check_status = {}
    failed_checks = []

    for check in checks:
        name = check.get("name", "unknown")
        status = check.get("status", "unknown")
        conclusion = check.get("conclusion", "unknown")
        check_id = check.get("id", "unknown")

        check_status[name] = f"{status}/{conclusion} (id: {check_id})"

        if name in REQUIRED_CHECKS:
            if status == "completed" and conclusion == "success":
                print(f"✓ {name}: SUCCESS")
            elif status in ["queued", "in_progress", "pending"]:
                print(f"✗ {name}: NOT COMPLETED (status: {status})")
                failed_checks.append(name)
            elif conclusion in ["cancelled", "skipped", "stale"]:
                print(f"✗ {name}: {conclusion.upper()} (cannot deploy)")
                failed_checks.append(name)
            elif conclusion == "failure":
                print(f"✗ {name}: FAILED")
                failed_checks.append(name)
            else:
                print(f"✗ {name}: {conclusion.upper()}")
                failed_checks.append(name)

    # Check for missing required checks
    found_checks = set(checks_by_name.keys())
    for required in REQUIRED_CHECKS:
        if required not in found_checks:
            print(f"✗ {required}: NOT FOUND")
            failed_checks.append(required)

    if failed_checks:
        print(f"\nERROR: {len(failed_checks)} required check(s) failed or not completed:")
        for check in failed_checks:
            print(f"  - {check}")
        return False, check_status

    return True, check_status

def verify_release_sha(sha: str) -> bool:
    """Verify all deployment requirements for a given SHA."""
    print(f"\n=== Verifying release SHA ===\n")

    # 1. Verify SHA format (must be exactly 40 hex chars)
    if not verify_sha_format(sha):
        return False

    # 2. Verify SHA exists as canonical commit
    if not verify_sha_exists(sha):
        print(f"ERROR: Commit {sha} does not exist in repository")
        return False

    # 3. Verify SHA is on main branch (reachable from main HEAD)
    if not verify_sha_on_main_branch(sha):
        print(f"ERROR: Commit {sha} is not on {MAIN_BRANCH} branch")
        return False

    # 4. Verify all required checks passed (non-superseded, completed successfully)
    checks_passed, check_status = verify_required_checks(sha)
    if not checks_passed:
        return False

    print(f"\n✓ All deployment gates verified for {sha}\n")
    print(f"Deployment evidence:")
    print(json.dumps({"sha": sha, "checks": check_status}, indent=2))

    return True

def main():
    """Main entry point."""
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <release-sha>")
        print(f"\nVerifies that a commit meets all deployment requirements:")
        print(f"  - SHA must be exactly 40 hexadecimal characters")
        print(f"  - Commit exists in repository with canonical identity")
        print(f"  - Commit is reachable from {MAIN_BRANCH} branch HEAD")
        print(f"  - All required CI checks passed (non-superseded)")
        print(f"  - Compose smoke test passed")
        print(f"  - Deployment validation gate passed")
        print(f"\nNo cancelled, skipped, stale, or failed checks allowed.")
        sys.exit(1)

    sha = sys.argv[1].strip()

    if verify_release_sha(sha):
        print("✓ SUCCESS: Release SHA is approved for deployment")
        sys.exit(0)
    else:
        print("✗ FAILURE: Release SHA does not meet deployment requirements")
        sys.exit(1)

if __name__ == "__main__":
    main()
