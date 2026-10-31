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
    pin_mismatches,
    SCRIPTS,
    gate_mismatches,
    load_module,
    load_workflow,
    policy_mismatches,
    route_step_mismatches,
    router_mismatches,
    scope_mismatches,
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

PIN_MUTANTS = (
    ('if change.get("status") != "M" or change.get("old_mode")', 'if change.get("old_mode")'),
    ('len(old) > PIN_MAX_BYTES or len(new) > PIN_MAX_BYTES or old == new', 'len(old) > PIN_MAX_BYTES'),
    ('PIN_EXCLUDED = (".github/workflows/ci.yml", ".github/workflows/deploy.yml")', "PIN_EXCLUDED = ()"),
    ('_PIN = r"[0-9a-f]{40}"', '_PIN = r"[0-9a-z]{1,40}"'),
    ("if before is None or before != after:", "if before is None:"),
    ("if not changes or len(changes) > 20:", "if len(changes) > 20:"),
    ("if not changes or len(changes) > 20:", "if not changes:"),
    ('change.get("new_mode") != REGULAR_FILE_MODE', "False"),
    ("and path not in PIN_EXCLUDED:", "and True:"),
    ('and "/" not in path[len(WORKFLOW_DIR):]', "and True"),
    ("if rule is None or SAFE_TEXT.fullmatch(text) is None:", "if rule is None:"),
    ('_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+-]*"', '_VERSION = r"[^\\r\\n]*"'),
    ('_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+-]*"', '_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+ ()$`;&|<>-]*"'),
    ("\\xa0-\\u2027\\u202a-\\ud7ff", "\\xa0-\\ud7ff"),
    ("\\x20-\\x7e", "\\x20-\\x7f"),
    ("\\ue000-\\ufefe\\uff00-\\ufffd", "\\ue000-\\ufffd"),
    ('path.endswith(".yml")', "True"),
    ('if before_structure is None or before_structure != structure(path, new):', "if before_structure is None:"),
    ('if before_structure is None or before_structure != structure(path, new):', "if False:"),
    ('        if match is not None:\n            return f"{match.group(1)}@<pin>"', "        if False:\n            return 'x'"),
    ('if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):', "if False:"),
    ('if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):',
     "if len(names) != len(set(names)):"),
    ('if not isinstance(key, yaml.ScalarNode) or key.value == "<<":', "if not isinstance(key, yaml.ScalarNode):"),
    ('raise ValueError("YAML tag")', "pass"),
    ('raise ValueError("YAML anchor")', "pass"),
    ('raise ValueError("duplicate JSON key")', "pass"),
    ("return ast.dump(tree) if masked == 1 else None", "return ast.dump(tree)"),
)

ROUTER_MUTANTS = (
    ("decision = pinned", 'decision = {**pinned, "level": "component"}'),


    ('output = _git("diff", "--no-renames",', 'output = _git("diff",'),
    ('merge_base = _git("merge-base", base, head).strip()', "merge_base = base"),
    ('decision = ci_policy.full([f"change detection failed: {error}"])',
     'decision = ci_policy.classify(["README.md"])'),
    ('route["degraded"] = True', 'route["degraded"] = False'),
    ('if event != "pull_request":', 'if event not in ("pull_request", "push", "workflow_dispatch"):'),
    ('SHA = re.compile(r"[0-9a-f]{40}")', 'SHA = re.compile(r"[0-9a-fA-F]{4,40}")'),
    ('values[f"run-{job}"] = "true" if route["jobs"][job] else "false"', 'values[f"run-{job}"] = "true"'),
)

