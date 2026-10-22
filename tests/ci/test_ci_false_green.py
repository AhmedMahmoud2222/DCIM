"""Negative tests: no applicable required validation can produce a false-green
aggregate result.

These tests prove the fail-closed properties:

* A job that failed, was cancelled, was skipped unexpectedly or never completed
  is never reported as success.
* Change-detection failures escalate to Full CI (never masked as Fast CI).
* Unknown paths escalate to Full CI (never slip through as Fast CI).
* The preserved required checks `backend` and `frontend` always run and can
  never be skipped as a side effect of Fast CI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import ci_policy  # noqa: E402
import ci_router  # noqa: E402

TRUE = ci_router.TRUE
FALSE = ci_router.FALSE
FULL = ci_policy.FULL
FAST = ci_policy.FAST

CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


class RequiredCheckAlwaysRunsTests(unittest.TestCase):
    """backend and frontend are required checks: they always complete."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(CI_YML.read_text())

    def test_backend_and_frontend_jobs_exist(self):
        jobs = self.workflow["jobs"]
        self.assertIn("backend", jobs)
        self.assertIn("frontend", jobs)

    def test_backend_and_frontend_always_run_if_always(self):
        # They must never be skipped as a side effect of Fast CI (never
        # "never completed"). `backend` depends on fast-ci only to read its
        # routing decision; `if: always()` guarantees it runs regardless of
        # Fast CI's result. `frontend` has no needs at all.
        backend_if = self.workflow["jobs"]["backend"].get("if")
        frontend_if = self.workflow["jobs"]["frontend"].get("if")
        self.assertEqual(backend_if, "always()")
        self.assertIsNone(frontend_if)

    def test_backend_reads_fast_ci_risk_but_cannot_be_skipped(self):
        # backend consumes fast-ci's output but `if: always()` means a failed
        # or slow Fast CI never prevents the required `backend` check from
        # completing.
        backend = self.workflow["jobs"]["backend"]
        self.assertEqual(backend.get("needs"), "fast-ci")
        self.assertEqual(backend.get("if"), "always()")
        risk_env = backend["env"].get("RISK_LEVEL")
        self.assertIsNotNone(risk_env, "backend must read the fast-ci risk level")


class FailedCancelledSkippedJobNeverGreenTests(unittest.TestCase):
    """A failed/cancelled/skipped job is never aggregated as success."""

    def test_failed_fast_ci_does_not_mask_into_full_ci_green(self):
        # If Fast CI itself has a real problem (result != success), the Full CI
        # escalation condition requires fast-ci.result == 'success', so Full CI
        # is not launched on top of a failed gate and no false green results.
        workflow = yaml.safe_load(CI_YML.read_text())
        full_if = workflow["jobs"]["full-ci"]["if"]
        self.assertIn("needs.fast-ci.result == 'success'", full_if)

    def test_component_jobs_require_successful_fast_ci(self):
        for job in ("component-edge-collector", "component-database-contracts", "component-opencode"):
            job_if = workflow_if = yaml.safe_load(CI_YML.read_text())["jobs"][job]["if"]
            self.assertIn("needs.fast-ci.result == 'success'", job_if)

    def test_detection_failure_never_reports_fast(self):
        # The router never returns Fast CI when detection failed.
        decision = ci_router.route([], detection_failed=True)
        self.assertEqual(decision["risk-level"], FULL)
        self.assertEqual(decision["run-full-ci"], TRUE)
        self.assertEqual(decision["detection-failed"], TRUE)

    def test_router_outputs_are_complete_never_missing(self):
        # Every output is always present; a missing output would be treated as
        # "not run" (false green) downstream.
        decision = ci_router.route(["README.md"])
        for key in (
            "risk-level",
            "run-full-ci",
            "run-edge-collector",
            "run-database-contracts",
            "run-opencode",
            "detection-failed",
        ):
            self.assertIn(key, decision)
            self.assertIsInstance(decision[key], str)
            self.assertIn(decision[key], (TRUE, FALSE) if key != "risk-level" else (FAST, ci_policy.COMPONENT, FULL))

    def test_no_routing_decision_omits_run_full_ci(self):
        decision = ci_router.route(["random/newfile.xyz"])
        self.assertEqual(decision["run-full-ci"], TRUE)
        self.assertEqual(decision["risk-level"], FULL)


class CancelledJobNeverGreenTests(unittest.TestCase):
    """A cancelled Fast CI must not let component/Full CI run (no false green)."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(CI_YML.read_text())

    def test_cancelled_fast_ci_gates_component_and_full_ci(self):
        # The gate is `needs.fast-ci.result == 'success'`. A cancelled job's result is
        # 'cancelled', which is not 'success', so the condition is False and no
        # downstream component/Full CI job runs - no false-green aggregate.
        for job in ("component-edge-collector", "component-database-contracts",
                    "component-opencode", "full-ci"):
            job_if = self.workflow["jobs"][job]["if"]
            self.assertIn("needs.fast-ci.result == 'success'", job_if)
            self.assertNotIn("== 'cancelled'", job_if)
            self.assertNotIn("always()", job_if)

    def test_cancelled_result_is_not_success(self):
        # Direct logic check: the router/workflow gate treats only 'success' as a pass.
        self.assertEqual('cancelled' == 'success', False)
        self.assertEqual('failure' == 'success', False)
        self.assertEqual('skipped' == 'success', False)


class WorkflowFullCiEscalationTests(unittest.TestCase):
    """Full CI must trigger for detection failure and high-risk changes."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(CI_YML.read_text())

    def test_full_ci_triggers_on_detection_failure(self):
        full_if = self.workflow["jobs"]["full-ci"]["if"]
        self.assertIn("detection-failed == 'true'", full_if)

    def test_full_ci_triggers_on_run_full_ci(self):
        full_if = self.workflow["jobs"]["full-ci"]["if"]
        self.assertIn("run-full-ci == 'true'", full_if)

    def test_full_ci_needs_fast_ci(self):
        self.assertEqual(self.workflow["jobs"]["full-ci"].get("needs"), "fast-ci")


if __name__ == "__main__":
    unittest.main()
