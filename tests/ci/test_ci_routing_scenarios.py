"""Regression tests for each required CI routing scenario.

Each scenario feeds a representative changed-file list through the router and
asserts the expected CI validation level and affected components. The policy
and router are pure functions, so these run without git or network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import ci_policy  # noqa: E402
import ci_router  # noqa: E402

TRUE = ci_router.TRUE
FALSE = ci_router.FALSE

FAST = ci_policy.FAST
COMPONENT = ci_policy.COMPONENT
FULL = ci_policy.FULL

BACKEND = ci_policy.BACKEND
FRONTEND = ci_policy.FRONTEND
EDGE_COLLECTOR = ci_policy.EDGE_COLLECTOR
DATABASE_CONTRACTS = ci_policy.DATABASE_CONTRACTS
OPENCODE = ci_policy.OPENCODE

# scenario_name -> (changed_files, expected_level, expected_full_bool,
#                  expected_components)
SCENARIOS = [
    # 1. README-only -> Fast CI only.
    (
        "readme-only",
        ["README.md"],
        FAST,
        False,
        [],
    ),
    # 2. OpenCode registry/router -> changing the classifier itself is Full CI.
    (
        "opencode-registry-router",
        [".github/opencode/model-registry.json", ".github/scripts/ci_router.py"],
        FULL,
        True,
        [],
    ),
    # OpenCode catalog/runtime only (not the router/policy) -> Component CI.
    (
        "opencode-catalog-only",
        [".github/opencode/runtime/package.json"],
        COMPONENT,
        False,
        [OPENCODE],
    ),
    # 3. Backend API -> Component CI (backend).
    (
        "backend-api",
        ["backend/app/routes/catalog.py", "backend/tests/api/test_catalog.py"],
        COMPONENT,
        False,
        [BACKEND],
    ),
    # 4. Frontend-only -> Component CI (frontend). package.json alone is Full CI.
    (
        "frontend-only",
        ["frontend/src/components/Dashboard.tsx", "frontend/package.json"],
        FULL,
        True,
        [],
    ),
    (
        "frontend-source-only",
        ["frontend/src/components/Dashboard.tsx"],
        COMPONENT,
        False,
        [FRONTEND],
    ),
    # 5. Migration -> Full CI.
    (
        "migration",
        ["backend/migrations/versions/0055_x.py"],
        FULL,
        True,
        [],
    ),
    # 6. Authentication / security -> Full CI.
    (
        "authentication",
        ["backend/app/auth/login.py"],
        FULL,
        True,
        [],
    ),
    (
        "security",
        ["backend/app/security/rbac.py"],
        FULL,
        True,
        [],
    ),
    # 7. Shared dependency -> Full CI.
    (
        "shared-dependency",
        ["backend/pyproject.toml"],
        FULL,
        True,
        [],
    ),
    # 8. Workflow modification -> Full CI.
    (
        "workflow-modification",
        [".github/workflows/ci.yml"],
        FULL,
        True,
        [],
    ),
    # Infrastructure -> Full CI.
    (
        "infrastructure",
        ["docker-compose.yml", "backend/Dockerfile"],
        FULL,
        True,
        [],
    ),
    # Edge collector affected -> Component CI (edge).
    (
        "edge-collector",
        ["edge_collector/snmp.py"],
        COMPONENT,
        False,
        [EDGE_COLLECTOR],
    ),
    # Database shared contract -> Component CI (database).
    (
        "database-contract",
        ["backend/app/models/asset.py"],
        COMPONENT,
        False,
        [DATABASE_CONTRACTS],
    ),
    # Mixed fast + component stays component.
    (
        "fast-plus-component",
        ["README.md", "frontend/src/components/Dashboard.tsx"],
        COMPONENT,
        False,
        [FRONTEND],
    ),
]


class RoutingScenarioTests(unittest.TestCase):
    def test_every_documented_scenario_routes_as_expected(self):
        for name, files, expected_level, expected_full, expected_components in SCENARIOS:
            with self.subTest(scenario=name):
                decision = ci_router.route(files)
                self.assertEqual(
                    decision["risk-level"],
                    expected_level,
                    f"scenario {name}: {files}",
                )
                self.assertEqual(
                    decision["run-full-ci"],
                    TRUE if expected_full else FALSE,
                    f"scenario {name}: {files}",
                )
                self.assertEqual(
                    sorted(decision["files"]), sorted(files), f"scenario {name}"
                )

    def test_full_ci_scenarios_suppress_component_jobs(self):
        # When Full CI runs, component jobs are not separately triggered (Full CI
        # is comprehensive). This avoids redundant validation.
        for name, files, expected_level, expected_full, expected_components in SCENARIOS:
            if not expected_full:
                continue
            with self.subTest(scenario=name):
                decision = ci_router.route(files)
                self.assertEqual(decision["run-edge-collector"], FALSE)
                self.assertEqual(decision["run-database-contracts"], FALSE)
                self.assertEqual(decision["run-opencode"], FALSE)

    def test_component_scenarios_enable_their_component_job(self):
        for name, files, expected_level, expected_full, expected_components in SCENARIOS:
            if expected_full:
                continue
            with self.subTest(scenario=name):
                decision = ci_router.route(files)
                if not expected_components:
                    continue
                component_jobs = {
                    ci_policy.EDGE_COLLECTOR: decision["run-edge-collector"],
                    ci_policy.DATABASE_CONTRACTS: decision["run-database-contracts"],
                    ci_policy.OPENCODE: decision["run-opencode"],
                }
                for component in expected_components:
                    if component not in component_jobs:
                        continue  # required check: always runs, no gated output
                    self.assertEqual(
                        component_jobs[component],
                        TRUE,
                        f"scenario {name} should enable {component}",
                    )

    def test_detection_failure_escalates_to_full_ci(self):
        # 9. Change-detection failure -> Full CI (escalation, never Fast CI).
        decision = ci_router.route([], detection_failed=True)
        self.assertEqual(decision["risk-level"], FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)
        self.assertEqual(decision["detection-failed"], TRUE)

    def test_unknown_path_always_escalates(self):
        # An unknown file type cannot hide behind Fast CI.
        decision = ci_router.route(["config/unknown.extension"])
        self.assertEqual(decision["risk-level"], FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)


if __name__ == "__main__":
    unittest.main()