SCOPE_MUTANTS = (
    ('if pinned is not None and pinned["paths"] == sorted(paths):', "if pinned is not None:"),
    ('pinned = ci_policy.pin_only(changes) if level == "fast" and changes else None',
     "pinned = ci_policy.pin_only(changes) if changes else None"),
    ('changes = pr_changes(base, head) if level == "fast" and trusted else None', "changes = None"),
    ('TRUST_PREFIXES = (".github/", "tests/ci/")', 'TRUST_PREFIXES = (".github/workflows/", "tests/ci/")'),
    ('TRUST_PREFIXES = (".github/", "tests/ci/")', 'TRUST_PREFIXES = (".github/",)'),
    ("if path.lower().startswith(TRUST_PREFIXES)", "if path.startswith(TRUST_PREFIXES)"),
    ('if level == "full":\n        return []', 'if level != "fast":\n        return []'),
    ("errors = scope_errors(level, paths, changes)", "errors = []"),
    ('"git", "diff", "--no-renames",', '"git", "diff",'),
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
    ('if level != "full" and (route.get("event") != "pull_request" or route.get("degraded") is not False):',
     "if False:"),
    ('route.get("event") != "pull_request" or route.get("degraded") is not False',
     'route.get("event") != "pull_request"'),
    ('route.get("event") != "pull_request" or route.get("degraded") is not False',
     'route.get("degraded") is True'),
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


def _set_route_script(old, new):
    def mutate(doc):
        step = next(s for s in doc["jobs"]["route"]["steps"] if s.get("id") == "route")
        assert step["run"].count(old) == 1, old
        step["run"] = step["run"].replace(old, new)
    return mutate


WORKFLOW_MUTANTS = {
    # The three review P2 mutations.
    "backend display name changed": lambda doc: _job(doc, "backend").__setitem__("name", "arbitrary-success"),
    "backend display name capitalized": lambda doc: _job(doc, "backend").__setitem__("name", "Backend"),
    "worker condition disabled with false &&": lambda doc: _job(doc, "backend-runtime-matrix").__setitem__(
        "if", "false && " + _job(doc, "backend-runtime-matrix")["if"]),
    "regression pytest replaced with echo": _set_run(
        "backend-suite", "--ignore=tests/integration/test_mvp_retention_hostile.py", "echo removed"),
    # Review re-check P2: a shell comment is not an executed command.
    "regression pytest commented out": _set_run(
        "backend-suite", "--ignore=tests/integration/test_mvp_retention_hostile.py",
        "# pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py"),
    "regression pytest behind a trailing comment": _set_run(
        "backend-suite", "--ignore=tests/integration/test_mvp_retention_hostile.py",
        "true # pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py"),
    "mypy commented out": _set_run("backend-runtime-matrix", "mypy app", "# mypy app"),
    # Review re-check P1-07: the bootstrap route must not run checkout code.
    "bootstrap runs the checkout router": _set_route_script(
        'echo "Routing policy source: none (event $event selects full)"',
        'CI_EVENT_NAME="$event" python3 .github/scripts/ci_router.py; exit 0'),
    "bootstrap copies the checkout policy": _set_route_script(
        'echo "Routing policy source: none (event $event selects full)"',
        'cp .github/scripts/ci_*.py "$trusted/"; CI_EVENT_NAME="$event" python3 "$trusted/ci_router.py"; exit 0'),
    "bootstrap drops a worker": _set_route_script("edge-interop browser-e2e; do", "browser-e2e; do"),
    "bootstrap writes fast": _set_route_script('echo "level=full"', 'echo "level=fast"'),
    "bootstrap for push uses the router": _set_route_script(
        'if [ "$CI_EVENT_NAME" = pull_request ] \\\n', 'if true \\\n'),
    "gate base SHA dropped": lambda doc: _step(doc, "backend", "ci_gate.py")["env"].pop("CI_BASE_SHA"),
    "gate shallow checkout": lambda doc: next(
        s for s in _job(doc, "frontend")["steps"] if "checkout" in str(s.get("uses")))["with"].pop("fetch-depth"),
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

    def test_pin_mutants_are_killed(self):
        self.assertEqual(pin_mismatches(load_module("ci_policy", SCRIPTS / "ci_policy.py")), [])
        for old, new in PIN_MUTANTS:
            with self.subTest(mutant=old), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ci_policy.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_policy.py", old, new))
                self.assertNotEqual(pin_mismatches(load_module("ci_policy", path)), [], "mutant survived")

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
                shutil.copy(SCRIPTS / "ci_policy.py", directory)
                path = Path(directory) / "ci_gate.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_gate.py", old, new))
                self.assertNotEqual(gate_mismatches(load_module("ci_gate", path)), [], "mutant survived")

    def test_scope_mutants_are_killed(self):
        for old, new in SCOPE_MUTANTS:
            with self.subTest(mutant=old), tempfile.TemporaryDirectory() as directory:
                shutil.copy(SCRIPTS / "ci_policy.py", directory)
                path = Path(directory) / "ci_gate.py"
                path.write_text(_mutate_source(SCRIPTS / "ci_gate.py", old, new))
                self.assertNotEqual(scope_mismatches(load_module("ci_gate", path)), [], "mutant survived")

    def test_workflow_mutants_are_killed(self):
        original = load_workflow()
        self.assertEqual(workflow_errors(original, GATE.GATES), [])
        for name, mutate in WORKFLOW_MUTANTS.items():
            with self.subTest(mutant=name):
                doc = copy.deepcopy(original)
                mutate(doc)
                errors = workflow_errors(doc, GATE.GATES)
                if not errors:
                    try:
                        errors = route_step_mismatches(doc, GATE)
                    except AssertionError as error:
                        errors = [str(error)]
                self.assertNotEqual(errors, [], "mutant survived")


if __name__ == "__main__":
    unittest.main()
