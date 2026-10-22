"""Deterministic CI risk-classification policy (policy v1.0).

Maps every repository path to a CI validation level and the affected component(s).
Classification is static: no LLM, no network, no secrets. Unknown paths always
escalate to Full CI (fail-closed).

Three validation levels:

* Fast CI — every PR. Change detection, risk classification, linting, security
  baseline and migration integrity.
* Component CI — affected components only: backend, frontend, edge collector,
  database/shared contracts, OpenCode.
* Full CI — high-risk changes. Complete Python compatibility matrix,
  PostgreSQL/Redis integration, Docker Compose, Playwright E2E, deployment
  tooling regressions, relevant security validation.

Full CI is mandatory for security, authentication, migrations, infrastructure,
workflow configuration and shared dependencies.

The required status checks `backend` and `frontend` are always run and always
validate meaningfully; this policy never gates them off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, List, Tuple

# ---------------------------------------------------------------------------
# Validation levels (ordered by severity). Full > component > fast.
# ---------------------------------------------------------------------------

FAST: Final = "fast"
COMPONENT: Final = "component"
FULL: Final = "full"

LEVEL_SEVERITY = {FAST: 0, COMPONENT: 1, FULL: 2}


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

BACKEND: Final = "backend"
FRONTEND: Final = "frontend"
EDGE_COLLECTOR: Final = "edge-collector"
DATABASE_CONTRACTS: Final = "database-contracts"
OPENCODE: Final = "opencode"

COMPONENT_NAMES: Final = (
    BACKEND,
    FRONTEND,
    EDGE_COLLECTOR,
    DATABASE_CONTRACTS,
    OPENCODE,
)

# Required status checks. Preserved exactly; never weakened or removed.
REQUIRED_CHECKS: Final = (BACKEND, FRONTEND)


# ---------------------------------------------------------------------------
# Path-pattern -> (level, component) mapping.
#
# First match wins, so high-risk (Full CI) patterns must be listed before the
# broader component patterns that contain them (e.g. backend/migrations/ before
# backend/). An unknown path (no pattern match) escalates to Full CI.
# ---------------------------------------------------------------------------

PathRule = Tuple[str, str, str | None]  # (path_prefix, level, component_or_None)

_PATH_RULES: Final[Tuple[PathRule, ...]] = (
    # --- Full CI: workflow configuration -----------------------------------
    # Changing the CI itself must re-validate everything.
    (".github/workflows/", FULL, None),
    (".github/scripts/ci_policy.py", FULL, None),
    (".github/scripts/ci_router.py", FULL, None),
    # --- Full CI: migrations -----------------------------------------------
    # Schema changes run the full PostgreSQL integration path.
    ("backend/migrations/", FULL, None),
    # --- Full CI: authentication / security --------------------------------
    ("backend/app/auth/", FULL, None),
    ("backend/app/security/", FULL, None),
    # --- Full CI: infrastructure -------------------------------------------
    ("docker-compose.yml", FULL, None),
    ("docker-compose.production.yml", FULL, None),
    ("docker-compose.override.yml", FULL, None),
    ("backend/Dockerfile", FULL, None),
    ("frontend/Dockerfile", FULL, None),
    ("scripts/deploy-docker-compose.sh", FULL, None),
    ("tests/deployment/", FULL, None),
    # --- Full CI: shared dependencies --------------------------------------
    ("backend/pyproject.toml", FULL, None),
    ("frontend/package.json", FULL, None),
    ("frontend/package-lock.json", FULL, None),
    # --- Component CI: database / shared contracts (specific, before backend/)
    ("backend/app/models/", COMPONENT, DATABASE_CONTRACTS),
    ("backend/app/core/", COMPONENT, DATABASE_CONTRACTS),
    ("backend/app/infrastructure/", COMPONENT, DATABASE_CONTRACTS),
    # --- Component CI: backend ---------------------------------------------
    ("backend/", COMPONENT, BACKEND),
    ("backend/tests/", COMPONENT, BACKEND),
    # --- Component CI: frontend --------------------------------------------
    ("frontend/", COMPONENT, FRONTEND),
    # --- Component CI: edge collector --------------------------------------
    ("edge_collector/", COMPONENT, EDGE_COLLECTOR),
    # --- Component CI: OpenCode agent infrastructure -----------------------
    (".github/opencode/", COMPONENT, OPENCODE),
    (".github/scripts/opencode_", COMPONENT, OPENCODE),
    # --- Fast CI: documentation only ---------------------------------------
    ("README.md", FAST, None),
    ("docs/", FAST, None),
    (".md", FAST, None),
)


@dataclass(frozen=True)
class Classification:
    level: str
    component: str | None


def classify_path(path: str) -> Classification:
    """Classify a single changed path.

    Unknown paths (no matching rule) escalate to Full CI with no component,
    so a change-detection failure or an unfamiliar file type can never slip
    through as Fast CI.
    """
    # Normalise separators so the rules work on Windows and Linux alike.
    normalised = path.replace("\\", "/")
    # Strip a leading ./ or / so relative and absolute diff output agree.
    while normalised.startswith("./") or normalised.startswith("/"):
        normalised = normalised[2:] if normalised.startswith("./") else normalised[1:]
    for prefix, level, component in _PATH_RULES:
        if normalised == prefix or normalised.startswith(prefix):
            return Classification(level=level, component=component)
    # Fail-closed: anything unclassified is treated as Full CI.
    return Classification(level=FULL, component=None)


def affected_components(paths: Iterable[str]) -> List[str]:
    """Return the sorted set of components touched by `paths` (component-level
    classifications only; Full CI paths do not add a component because Full CI
    is comprehensive)."""
    found: set[str] = set()
    for path in paths:
        classification = classify_path(path)
        if classification.level == COMPONENT and classification.component:
            found.add(classification.component)
    return sorted(found)


def any_full_risk(paths: Iterable[str]) -> bool:
    """True if any changed path requires Full CI."""
    return any(classify_path(path).level == FULL for path in paths)


def component_level(paths: Iterable[str]) -> str:
    """Overall level for `paths`: Full if any path is Full, component if any
    path is component-level, fast otherwise. Unknown paths count as Full."""
    levels = {classify_path(path).level for path in paths} or {FAST}
    return max(levels, key=LEVEL_SEVERITY.__getitem__)


def is_required_check(component: str | None) -> bool:
    """True if `component` is one of the preserved required status checks."""
    return component in REQUIRED_CHECKS


def all_components() -> Tuple[str, ...]:
    return COMPONENT_NAMES


def required_checks() -> Tuple[str, ...]:
    return REQUIRED_CHECKS


__all__ = [
    "FAST",
    "COMPONENT",
    "FULL",
    "BACKEND",
    "FRONTEND",
    "EDGE_COLLECTOR",
    "DATABASE_CONTRACTS",
    "OPENCODE",
    "REQUIRED_CHECKS",
    "Classification",
    "classify_path",
    "affected_components",
    "any_full_risk",
    "component_level",
    "is_required_check",
    "all_components",
    "required_checks",
]
