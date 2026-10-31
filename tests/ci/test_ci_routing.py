"""Behavioral tests for the CI policy, the router and the gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from ci_support import (
    HEAD,
    ROOT,
    SCRIPTS,
    WORKERS,
    gate_mismatches,
    load_module,
    pin_mismatches,
    policy_mismatches,
    route_needs,
    router_mismatches,
    scope_mismatches,
)

policy = load_module("ci_policy", SCRIPTS / "ci_policy.py")
gate = load_module("ci_gate", SCRIPTS / "ci_gate.py")


class PolicyTests(unittest.TestCase):
    def test_policy_routes_every_scenario(self):
        self.assertEqual(policy_mismatches(policy), [])

    def test_pin_only_scenarios(self):
        self.assertEqual(pin_mismatches(policy), [])

    def test_policy_workers_are_the_gate_workers(self):
        self.assertEqual(policy.WORKERS, WORKERS)
        self.assertEqual(set(gate.GATES["backend"]), {"route", "ci-policy", *WORKERS})

    def test_named_high_risk_files_exist(self):
        # A renamed security file must not leave a stale rule behind (review P1-04).
        for rule in policy.HIGH_RISK_PATHS:
            with self.subTest(rule=rule):
                self.assertTrue((ROOT / rule).exists(), f"{rule} does not exist")

    def test_every_tracked_file_classifies(self):
        paths = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                               capture_output=True, text=True).stdout.split("\0")
        for path in filter(None, paths):
            decision = policy.classify([path])
            self.assertIn(decision["level"], policy.LEVELS, path)

    def test_security_code_never_routes_below_full(self):
        paths = subprocess.run(["git", "ls-files", "backend/app", "frontend/src"], cwd=ROOT, check=True,
                               capture_output=True, text=True).stdout.split()
        risky = [p for p in paths if any(w in p.lower() for w in ("auth", "rbac", "secur", "crypt", "session"))]
        self.assertTrue(risky)
        for path in risky:
            self.assertEqual(policy.classify([path])["level"], "full", path)


class RouterTests(unittest.TestCase):
    def test_router_routes_real_git_histories(self):
        self.assertEqual(router_mismatches(SCRIPTS / "ci_router.py"), [])

    def test_unreadable_pin_file_selects_full(self):
        # A workflow that is not valid UTF-8 cannot be proven pin-only: the route stays full.
        import tempfile
        from pathlib import Path

        from ci_support import PIN_A, WORKFLOW, commit, git, pin_yml, run_router

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            base = commit(repo, "base", texts={WORKFLOW: pin_yml(PIN_A)})
            (repo / WORKFLOW).write_bytes(pin_yml("b" * 40).encode() + b"\xff\xfe")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "binary")
            head = git(repo, "rev-parse", "HEAD")
            route, _ = run_router(SCRIPTS / "ci_router.py", repo, "pull_request", base, head)
            self.assertEqual(route["level"], "full")
            self.assertTrue(all(route["jobs"].values()))


class GateTests(unittest.TestCase):
    def test_gate_truth_table(self):
        self.assertEqual(gate_mismatches(gate), [])

    def run_gate(self, name, needs, head=HEAD):
        env = dict(os.environ, NEEDS_JSON=json.dumps(needs), EXPECTED_HEAD_SHA=head)
        return subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), name], env=env,
                              capture_output=True, text=True, check=False)

    def test_gate_diffs_the_pr_itself(self):
        # Review P1-07: a PR that changes the CI controls can never pass with fast or component.
        self.assertEqual(scope_mismatches(gate), [])

    def test_gate_command_exit_codes(self):
        self.assertEqual(self.run_gate("backend", route_needs("full", set(WORKERS))).returncode, 0)
        needs = route_needs("full", set(WORKERS))
        needs["browser-e2e"]["result"] = "cancelled"
        result = self.run_gate("backend", needs)
        self.assertEqual(result.returncode, 1)
        self.assertIn("browser-e2e: required by route, result 'cancelled'", result.stdout)

    def test_gate_command_rejects_bad_environment(self):
        env = dict(os.environ, NEEDS_JSON="not json", EXPECTED_HEAD_SHA=HEAD)
        result = subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), "backend"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        env.pop("NEEDS_JSON")
        result = subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), "frontend"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
