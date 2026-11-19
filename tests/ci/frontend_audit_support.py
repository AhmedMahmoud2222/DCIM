"""Shared fixtures, builders and the fake npm for the frontend audit gate tests.

The configurations built here are TEST FIXTURES. They are not an owner approval.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from ci_support import ROOT, SCRIPTS, load_module

gate = load_module("frontend_audit_gate", SCRIPTS / "frontend_audit_gate.py")

FIXTURES = ROOT / "tests" / "ci" / "fixtures" / "frontend_audit_gate"
SHIPPED_CONFIG = ROOT / ".github" / "security-exceptions" / "frontend-audit-picomatch.json"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
EXC = "GHSA-fc5q-39xc-vjpf"
OTHER_HIGH = "GHSA-2222-3333-4444"
OTHER_CRITICAL = "GHSA-5555-6666-7777"
OTHER_MODERATE = "GHSA-8888-9999-cccc"

GOOD_CONFIG = {
    "schema_version": 1,
    "advisory": EXC,
    "approved": True,
    "approved_by": "test-fixture",
    "approval_reference": "https://github.com/AhmedMahmoud2222/DCIM/issues/364#issuecomment-1",
    "approved_on": "2026-11-18",
    "expires_utc": "2026-12-18T00:00:00Z",
}
GOOD_ADVISORY = {
    "ghsa_id": EXC,
    "withdrawn_at": None,
    "vulnerabilities": [
        {"package": {"name": "picomatch", "ecosystem": "npm"}, "vulnerable_version_range": "<= 4.0.7", "first_patched_version": None}
    ],
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def advisory(ghsa: str, package: str, severity: str, rng: str = "<=1.0.0") -> dict:
    return {
        "source": 1,
        "name": package,
        "dependency": package,
        "title": "t",
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": severity,
        "cwe": [],
        "cvss": {"score": 0, "vectorString": None},
        "range": rng,
    }


def node(name: str, severity: str, via: list) -> dict:
    return {"name": name, "severity": severity, "isDirect": False, "via": via, "effects": [], "range": "*", "nodes": [f"node_modules/{name}"], "fixAvailable": False}


def report(*nodes: dict, **overrides) -> str:
    counts = {s: 0 for s in ("info", "low", "moderate", "high", "critical")}
    for n in nodes:
        counts[n["severity"]] += 1
    counts["total"] = len(nodes)
    doc = {
        "auditReportVersion": 2,
        "vulnerabilities": {n["name"]: n for n in nodes},
        "metadata": {"vulnerabilities": counts, "dependencies": {"total": 1}},
    }
    doc.update(overrides)
    return json.dumps(doc)


EXC_ADV = advisory(EXC, "picomatch", "high", "<=4.0.7")
EXC_ONLY = report(node("picomatch", "high", [EXC_ADV]), node("vite", "high", ["picomatch"]))
CLEAN = fixture("pr366_prod_audit.json")  # a real, valid report with no vulnerabilities


class FakeNpm:
    """Stands in for npm: records calls and returns canned results."""

    def __init__(self, dev=None, prod=None, versions=None, dev_code=1, prod_code=0, view_code=0):
        self.dev = fixture("pr366_dev_audit.json") if dev is None else dev
        self.prod = fixture("pr366_prod_audit.json") if prod is None else prod
        self.versions = fixture("picomatch_versions.json") if versions is None else versions
        self.dev_code, self.prod_code, self.view_code = dev_code, prod_code, view_code
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        if list(args[:3]) == ["npm", "view", "picomatch"]:
            return gate.CommandResult(self.view_code, self.versions)
        if "--omit=dev" in args:
            return gate.CommandResult(self.prod_code, self.prod)
        return gate.CommandResult(self.dev_code, self.dev)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.frontend = self.tmp / "frontend"
        self.write_tree({"src/App.tsx": "export const a = 1;\n", "e2e/smoke.spec.ts": "export {};\n"})
        self.config = self.tmp / "cfg.json"

    def write_tree(self, files: dict[str, str | bytes]) -> None:
        for rel, content in files.items():
            path = self.frontend / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")

    def write_config(self, **changes) -> None:
        doc = dict(GOOD_CONFIG)
        doc.update(changes)
        for key in [k for k, v in changes.items() if v is gate]:  # sentinel: delete the key
            del doc[key]
        self.config.write_text(json.dumps(doc), encoding="utf-8")

    def run_gate(self, mode="exception", npm=None, advisory_doc=None, now=NOW, config=True):
        if config is True:
            self.write_config()
        out: list[str] = []
        if advisory_doc is None:
            advisory_doc = GOOD_ADVISORY
        fetcher = advisory_doc if callable(advisory_doc) else (lambda: advisory_doc)
        code = gate.run_gate(mode, self.frontend, self.config, now, npm or FakeNpm(), fetcher, out.append)
        return code, "\n".join(out)

    def assertFailed(self, result, *needles):
        code, output = result
        self.assertEqual(code, 1, output)
        for needle in needles:
            self.assertIn(needle, output)

    def assertPassed(self, result):
        self.assertEqual(result[0], 0, result[1])
