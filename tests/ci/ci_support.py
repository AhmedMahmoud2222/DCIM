"""Shared scenarios for the CI routing tests and the mutation tests.

Each scenario function takes the module or script under test and returns the list
of scenarios where the actual behavior differs from the expected behavior. The
tests assert an empty list for the real code. The mutation tests assert a
non-empty list for each mutant of the same code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
WORKERS = (
    "backend-suite",
    "frontend-suite",
    "backend-runtime-matrix",
    "edge-collector",
    "edge-interop",
    "browser-e2e",
)
HEAD = "a" * 40
RESULTS = ("success", "failure", "cancelled", "skipped", None, "neutral")


def load_module(name: str, path: Path):
    """Import a module from a file path, also when another copy has the same name."""
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))
        sys.modules.pop("ci_policy", None)


# ---------------------------------------------------------------- policy


POLICY_CASES = (
    # (paths, level, workers that must be selected; None = every worker)
    (["README.md"], "fast", set()),
    (["backend/README.md"], "component", {"backend-suite", "browser-e2e"}),
    (["edge_collector/interop/README.md"], "component", {"edge-collector", "edge-interop", "backend-suite"}),
    (["docs/ci/ROUTING_MATRIX.md", "PHASE1_BASELINE.md"], "fast", set()),
    (["frontend/src/App.tsx"], "component", {"frontend-suite", "browser-e2e"}),
    (["backend/app/application/alarm_service.py"], "component", {"backend-suite", "browser-e2e"}),
    (["backend/tests/unit/test_drivers.py"], "component", {"backend-suite", "browser-e2e"}),
    (["edge_collector/edge_collector/snmp.py"], "component",
     {"edge-collector", "edge-interop", "backend-suite"}),
    (["backend/app/domain/auth/models.py"], "full", None),
    (["backend/app/core/security.py"], "full", None),
    (["backend/app/core/config.py"], "full", None),
    (["backend/app/api/v1/auth.py"], "full", None),
    (["backend/app/application/auth_service.py"], "full", None),
    (["backend/app/application/rbac.py"], "full", None),
    (["backend/app/application/authority_lock.py"], "full", None),
    (["backend/app/application/collector_auth.py"], "full", None),
    (["backend/app/api/v1/new_permission_checks.py"], "full", None),
    (["README.md", "backend/app/api/v1/auth.py"], "full", None),
    (["frontend/src/auth/session.ts"], "full", None),
    ([".github/scripts/opencode_sandbox.py"], "full", None),
    ([".github/opencode/model-registry.json"], "full", None),
    ([".github/workflows/ci.yml"], "full", None),
    ([".github/scripts/ci_policy.py"], "full", None),
    ([".github/scripts/ci_gate.py"], "full", None),
    (["backend/migrations/versions/0099_new.py"], "full", None),
    (["backend/pyproject.toml"], "full", None),
    (["frontend/package-lock.json"], "full", None),
    (["edge_collector/pyproject.toml"], "full", None),
    (["docker-compose.yml"], "full", None),
    (["tests/deployment/test_x.py"], "full", None),
    (["scripts/deploy-docker-compose.sh"], "full", None),
    (["Makefile"], "full", None),
    (["unknown/dir/file.py"], "full", None),
    (["docs/x.md", "new_top_level.txt"], "full", None),
    ([], "full", None),
    ([""], "full", None),
    (["/etc/passwd"], "full", None),
)


def policy_mismatches(policy) -> list[str]:
    mismatches = []
    for paths, level, selected in POLICY_CASES:
        try:
            decision = policy.classify(list(paths))
        except Exception as error:  # noqa: BLE001 - a crash is a mismatch
            mismatches.append(f"{paths}: raised {error!r}")
            continue
        jobs = decision.get("jobs", {})
        want = set(WORKERS) if selected is None else selected
        got = {job for job, run in jobs.items() if run}
        if decision.get("level") != level or set(jobs) != set(WORKERS):
            mismatches.append(f"{paths}: level {decision.get('level')} jobs {sorted(jobs)}")
        elif (selected is None and got != want) or not want <= got:
            mismatches.append(f"{paths}: selected {sorted(got)}, expected {sorted(want)}")
    return mismatches


# ---------------------------------------------------------------- router


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, message: str, write=(), delete=(), rename=()) -> str:
    for path in write:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path} {message}\n")
    for path in delete:
        git(repo, "rm", "-q", path)
    for old, new in rename:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", old, new)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run_router(router: Path, repo: Path, event: str, base: str, head: str) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as directory:
        output_file = Path(directory) / "output"
        env = dict(os.environ, CI_EVENT_NAME=event, CI_BASE_SHA=base, CI_HEAD_SHA=head,
                   GITHUB_OUTPUT=str(output_file))
        subprocess.run([sys.executable, str(router)], cwd=repo, env=env, check=True,
                       capture_output=True, text=True)
        outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines())
    return json.loads(outputs["route"]), outputs


def router_mismatches(router: Path) -> list[str]:
    """Run the router against real git histories. Return the wrong routes."""
    mismatches = []
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "-q")
        base = commit(repo, "base", write=["README.md", "backend/app/domain/auth/policy.py",
                                            "backend/app/application/alarm_service.py",
                                            "frontend/src/App.tsx"])
        cases = []

        git(repo, "checkout", "-q", "-b", "docs", base)
        commit(repo, "d1", write=["docs/a.md"])
        cases.append(("multi-commit docs", base, commit(repo, "d2", write=["docs/b.md"]), "fast", 2))

        git(repo, "checkout", "-q", "-b", "multi", base)
        commit(repo, "m1", write=["backend/app/application/alarm_service.py"])
        cases.append(("multi-commit backend then docs", base,
                      commit(repo, "m2", write=["docs/c.md"]), "component", 2))

        git(repo, "checkout", "-q", "-b", "rename", base)
        cases.append(("rename out of auth", base,
                      commit(repo, "r", rename=[("backend/app/domain/auth/policy.py", "docs/policy.py")]),
                      "full", 2))

        git(repo, "checkout", "-q", "-b", "delete", base)
        cases.append(("delete auth file", base,
                      commit(repo, "x", delete=["backend/app/domain/auth/policy.py"]), "full", 1))

        git(repo, "checkout", "-q", "-b", "frontend", base)
        cases.append(("frontend only", base, commit(repo, "f", write=["frontend/src/App.tsx"]),
                      "component", 1))

        # The base branch moves on after the PR branches off: only the PR diff counts.
        git(repo, "checkout", "-q", "main")
        moved = commit(repo, "main moves", write=["backend/app/core/config.py"])
        cases.append(("moved base", moved, git(repo, "rev-parse", "docs"), "fast", 2))

        head = git(repo, "rev-parse", "docs")
        for name, case_base, case_head, level, count in cases:
            route, outputs = run_router(router, repo, "pull_request", case_base, case_head)
            if route.get("level") != level or route.get("changed_count") != count or route.get("degraded"):
                mismatches.append(f"{name}: {route.get('level')} count {route.get('changed_count')}")
            elif outputs.get("level") != level or outputs.get("head-sha") != case_head:
                mismatches.append(f"{name}: outputs {outputs}")
            elif any(outputs.get(f"run-{job}") != ("true" if route["jobs"][job] else "false")
                     for job in WORKERS):
                mismatches.append(f"{name}: run-* outputs disagree with the route JSON")

        degraded = (
            ("missing base commit", "pull_request", "b" * 40, head),
            ("short base SHA", "pull_request", base[:7], head),
            ("empty base SHA", "pull_request", "", head),
            ("uppercase head SHA", "pull_request", base, head.upper()),
        )
        for name, event, case_base, case_head in degraded:
            route, _ = run_router(router, repo, event, case_base, case_head)
            if route.get("level") != "full" or not route.get("degraded"):
                mismatches.append(f"{name}: {route.get('level')} degraded={route.get('degraded')}")
        for event in ("push", "workflow_dispatch", "pull_request-without-base-policy", ""):
            route, _ = run_router(router, repo, event, base, head)
            if route.get("level") != "full" or not all(route["jobs"].values()):
                mismatches.append(f"event {event!r}: {route.get('level')}")
    return mismatches


# ---------------------------------------------------------------- gate


def route_needs(level: str, selected: set[str], head: str = HEAD, gate_scope=WORKERS) -> dict:
    jobs = {job: (level == "full" or job in selected) for job in WORKERS}
    route = {"level": level, "head": head, "jobs": jobs, "event": "pull_request", "degraded": False}
    outputs = {"level": level, "head-sha": head, "route": json.dumps(route)}
    outputs.update({f"run-{job}": "true" if jobs[job] else "false" for job in WORKERS})
    needs = {"route": {"result": "success", "outputs": outputs}, "ci-policy": {"result": "success", "outputs": {}}}
    for job in gate_scope:
        needs[job] = {"result": "success" if jobs[job] else "skipped", "outputs": {}}
    return needs


ROUTES = (
    ("fast", set()),
    ("component", {"frontend-suite", "browser-e2e"}),
    ("component", {"backend-suite", "browser-e2e"}),
    ("component", {"edge-collector", "edge-interop", "backend-suite"}),
    ("full", set(WORKERS)),
)
FRONTEND_SCOPE = ("frontend-suite", "browser-e2e")


def gate_cases():
    """Yield (name, gate, needs, expected_head, expected_pass)."""
    for gate, scope in (("backend", WORKERS), ("frontend", FRONTEND_SCOPE)):
        for level, selected in ROUTES:
            yield (f"{gate} {level} {sorted(selected)} all as routed", gate,
                   route_needs(level, selected, gate_scope=scope), HEAD, True)
            for job in scope:
                applicable = level == "full" or job in selected
                for result in RESULTS:
                    needs = route_needs(level, selected, gate_scope=scope)
                    needs[job]["result"] = result
                    expected = result == "success" if applicable else result in ("skipped", "success")
                    yield (f"{gate} {level} {job}={result}", gate, needs, HEAD, expected)
            for control in ("route", "ci-policy"):
                for result in RESULTS:
                    needs = route_needs(level, selected, gate_scope=scope)
                    needs[control]["result"] = result
                    yield (f"{gate} {level} {control}={result}", gate, needs, HEAD, result == "success")

    def mutated(change, gate="backend", head=HEAD, level="component", selected=("frontend-suite",)):
        scope = WORKERS if gate == "backend" else FRONTEND_SCOPE
        needs = route_needs(level, set(selected), gate_scope=scope)
        change(needs)
        return needs, head

    def set_output(key, value):
        return lambda needs: needs["route"]["outputs"].__setitem__(key, value)

    def set_route(key, value):
        def change(needs):
            route = json.loads(needs["route"]["outputs"]["route"])
            route[key] = value
            needs["route"]["outputs"]["route"] = json.dumps(route)
        return change

    def drop_route_job(job):
        def change(needs):
            route = json.loads(needs["route"]["outputs"]["route"])
            del route["jobs"][job]
            needs["route"]["outputs"]["route"] = json.dumps(route)
            needs["route"]["outputs"].pop(f"run-{job}")
            needs[job]["result"] = "skipped"
        return change

    invalid = (
        ("route output not JSON", set_output("route", "{")),
        ("route output empty", set_output("route", "")),
        ("route output missing", lambda needs: needs["route"]["outputs"].pop("route")),
        ("route outputs missing", lambda needs: needs["route"].pop("outputs")),
        ("level output disagrees", set_output("level", "full")),
        ("level unknown", set_route("level", "partial")),
        ("route head is another commit", set_route("head", "c" * 40)),
        ("head output is another commit", set_output("head-sha", "c" * 40)),
        ("run flag disagrees with JSON", set_output("run-frontend-suite", "false")),
        ("run flag malformed", set_output("run-backend-suite", "yes")),
        ("job flag not boolean", set_route("jobs", {job: "true" for job in WORKERS})),
        ("jobs not an object", set_route("jobs", [])),
        ("job flags are integers", set_route("jobs", {job: int(job == "frontend-suite") for job in WORKERS})),
        ("route and output level unknown", lambda needs: (set_route("level", "partial")(needs),
                                                         set_output("level", "partial")(needs))),
        ("component route from a push event", set_route("event", "push")),
        ("component route from the bootstrap event", set_route("event", "pull_request-without-base-policy")),
        ("component route without an event", set_route("event", None)),
        ("component route after degraded detection", set_route("degraded", True)),
        ("component route with degraded missing", set_route("degraded", None)),
        ("component selects nothing", lambda needs: (
            set_route("jobs", {job: False for job in WORKERS})(needs),
            set_output("run-frontend-suite", "false")(needs),
            needs["frontend-suite"].__setitem__("result", "skipped"))),
        ("worker missing from needs", lambda needs: needs.pop("edge-interop")),
        ("route missing from needs", lambda needs: needs.pop("route")),
        ("ci-policy missing from needs", lambda needs: needs.pop("ci-policy")),
        ("unaudited extra need", lambda needs: needs.__setitem__("extra", {"result": "success"})),
        ("need entry not an object", lambda needs: needs.__setitem__("backend-suite", "skipped")),
        ("new worker unknown to base policy skipped", drop_route_job("edge-interop")),
    )
    for name, change in invalid:
        needs, head = mutated(change)
        yield (f"invalid: {name}", "backend", needs, head, False)
    needs, _ = mutated(lambda needs: None, level="fast", selected=())
    route = json.loads(needs["route"]["outputs"]["route"])
    route["jobs"]["backend-suite"] = True
    needs["route"]["outputs"]["route"] = json.dumps(route)
    needs["route"]["outputs"]["run-backend-suite"] = "true"
    needs["backend-suite"]["result"] = "success"
    yield ("invalid: fast selects a worker", "backend", needs, HEAD, False)
    needs, _ = mutated(lambda needs: None, level="full", selected=WORKERS)
    route = json.loads(needs["route"]["outputs"]["route"])
    route["jobs"]["edge-interop"] = False
    needs["route"]["outputs"]["route"] = json.dumps(route)
    needs["route"]["outputs"]["run-edge-interop"] = "false"
    yield ("invalid: full skips a worker", "backend", needs, HEAD, False)
    needs, _ = mutated(lambda needs: (set_route("head", "")(needs), set_output("head-sha", "")(needs)))
    yield ("invalid: empty head everywhere", "backend", needs, "", False)
    for head in ("", "abc", "A" * 40):
        needs, _ = mutated(lambda needs: None)
        yield (f"invalid: expected head {head!r}", "backend", needs, head, False)
    yield ("invalid: needs not an object", "backend", None, HEAD, False)
    yield ("invalid: unknown gate", "release", route_needs("full", set(WORKERS)), HEAD, False)
    yield ("frontend gate with backend scope", "frontend", route_needs("fast", set()), HEAD, False)


def gate_mismatches(gate_module) -> list[str]:
    mismatches = []
    for name, gate, needs, head, expected in gate_cases():
        try:
            passed = gate_module.evaluate(gate, needs, head) == []
        except Exception as error:  # noqa: BLE001 - a crash is a mismatch
            mismatches.append(f"{name}: raised {error!r}")
            continue
        if passed != expected:
            mismatches.append(f"{name}: passed={passed}, expected {expected}")
    return mismatches


# ---------------------------------------------------------------- workflow


# Commands that each worker must keep. This is the coverage inventory of the
# original CI jobs (main before the routing change).
COMMAND_INVENTORY = {
    "backend-suite": (
        "sec-arch-02-preflight", 'pip install -e ".[dev]"', "tesseract-ocr", "clamav-daemon",
        "python scripts/check_compose_settings.py", "CREATE ROLE dcim_app",
        "python scripts/check_alembic_single_head.py", "ruff check app tests", "mypy app",
        "alembic upgrade head", "alembic downgrade 0010_mvp_alarms",
        "scripts/bootstrap_privileged_roles.sql", "setcap cap_net_raw+ep",
        "pytest -q tests/integration/test_mvp_retention_hostile.py",
        "pytest -q tests/api/test_catalog_extraction_apply.py",
        "pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py",
    ),
    "frontend-suite": (
        "npm ci", "npm audit --audit-level=high", "npm test", "npm run typecheck",
        "npx eslint . --ext ts,tsx", "npm run build",
    ),
    "backend-runtime-matrix": (
        'pip install -e ".[dev]"', "tesseract-ocr", "python scripts/check_alembic_single_head.py",
        "ruff check app tests", "mypy app", "alembic upgrade head", "alembic downgrade 0010_mvp_alarms",
        "scripts/bootstrap_privileged_roles.sql",
        "pytest -q tests/integration/test_mvp_retention_hostile.py",
        "pytest -q --ignore=tests/integration/test_mvp_retention_hostile.py",
        "pytest -q edge_collector/tests",
    ),
    "edge-collector": ('pip install "./edge_collector[test]"', "ruff check edge_collector",
                       "pytest -q edge_collector/tests"),
    "edge-interop": ("python -m edge_collector.interop.run_interop --json interop-results.json",
                     "snmpv3-interop-results"),
    "browser-e2e": (
        "CREATE DATABASE dcim_e2e;", "alembic upgrade head", "scripts/create_admin.py",
        "/api/v1/health/ready", "celery -A app.infrastructure.celery_app worker",
        "inspect ping", "npx playwright install --with-deps chromium", "npm run test:e2e",
    ),
}
# Isolation and fixture settings that the browser suite needs.
E2E_ENV = {
    "CATALOG_PDF_SCAN_MODE": "off", "OUTBOUND_ALLOW_LOOPBACK": "true",
    "OUTBOUND_ALLOW_HTTP": "true", "OUTBOUND_ALLOWED_PORTS": "[18089]",
    "DATABASE_URL": "postgresql+asyncpg://dcim_app:e2e_app_password@127.0.0.1:5433/dcim_e2e",
}
EXPECTED_HEAD_EXPR = "${{ github.event.pull_request.head.sha || github.sha }}"


def load_workflow(path: Path = CI_YML) -> dict:
    return yaml.safe_load(path.read_text())


def job_text(job: dict) -> str:
    return yaml.safe_dump(job, sort_keys=True, width=10_000)


def emitted_names(doc: dict) -> list[str]:
    """The check names that the jobs emit, with matrix names expanded."""
    names = []
    for job_id, job in doc["jobs"].items():
        name = job.get("name", job_id)
        include = job.get("strategy", {}).get("matrix", {}).get("include")
        if include:
            for entry in include:
                expanded = name
                for key, value in entry.items():
                    expanded = expanded.replace("${{ matrix." + key + " }}", str(value))
                names.append(expanded)
        else:
            names.append(name)
    return names


def executable(script: object) -> str:
    """The shell text of a run script without comment lines and trailing comments."""
    lines = []
    for line in str(script).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(stripped.split(" #", 1)[0])
    return " ".join(lines)


def route_script(doc: dict) -> str:
    return next(step["run"] for step in doc["jobs"]["route"]["steps"] if step.get("id") == "route")


def workflow_errors(doc: dict, gates: dict, workers=WORKERS) -> list[str]:
    """Structural rules that make the required checks fail closed."""
    errors = []
    jobs = doc.get("jobs", {})
    group = str(doc.get("concurrency", {}).get("group", ""))
    if "github.ref" in group or "github.sha" not in group:
        errors.append("concurrency group can cancel a main run")
    for gate, scope in gates.items():
        job = jobs.get(gate)
        if not isinstance(job, dict):
            errors.append(f"gate {gate} missing")
            continue
        if job.get("name", gate) != gate:
            errors.append(f"gate {gate} emits check name {job.get('name')!r}")
        if job.get("if") != "always()":
            errors.append(f"gate {gate} if is {job.get('if')!r}, expected always()")
        for key in ("strategy", "continue-on-error"):
            if key in job:
                errors.append(f"gate {gate} has {key}")
        needs = job.get("needs")
        if not isinstance(needs, list) or sorted(needs) != sorted(scope) or len(needs) != len(scope):
            errors.append(f"gate {gate} needs {needs}, expected {list(scope)}")
        steps = job.get("steps", [])
        gate_steps = [s for s in steps if s.get("run") == f"python3 .github/scripts/ci_gate.py {gate}"]
        if len(gate_steps) != 1 or gate_steps[0].get("continue-on-error"):
            errors.append(f"gate {gate} does not run ci_gate.py {gate} exactly once")
        else:
            env = gate_steps[0].get("env", {})
            if env.get("NEEDS_JSON") != "${{ toJSON(needs) }}" or env.get("EXPECTED_HEAD_SHA") != EXPECTED_HEAD_EXPR:
                errors.append(f"gate {gate} gate step env is {env}")
        if any("if" in step for step in steps):
            errors.append(f"gate {gate} has a conditional step")
    for job_id, job in jobs.items():
        if job_id in gates:
            continue
        if job.get("name", job_id) in gates:
            errors.append(f"job {job_id} emits the required check name {job.get('name')!r}")
    route = jobs.get("route", {})
    for job_id in ("route", "ci-policy"):
        if job_id not in jobs or "if" in jobs[job_id] or "needs" in jobs[job_id]:
            errors.append(f"control job {job_id} missing or conditional")
    route_runs = executable(" ".join(step.get("run", "") for step in route.get("steps", [])))
    if 'git show "$CI_BASE_SHA:.github/scripts/ci_policy.py"' not in route_runs or \
            'python3 "$trusted/ci_router.py"' not in route_runs:
        errors.append("route job does not run the base-commit policy")
    if ".github/scripts/" in route_runs.replace("$CI_BASE_SHA:.github/scripts/", ""):
        errors.append("route job reads a routing file from the checkout")
    if f"for job in {' '.join(workers)}; do" not in route_runs:
        errors.append("route job bootstrap does not select every worker")
    policy_runs = " ".join(executable(step.get("run", "")) for step in jobs.get("ci-policy", {}).get("steps", []))
    if policy_runs.strip() != "python3 -m pip install PyYAML==6.0.2 python3 -m unittest discover -s tests/ci -v":
        errors.append("ci-policy job does not run tests/ci")
    outputs = route.get("outputs", {})
    for key in ("level", "head-sha", "route"):
        if outputs.get(key) != "${{ steps.route.outputs." + key + " }}":
            errors.append(f"route output {key} missing")
    actual_workers = set(jobs) - set(gates) - {"route", "ci-policy"}
    if actual_workers != set(workers):
        errors.append(f"worker jobs {sorted(actual_workers)} are not the policy workers {sorted(workers)}")
    for job_id in workers:
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            continue
        if outputs.get(f"run-{job_id}") != "${{ steps.route.outputs.run-" + job_id + " }}":
            errors.append(f"route output run-{job_id} missing")
        expected_if = f"needs.route.outputs.level == 'full' || needs.route.outputs.run-{job_id} == 'true'"
        if job.get("needs") != "route" or job.get("if") != expected_if:
            errors.append(f"worker {job_id} needs/if is {job.get('needs')!r}/{job.get('if')!r}")
        if job.get("continue-on-error") not in (None, False, "${{ matrix.experimental }}"):
            errors.append(f"worker {job_id} continue-on-error {job.get('continue-on-error')!r}")
        for entry in job.get("strategy", {}).get("matrix", {}).get("include", []):
            if entry.get("experimental") is not False:
                errors.append(f"worker {job_id} has a non-blocking matrix entry {entry}")
        steps = job.get("steps", [])
        for step in steps:
            if step.get("continue-on-error") and "clamav-daemon" not in step.get("run", ""):
                errors.append(f"worker {job_id} step {step.get('name')!r} is continue-on-error")
        text = " ".join(executable(step.get("run", "")) + " " + str(step.get("with", "")) for step in steps)
        for command in COMMAND_INVENTORY.get(job_id, ()):
            if command not in text:
                errors.append(f"worker {job_id} no longer runs {command!r}")
        if any(str(step.get("run", "")).strip().startswith(("echo", "true", ":")) and
               "pytest" in str(step.get("name", "")).lower() for step in steps):
            errors.append(f"worker {job_id} replaced a test step")
    e2e_env = jobs.get("browser-e2e", {}).get("env", {})
    for key, value in E2E_ENV.items():
        if e2e_env.get(key) != value:
            errors.append(f"browser-e2e env {key}={e2e_env.get(key)!r}")
    if "browser-e2e" in jobs and "backend-suite" in jobs:
        ports = lambda job: sorted(str(s.get("ports")) for s in job.get("services", {}).values())
        if set(ports(jobs["browser-e2e"])) & set(ports(jobs["backend-suite"])):
            errors.append("browser-e2e shares service ports with backend-suite")
    return errors


def run_route_step(doc: dict, repo: Path, event: str, base: str, head: str) -> dict:
    """Run the route step of the workflow with bash. Return the step outputs."""
    with tempfile.TemporaryDirectory() as directory:
        output_file = Path(directory) / "output"
        output_file.write_text("")
        env = dict(os.environ, CI_EVENT_NAME=event, CI_BASE_SHA=base, CI_HEAD_SHA=head,
                   GITHUB_OUTPUT=str(output_file), RUNNER_TEMP=directory)
        result = subprocess.run(["bash", "-c", route_script(doc)], cwd=repo, env=env,
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AssertionError(f"route step failed: {result.stderr}")
        outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines())
    return outputs


def route_step_mismatches(doc: dict, gate_module) -> list[str]:
    """Run the real route step against repositories with and without a base policy.

    The outputs go to the real gate. A full route must require every worker.
    """
    mismatches = []
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "-q")
        bare = commit(repo, "base without policy", write=["README.md"])
        # A PR head whose own router and policy would claim fast.
        (repo / ".github/scripts").mkdir(parents=True)
        (repo / ".github/scripts/ci_router.py").write_text(
            "import os\nopen(os.environ['GITHUB_OUTPUT'], 'a').write('level=fast\\n')\n")
        (repo / ".github/scripts/ci_policy.py").write_text("")
        hostile = commit(repo, "hostile head", write=["docs/a.md"])
        git(repo, "checkout", "-q", "-b", "policy", bare)
        for name in ("ci_policy.py", "ci_router.py"):
            (repo / ".github/scripts").mkdir(parents=True, exist_ok=True)
            (repo / ".github/scripts" / name).write_text((SCRIPTS / name).read_text())
        trusted = commit(repo, "base with policy")
        docs = commit(repo, "docs head", write=["docs/b.md"])
        git(repo, "checkout", "-q", hostile)
        cases = (
            ("bootstrap PR", "pull_request", bare, hostile, "full"),
            ("push", "push", "", hostile, "full"),
            ("workflow_dispatch", "workflow_dispatch", "", hostile, "full"),
            ("base policy, docs PR", "pull_request", trusted, docs, "fast"),
        )
        for name, event, base, head, level in cases:
            outputs = run_route_step(doc, repo, event, base, head)
            if outputs.get("level") != level or outputs.get("head-sha") != head:
                mismatches.append(f"{name}: outputs {outputs}")
                continue
            needs = {"route": {"result": "success", "outputs": outputs},
                     "ci-policy": {"result": "success", "outputs": {}}}
            for job in WORKERS:
                needs[job] = {"result": "skipped", "outputs": {}}
            skipped_passes = gate_module.evaluate("backend", needs, head) == []
            for job in WORKERS:
                needs[job]["result"] = "success"
            if gate_module.evaluate("backend", needs, head) != []:
                mismatches.append(f"{name}: gate rejects a valid {level} route: "
                                  f"{gate_module.evaluate('backend', needs, head)}")
            if skipped_passes != (level == "fast"):
                mismatches.append(f"{name}: gate with skipped workers passed={skipped_passes}")
    return mismatches
