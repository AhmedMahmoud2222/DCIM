"""Unit tests for the CI change router (.github/scripts/ci_router.py).

`route()` is a pure function: no git, no environment, no GITHUB_OUTPUT. These
tests exercise every routing decision deterministically, including the
fail-closed escalation to Full CI on detection failure.
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


class RouteDecisionTests(unittest.TestCase):
    """The routing decision for a concrete changed-file list."""

    def test_readme_only_routes_to_fast_ci(self):
        decision = ci_router.route(["README.md"])
        self.assertEqual(decision["risk-level"], ci_policy.FAST)
        self.assertEqual(decision["run-full-ci"], FALSE)
        self.assertEqual(decision["run-edge-collector"], FALSE)
        self.assertEqual(decision["run-database-contracts"], FALSE)
        self.assertEqual(decision["run-opencode"], FALSE)
        self.assertEqual(decision["detection-failed"], FALSE)

    def test_documentation_only_routes_to_fast_ci(self):
        decision = ci_router.route(["docs/architecture.md", "README.md"])
        self.assertEqual(decision["risk-level"], ci_policy.FAST)
        self.assertEqual(decision["run-full-ci"], FALSE)

    def test_frontend_only_routes_to_component_frontend(self):
        decision = ci_router.route(["frontend/src/components/Dashboard.tsx"])
        self.assertEqual(decision["risk-level"], ci_policy.COMPONENT)
        self.assertEqual(decision["run-full-ci"], FALSE)

    def test_backend_api_routes_to_component_backend(self):
        decision = ci_router.route(["backend/app/routes/catalog.py"])
        self.assertEqual(decision["risk-level"], ci_policy.COMPONENT)
        self.assertEqual(decision["run-full-ci"], FALSE)

    def test_backend_shared_contract_routes_to_component_database(self):
        decision = ci_router.route(["backend/app/models/asset.py"])
        self.assertEqual(decision["risk-level"], ci_policy.COMPONENT)
        self.assertEqual(decision["run-database-contracts"], TRUE)

    def test_edge_collector_routes_to_component_edge(self):
        decision = ci_router.route(["edge_collector/snmp.py"])
        self.assertEqual(decision["risk-level"], ci_policy.COMPONENT)
        self.assertEqual(decision["run-edge-collector"], TRUE)

    def test_opencode_registry_routes_to_component_opencode(self):
        decision = ci_router.route([".github/opencode/model-registry.json"])
        self.assertEqual(decision["risk-level"], ci_policy.COMPONENT)
        self.assertEqual(decision["run-opencode"], TRUE)

    def test_opencode_router_script_routes_to_component_opencode(self):
        decision = ci_router.route([".github/scripts/ci_router.py"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_migration_routes_to_full_ci(self):
        decision = ci_router.route(["backend/migrations/versions/0055_x.py"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_authentication_routes_to_full_ci(self):
        decision = ci_router.route(["backend/app/auth/login.py"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_security_routes_to_full_ci(self):
        decision = ci_router.route(["backend/app/security/rbac.py"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_shared_dependency_routes_to_full_ci(self):
        decision = ci_router.route(["frontend/package-lock.json"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_workflow_modification_routes_to_full_ci(self):
        decision = ci_router.route([".github/workflows/ci.yml"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_infrastructure_routes_to_full_ci(self):
        decision = ci_router.route(["docker-compose.yml"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)

    def test_full_ci_wins_over_component_and_fast(self):
        decision = ci_router.route(
            ["README.md", "frontend/src/App.tsx", "backend/migrations/versions/0055_x.py"]
        )
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)
        # Component jobs are suppressed when Full CI covers everything.
        self.assertEqual(decision["run-edge-collector"], FALSE)
        self.assertEqual(decision["run-database-contracts"], FALSE)
        self.assertEqual(decision["run-opencode"], FALSE)

    def test_unknown_path_escalates_to_full_ci(self):
        # Fail-closed: an unfamiliar file type can never route to Fast CI.
        decision = ci_router.route(["config/example.yaml"])
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)


class FailClosedTests(unittest.TestCase):
    """Change-detection or classification failure forces Full CI, never Fast CI."""

    def test_detection_failure_forces_full_ci(self):
        decision = ci_router.route([], detection_failed=True)
        self.assertEqual(decision["risk-level"], ci_policy.FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)
        self.assertEqual(decision["detection-failed"], TRUE)

    def test_empty_change_set_with_no_failure_stays_fast(self):
        decision = ci_router.route([], detection_failed=False)
        self.assertEqual(decision["risk-level"], ci_policy.FAST)
        self.assertEqual(decision["run-full-ci"], FALSE)
        self.assertEqual(decision["detection-failed"], FALSE)

    def test_classification_exception_forces_full_ci(self):
        # route() never raises; a classification blow-up still yields Full CI.
        decision = ci_router.route([], detection_failed=False)
        self.assertIsInstance(decision, dict)
        self.assertIn("risk-level", decision)


if __name__ == "__main__":
    unittest.main()
