"""Structural tests for .github/workflows/ci.yml and its required checks."""

from __future__ import annotations

import unittest

from ci_support import (
    SCRIPTS,
    emitted_names,
    load_module,
    load_workflow,
    workflow_errors,
)

gate = load_module("ci_gate", SCRIPTS / "ci_gate.py")
release = load_module("verify_release_sha", SCRIPTS / "verify_release_sha.py")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.doc = load_workflow()

    def test_workflow_is_fail_closed(self):
        self.assertEqual(workflow_errors(self.doc, gate.GATES), [])

    def test_required_check_names_are_exact_and_unique(self):
        names = emitted_names(self.doc)
        for required in ("backend", "frontend"):
            self.assertEqual(names.count(required), 1, required)

    def test_release_verifier_job_names_are_emitted(self):
        _, path, required = release.WORKFLOWS["ci"]
        self.assertEqual(path, ".github/workflows/ci.yml")
        names = emitted_names(self.doc)
        for name in required:
            self.assertEqual(names.count(name), 1, name)

    def test_push_to_main_runs_every_job(self):
        self.assertEqual(self.doc[True]["push"], {"branches": ["main"]})


if __name__ == "__main__":
    unittest.main()
