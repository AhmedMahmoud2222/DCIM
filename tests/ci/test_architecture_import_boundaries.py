"""Ratchet backend import boundaries without changing the ORM design."""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
APP = BACKEND / "app"

DOMAIN_FORBIDDEN_PREFIXES = (
    "fastapi",
    "celery",
    "app.api",
    "app.infrastructure",
    "app.application",
)

# (source path relative to backend/, imported module). These are the exact
# current imports, including duplicate imports from the same module in a file.
API_TO_INFRASTRUCTURE = Counter(
    {
        ("app/api/v1/bulk_import.py", "app.infrastructure.storage"): 1,
        ("app/api/v1/bulk_import.py", "app.infrastructure.tasks.bulk_import"): 1,
        ("app/api/v1/catalog_designer.py", "app.infrastructure.storage"): 1,
        ("app/api/v1/catalog_documents.py", "app.infrastructure.storage"): 1,
        ("app/api/v1/catalog_extraction.py", "app.infrastructure.tasks.catalog_extraction"): 1,
        ("app/api/v1/equipment.py", "app.infrastructure.storage"): 1,
        ("app/api/v1/floor_plans.py", "app.infrastructure.tasks.floorplan_import"): 1,
        ("app/api/v1/power_analytics.py", "app.infrastructure.tasks.power_analytics"): 1,
        ("app/api/v1/racks.py", "app.infrastructure.storage"): 1,
    }
)

APPLICATION_TO_API = Counter(
    {
        ("app/application/rbac.py", "app.api.deps"): 1,
    }
)


def imported_modules(source: str) -> list[str]:
    """Return absolute module names represented by import statements."""
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                modules.append(node.module)
                # `from app import api` is also a dependency on app.api.
                if node.module == "app":
                    modules.extend(f"app.{alias.name}" for alias in node.names)
            else:
                modules.extend(alias.name for alias in node.names)
    return modules


def source_imports(root: Path) -> Counter[tuple[str, str]]:
    imports: Counter[tuple[str, str]] = Counter()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        for module in imported_modules(path.read_text(encoding="utf-8")):
            imports[(relative, module)] += 1
    return imports


def under_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def ratchet_delta(
    observed: Counter[tuple[str, str]], allowed: Counter[tuple[str, str]]
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    return observed - allowed, allowed - observed


class ArchitectureImportBoundaryTests(unittest.TestCase):
    def test_domain_does_not_import_framework_or_outer_layers(self):
        imports = source_imports(APP / "domain")
        violations = sorted(
            (path, module)
            for (path, module), count in imports.items()
            if count
            for prefix in DOMAIN_FORBIDDEN_PREFIXES
            if under_prefix(module, prefix)
        )
        self.assertFalse(
            violations,
            "Domain imports must not depend on FastAPI, Celery, app.api, "
            f"app.infrastructure or app.application: {violations}",
        )

    def test_api_infrastructure_imports_match_reviewed_allowlist(self):
        observed = Counter(
            {
                key: count
                for key, count in source_imports(APP / "api").items()
                if under_prefix(key[1], "app.infrastructure")
            }
        )
        added, removed = ratchet_delta(observed, API_TO_INFRASTRUCTURE)
        self.assertFalse(added or removed, f"Review API -> infrastructure changes: added={added}, removed={removed}")

    def test_application_api_imports_match_reviewed_allowlist(self):
        observed = Counter(
            {
                key: count
                for key, count in source_imports(APP / "application").items()
                if under_prefix(key[1], "app.api")
            }
        )
        added, removed = ratchet_delta(observed, APPLICATION_TO_API)
        self.assertFalse(added or removed, f"Review application -> API changes: added={added}, removed={removed}")

    def test_forbidden_domain_imports_are_detected(self):
        imports = imported_modules(
            "from fastapi import APIRouter\n"
            "from celery import Celery\n"
            "from app.api import router\n"
            "from app.infrastructure import storage\n"
            "from app.application import service\n"
            "from sqlalchemy.orm import Mapped\n"
        )
        violations = [
            module
            for module in imports
            if any(under_prefix(module, prefix) for prefix in DOMAIN_FORBIDDEN_PREFIXES)
        ]
        self.assertEqual(
            violations,
            ["fastapi", "celery", "app.api", "app.infrastructure", "app.application"],
        )
        self.assertNotIn("sqlalchemy.orm", violations)

    def test_ratchet_detects_a_new_infrastructure_import(self):
        observed = API_TO_INFRASTRUCTURE.copy()
        new_import = ("app/api/v1/new_route.py", "app.infrastructure.storage")
        observed[new_import] += 1
        added, removed = ratchet_delta(observed, API_TO_INFRASTRUCTURE)
        self.assertEqual(added, Counter({new_import: 1}))
        self.assertFalse(removed)

    def test_ratchet_detects_a_new_application_to_api_import(self):
        observed = APPLICATION_TO_API.copy()
        new_import = ("app/application/new_service.py", "app.api.deps")
        observed[new_import] += 1
        added, removed = ratchet_delta(observed, APPLICATION_TO_API)
        self.assertEqual(added, Counter({new_import: 1}))
        self.assertFalse(removed)


if __name__ == "__main__":
    unittest.main()
