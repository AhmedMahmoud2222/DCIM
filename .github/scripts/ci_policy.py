"""CI routing policy: map changed paths to the CI worker jobs that must run.

The policy is a pure function. It reads no git state, no environment and no
network. The router (ci_router.py) loads this file from the PR base commit, so a
PR cannot weaken the policy that classifies that same PR.

Levels:
* fast: no worker job applies (documentation only). The route, ci-policy and
  gate jobs still run.
* component: only the worker jobs of the changed components run.
* full: every worker job runs.

Fail-closed rules:
* An empty path list, an unknown path or a high-risk path selects full.
* A path that matches no component rule selects full.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

POLICY_VERSION = 2

FAST = "fast"
COMPONENT = "component"
FULL = "full"
LEVELS = (FAST, COMPONENT, FULL)

# The worker job ids in .github/workflows/ci.yml. The required checks `backend`
# and `frontend` are gate jobs, not workers: they never appear here.
WORKERS = (
    "backend-suite",
    "frontend-suite",
    "backend-runtime-matrix",
    "edge-collector",
    "edge-interop",
    "browser-e2e",
)

# Any of these selects full. Prefix rules end with "/".
HIGH_RISK_PATHS = (
    ".github/",
    "tests/",
    "scripts/",
    "backend/migrations/",
    "backend/scripts/",
    "backend/app/core/",
    "backend/app/db/",
    "backend/app/domain/auth/",
    "backend/app/api/deps.py",
    "backend/app/api/v1/auth.py",
    "backend/app/application/auth_service.py",
    "backend/app/application/rbac.py",
    "backend/app/application/authority_lock.py",
    "backend/app/application/access_control.py",
    "backend/app/application/access_preflight.py",
    "backend/app/main.py",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    "backend/Dockerfile",
    "frontend/Dockerfile",
    "frontend/nginx.conf",
    "frontend/package.json",
    "frontend/package-lock.json",
    "edge_collector/pyproject.toml",
    "edge_collector/interop/requirements.txt",
    "docker-compose.yml",
    "docker-compose.production.yml",
    ".env.example",
)

# A path segment with one of these words selects full, wherever it is. This
# catches security code that a new file adds outside the paths above.
HIGH_RISK_WORDS = re.compile(
    r"auth|secur|rbac|permission|authority|privileg|crypt|secret|credential|"
    r"password|token|session|sandbox|opencode|preflight|retention|audit",
)

# Component rules. First match wins. Each rule names the workers it selects.
COMPONENT_RULES = (
    # The backend contract tests import the Edge scheduler constants.
    ("edge_collector/", ("edge-collector", "edge-interop", "backend-suite")),
    ("backend/", ("backend-suite", "browser-e2e")),
    ("frontend/", ("frontend-suite", "browser-e2e")),
)

DOC_DIRS = ("docs/",)


def _matches(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def _is_doc(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in DOC_DIRS):
        return True
    return "/" not in path and path.endswith(".md")


def classify(paths: Iterable[str]) -> dict:
    """Return {"level", "jobs", "reasons"} for a list of repository paths."""
    paths = list(paths)
    reasons: list[str] = []
    jobs: set[str] = set()
    level = FAST
    if not paths:
        return full(["no changed paths: change detection gave no evidence"])
    for path in paths:
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
            return full([f"unparseable path: {path!r}"])
        lowered = path.lower()
        if any(_matches(path, rule) for rule in HIGH_RISK_PATHS):
            return full([f"high-risk path: {path}"])
        if HIGH_RISK_WORDS.search(lowered):
            return full([f"high-risk path word: {path}"])
        if _is_doc(path):
            reasons.append(f"documentation: {path}")
            continue
        for prefix, selected in COMPONENT_RULES:
            if path.startswith(prefix):
                jobs.update(selected)
                level = COMPONENT
                reasons.append(f"component {prefix}: {path}")
                break
        else:
            return full([f"unknown path: {path}"])
    return {
        "level": level,
        "jobs": {job: job in jobs for job in WORKERS},
        "reasons": reasons,
    }


def full(reasons: list[str]) -> dict:
    return {"level": FULL, "jobs": {job: True for job in WORKERS}, "reasons": reasons}
