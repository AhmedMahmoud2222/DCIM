"""Mutation tests: each unsafe change to the CI policy, router, gate or workflow
must make the behavioral checks fail.

A surviving mutant means the checks can no longer see that defect. The workflow
mutants include the three mutations from the PR #191 review (P2) that the
earlier test suite did not detect.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from ci_support import (
    SCRIPTS,
    gate_mismatches,
    load_module,
    load_workflow,
    policy_mismatches,
    router_mismatches,
    workflow_errors,
)

GATE = load_module("ci_gate", SCRIPTS / "ci_gate.py")

POLICY_MUTANTS = (
    ('r"auth|secur|', 'r"secur|'),
    ('return full([f"unknown path: {path}"])', "continue"),
    ('return full(["no changed paths: change detection gave no evidence"])',
     'return {"level": FAST, "jobs": {job: False for job in WORKERS}, "reasons": []}'),
    ('("backend/", ("backend-suite", "browser-e2e")),', '("backend/", ("browser-e2e",)),'),
    ('("frontend/", ("frontend-suite", "browser-e2e")),', '("frontend/", ("frontend-suite",)),'),
    ('("edge_collector/", ("edge-collector", "edge-interop", "backend-suite")),',
     '("edge_collector/", ("edge-collector", "edge-interop")),'),
    ('"backend/app/core/",', ""),
    ('"backend/migrations/",', ""),
    ('"backend/pyproject.toml",', ""),
    ('return "/" not in path and path.endswith(".md")', 'return path.endswith(".md")'),
    ('if any(_matches(path, rule) for rule in HIGH_RISK_PATHS):', "if False:"),
    ('"jobs": {job: True for job in WORKERS}', '"jobs": {job: False for job in WORKERS}'),
)

ROUTER_MUTANTS = (
    ('"--no-renames", ', ""),
    ('merge_base = _git("merge-base", base, head).strip()', "merge_base = base"),
    ('decision = ci_policy.full([f"change detection failed: {error}"])',
     'decision = ci_policy.classify(["README.md"])'),
    ('route["degraded"] = True', 'route["degraded"] = False'),
    ('if event != "pull_request":', 'if event not in ("pull_request", "push", "workflow_dispatch"):'),
    ('SHA = re.compile(r"[0-9a-f]{40}")', 'SHA = re.compile(r"[0-9a-fA-F]{4,40}")'),
    ('values[f"run-{job}"] = "true" if route["jobs"][job] else "false"', 'values[f"run-{job}"] = "true"'),
)

GATE_MUTANTS = (
    ('if applicable and status != "success":', 'if applicable and status == "failure":'),
    ('status not in ("skipped", "success")', 'status not in ("skipped", "success", "cancelled")'),
    ('applicable = level == "full" or job not in jobs or jobs[job]',
     'applicable = level == "full" or jobs.get(job, False)'),
    ('if result(job) != "success":', 'if result(job) == "failure":'),
    ('if route.get("head") != expected_head or outputs.get("head-sha") != expected_head:', "if False:"),
    ('errors.append(f"{job}: missing from needs")', "pass"),
    ('errors.append(f"{job}: in needs but not in the {gate} gate scope")', "pass"),
    ('if level == "full" and not all(jobs.values()):', "if False:"),
    ('if level == "fast" and any(jobs.values()):', "if False:"),
    ('if level == "component" and not any(jobs.values()):', "if False:"),
    ('if job in jobs and flag != ("true" if jobs[job] else "false"):', "if False:"),
    ("if any(not isinstance(value, bool) for value in jobs.values()):", "if False:"),
    ('if not SHA.fullmatch(expected_head or ""):', "if False:"),
    ('if level not in LEVELS or outputs.get("level") != level:', 'if outputs.get("level") != level:'),
    ('"frontend": CONTROL_JOBS + ("frontend-suite", "browser-e2e"),',
     '"frontend": CONTROL_JOBS + ("frontend-suite",),'),
    ('if job in CONTROL_JOBS:\n            continue', "if job in CONTROL_JOBS or job == 'browser-e2e':\n            continue"),
)


def _job(doc, job_id):
    return doc["jobs"][job_id]


def _step(doc, job_id, needle):
    return next(s for s in _job(doc, job_id)["steps"] if needle in str(s.get("run", "")) + str(s.get("name", "")))


def _set_run(job_id, needle, run):
    return lambda doc: _step(doc, job_id, needle).__setitem__("run", run)


WORKFLOW_MUTANTS = {
    # The three review P2 mutations.
    "backend display name changed": lambda doc: _job(doc, "backend").__setitem__("name", "arbitrary-success"),
    "backend display name capitalized": lambda doc: _job(doc, "backend").__setitem__("name", "Backend"),
    "worker condition disabled with false &&": lambda doc: _job(doc, "backend-runtime-matrix").__setitem__(
        "if", "false && " + _job(doc, "backend-runtime-matrix")["if"]),
    "regression pytest replaced with echo": _set_run(
        "backend-suite", "--ignore=tests/integration/test_mvp_retention_hostile.py", "echo removed"),
    # Gate wiring.
    "gate if success()": lambda doc: _job(doc, "backend").__setitem__("if", "success()"),
    "gate if false && always()": lambda doc: _job(doc, "frontend").__setitem__("if", "false && always()"),
    "gate drops a need": lambda doc: _job(doc, "backend")["needs"].remove("browser-e2e"),
    "frontend gate drops e2e": lambda doc: _job(doc, "frontend")["needs"].remove("browser-e2e"),
    "gate step replaced with true": _set_run("backend", "ci_gate.py", "true"),
    "gate step continue-on-error": lambda doc: _step(doc, "frontend", "ci_gate.py").__setitem__(
        "continue-on-error", True),
    "gate conditional step": lambda doc: _step(doc, "backend", "ci_gate.py").__setitem__("if", "success()"),
    "gate head expression": lambda doc: _step(doc, "backend", "ci_gate.py")["env"].__setitem__(
        "EXPECTED_HEAD_SHA", "${{ github.sha }}"),
    "gate job continue-on-error": lambda doc: _job(doc, "backend").__setitem__("continue-on-error", True),
    "worker emits required name": lambda doc: _job(doc, "frontend-suite").__setitem__("name", "frontend"),
    # Routing.
    "route reads the PR head policy": _set_run(
        "route", "trusted-ci-policy", 'cp .github/scripts/ci_*.py "$RUNNER_TEMP/" && python3 "$RUNNER_TEMP/ci_router.py"'),
    "route job conditional": lambda doc: _job(doc, "route").__setitem__("if", "github.event_name == 'push'"),
    "route output dropped": lambda doc: _job(doc, "route")["outputs"].pop("run-edge-interop"),
    "ci-policy tests removed": _set_run("ci-policy", "unittest", "echo skipped"),
    "concurrency cancels main runs": lambda doc: doc["concurrency"].__setitem__(
        "group", "ci-${{ github.head_ref || github.ref }}"),
    # Worker coverage.
    "worker unconditional skip": lambda doc: _job(doc, "edge-interop").__setitem__("if", "false"),
    "worker removed": lambda doc: doc["jobs"].pop("edge-interop"),
    "unrouted worker added": lambda doc: doc["jobs"].__setitem__(
        "extra", {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}),
    "matrix leg non-blocking": lambda doc: _job(doc, "backend-runtime-matrix")["strategy"]["matrix"]["include"][2]
    .__setitem__("experimental", True),
    "worker continue-on-error": lambda doc: _job(doc, "edge-collector").__setitem__("continue-on-error", True),
    "mypy step continue-on-error": lambda doc: _step(doc, "backend-suite", "mypy app").__setitem__(
        "continue-on-error", True),
    "mypy removed from matrix": _set_run("backend-runtime-matrix", "mypy app", "echo no mypy"),
    "interop harness removed": _set_run("edge-interop", "run_interop", "echo none"),
    "playwright removed": _set_run("browser-e2e", "test:e2e", "echo none"),
    "celery readiness removed": _set_run("browser-e2e", "inspect ping", "true"),
    "e2e scanner mode dropped": lambda doc: _job(doc, "browser-e2e")["env"].pop("CATALOG_PDF_SCAN_MODE"),
    "e2e shares the backend database": lambda doc: _job(doc, "browser-e2e")["env"].__setitem__(
        "DATABASE_URL", "postgresql+asyncpg://dcim_app:dcim_dev_password@localhost:5432/dcim_test"),
    "e2e shares backend ports": lambda doc: _job(doc, "browser-e2e")["services"]["postgres"].__setitem__(
        "ports", ["5432:5432"]),
    "edge install path moved": _set_run("edge-collector", "edge_collector[test]", 'pip install "./edge_collector"'),
}


def _mutate_source(path: Path, old: str, new: str) -> str:
    text = path.read_text()
    assert text.count(old) == 1, f"mutant anchor not unique in {path.name}: {old!r}"
    return text.replace(old, new)


class MutationTests(unittest.TestCase):
    def test_policy_mutants_are_killed(self):
        for old, new in POLICY_MUTANTS:
            with self.subTest(mutant=old), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ci_policy.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_policy.py", old, new))
                self.assertNotEqual(policy_mismatches(load_module("ci_policy", path)), [], "mutant survived")

    def test_router_mutants_are_killed(self):
        for old, new in ROUTER_MUTANTS:
            with self.subTest(mutant=old), tempfile.TemporaryDirectory() as directory:
                shutil.copy(SCRIPTS / "ci_policy.py", directory)
                path = Path(directory) / "ci_router.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_router.py", old, new))
                self.assertNotEqual(router_mismatches(path), [], "mutant survived")

    def test_gate_mutants_are_killed(self):
        for old, new in GATE_MUTANTS:
            with self.subTest(mutant=old), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ci_gate.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_gate.py", old, new))
                self.assertNotEqual(gate_mismatches(load_module("ci_gate", path)), [], "mutant survived")

    def test_workflow_mutants_are_killed(self):
        original = load_workflow()
        self.assertEqual(workflow_errors(original, GATE.GATES), [])
        for name, mutate in WORKFLOW_MUTANTS.items():
            with self.subTest(mutant=name):
                doc = copy.deepcopy(original)
                mutate(doc)
                self.assertNotEqual(workflow_errors(doc, GATE.GATES), [], "mutant survived")


if __name__ == "__main__":
    unittest.main()
