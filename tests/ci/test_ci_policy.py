"""Unit tests for the CI risk-classification policy (.github/scripts/ci_policy.py).

All classification is static and deterministic: no network, no LLM, no git.
Unknown paths escalate to Full CI (fail-closed).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import ci_policy  # noqa: E402


class RequiredChecksTests(unittest.TestCase):
    """The preserved required status checks must always be backend + frontend."""

    def test_required_checks_are_backend_and_frontend(self):
        self.assertEqual(ci_policy.required_checks(), ("backend", "frontend"))

    def test_required_checks_are_components(self):
        for check in ci_policy.required_checks():
            self.assertIn(check, ci_policy.all_components())


class ClassifyPathTests(unittest.TestCase):
    """Every path maps to a level; unknown paths escalate to Full CI."""

    def test_readme_only_is_fast(self):
        self.assertEqual(ci_policy.classify_path("README.md").level, ci_policy.FAST)

    def test_documentation_is_fast(self):
        self.assertEqual(ci_policy.classify_path("docs/architecture.md").level, ci_policy.FAST)
        self.assertEqual(ci_policy.classify_path("docs/ci/ROLLBACK.md").level, ci_policy.FAST)

    def test_frontend_source_is_component_frontend(self):
        c = ci_policy.classify_path("frontend/src/components/Dashboard.tsx")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.FRONTEND)

    def test_backend_api_is_component_backend(self):
        c = ci_policy.classify_path("backend/app/routes/catalog.py")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.BACKEND)

    def test_backend_tests_are_component_backend(self):
        c = ci_policy.classify_path("backend/tests/api/test_catalog.py")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.BACKEND)

    def test_edge_collector_is_component_edge_collector(self):
        c = ci_policy.classify_path("edge_collector/snmp.py")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.EDGE_COLLECTOR)

    def test_opencode_router_is_component_opencode(self):
        c = ci_policy.classify_path(".github/opencode/router.py")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.OPENCODE)

    def test_opencode_script_is_component_opencode(self):
        c = ci_policy.classify_path(".github/scripts/opencode_guard.py")
        self.assertEqual(c.level, ci_policy.COMPONENT)
        self.assertEqual(c.component, ci_policy.OPENCODE)

    def test_database_shared_contract_is_component_database_contracts(self):
        for path in (
            "backend/app/models/asset.py",
            "backend/app/core/config.py",
            "backend/app/infrastructure/db.py",
        ):
            c = ci_policy.classify_path(path)
            self.assertEqual(c.level, ci_policy.COMPONENT, path)
            self.assertEqual(c.component, ci_policy.DATABASE_CONTRACTS, path)

    def test_backend_migration_is_full(self):
        for path in (
            "backend/migrations/versions/0055_x.py",
            "backend/migrations/versions/0056_y.py",
        ):
            c = ci_policy.classify_path(path)
            self.assertEqual(c.level, ci_policy.FULL, path)

    def test_authentication_is_full(self):
        c = ci_policy.classify_path("backend/app/auth/login.py")
        self.assertEqual(c.level, ci_policy.FULL)

    def test_security_is_full(self):
        c = ci_policy.classify_path("backend/app/security/rbac.py")
        self.assertEqual(c.level, ci_policy.FULL)

    def test_shared_dependency_is_full(self):
        for path in (
            "backend/pyproject.toml",
            "frontend/package.json",
            "frontend/package-lock.json",
        ):
            self.assertEqual(ci_policy.classify_path(path).level, ci_policy.FULL, path)

    def test_workflow_modification_is_full(self):
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/deploy.yml",
        ):
            self.assertEqual(ci_policy.classify_path(path).level, ci_policy.FULL, path)

    def test_infrastructure_is_full(self):
        for path in (
            "docker-compose.yml",
            "docker-compose.production.yml",
            "docker-compose.override.yml",
            "backend/Dockerfile",
            "frontend/Dockerfile",
            "scripts/deploy-docker-compose.sh",
        ):
            self.assertEqual(ci_policy.classify_path(path).level, ci_policy.FULL, path)

    def test_deployment_tooling_is_full(self):
        self.assertEqual(ci_policy.classify_path("tests/deployment/test_x.py").level, ci_policy.FULL)

    def test_ci_policy_and_router_are_full(self):
        # Changing the classifier itself must re-validate everything.
        for path in (".github/scripts/ci_policy.py", ".github/scripts/ci_router.py"):
            self.assertEqual(ci_policy.classify_path(path).level, ci_policy.FULL, path)

    def test_unknown_path_escalates_to_full(self):
        # Fail-closed: an unfamiliar file type can never route to Fast CI.
        for path in (
            "random/newfile.xyz",
            "src/unknown/module.txt",
            "config/example.yaml",
        ):
            c = ci_policy.classify_path(path)
            self.assertEqual(c.level, ci_policy.FULL, path)
            self.assertIsNone(c.component)


class ComponentLevelTests(unittest.TestCase):
    """Overall level for a change set: Full > component > fast."""

    def test_empty_change_set_is_fast(self):
        self.assertEqual(ci_policy.component_level([]), ci_policy.FAST)

    def test_multiple_fast_paths_stay_fast(self):
        self.assertEqual(
            ci_policy.component_level(["README.md", "docs/a.md", "docs/b.md"]),
            ci_policy.FAST,
        )

    def test_component_only_change_set_is_component(self):
        self.assertEqual(
            ci_policy.component_level(["frontend/src/App.tsx", "edge_collector/snmp.py"]),
            ci_policy.COMPONENT,
        )

    def test_any_full_path_makes_the_set_full(self):
        self.assertEqual(
            ci_policy.component_level(["README.md", "backend/migrations/versions/0055_x.py"]),
            ci_policy.FULL,
        )
        self.assertEqual(
            ci_policy.component_level(["docs/x.md", "random/newfile.xyz"]),
            ci_policy.FULL,
        )


class AffectedComponentsTests(unittest.TestCase):
    """Component-level paths contribute their component; Full CI paths do not."""

    def test_single_component(self):
        self.assertEqual(
            ci_policy.affected_components(["frontend/src/App.tsx"]),
            [ci_policy.FRONTEND],
        )

    def test_multiple_components_sorted(self):
        self.assertEqual(
            ci_policy.affected_components(
                ["edge_collector/snmp.py", "frontend/src/App.tsx"]
            ),
            [ci_policy.EDGE_COLLECTOR, ci_policy.FRONTEND],
        )

    def test_full_ci_paths_add_no_component(self):
        # Full CI is comprehensive; its paths do not add a component label.
        self.assertEqual(
            ci_policy.affected_components(
                ["backend/migrations/versions/0055_x.py", ".github/workflows/ci.yml"]
            ),
            [],
        )

    def test_unknown_paths_add_no_component(self):
        self.assertEqual(ci_policy.affected_components(["random/newfile.xyz"]), [])


if __name__ == "__main__":
    unittest.main()
