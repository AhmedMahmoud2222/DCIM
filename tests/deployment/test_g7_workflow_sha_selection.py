"""Regression tests for exact SHA selection in the G7 mutation workflow."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/g7-mutation-validation.yml"
PR_HEAD = "github.event.pull_request.head.sha"


def _select(expr: str, event: str, pr_sha: str, input_sha: str) -> str:
    match = re.fullmatch(
        r"\$\{\{ github\.event_name == 'pull_request' && (\S+) \|\| (\S+) \}\}",
        expr,
    )
    if not match:
        raise AssertionError(f"unexpected EXPECTED_SHA expression: {expr}")
    values = {PR_HEAD: pr_sha, "github.event.inputs.sha": input_sha}
    return values[match.group(1) if event == "pull_request" else match.group(2)]


class G7WorkflowShaSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.job = cls.workflow["jobs"]["g7-mutation"]
        cls.expected = cls.job["env"]["EXPECTED_SHA"]

    def test_workflow_token_permissions_are_read_only(self) -> None:
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})

    def test_both_checkouts_disable_credential_persistence(self) -> None:
        checkouts = [
            step for step in self.job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertEqual(len(checkouts), 2)
        for step in checkouts:
            with self.subTest(step=step.get("name")):
                self.assertIs(step["with"].get("persist-credentials"), False)

    def test_pull_request_uses_pr_head_not_merge_sha(self) -> None:
        pr_sha, dispatch_sha = "a" * 40, "b" * 40
        self.assertEqual(_select(self.expected, "pull_request", pr_sha, dispatch_sha), pr_sha)
        self.assertNotIn("github.sha", self.expected)

    def test_dispatch_uses_explicit_input_sha(self) -> None:
        self.assertEqual(_select(self.expected, "workflow_dispatch", "a" * 40, "b" * 40), "b" * 40)

    def test_both_checkouts_use_expected_sha(self) -> None:
        checkouts = [
            step for step in self.job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertEqual(len(checkouts), 2)
        for step in checkouts:
            self.assertEqual(step["with"]["ref"], "${{ env.EXPECTED_SHA }}")

    def test_checkout_refs_have_no_hardcoded_sha_fallback(self) -> None:
        for step in self.job["steps"]:
            ref = str(step.get("with", {}).get("ref", ""))
            self.assertNotRegex(ref, r"[0-9a-f]{40}")

    def _run_record_step(self, expected: str, actual: str) -> subprocess.CompletedProcess[str]:
        step = next(
            item for item in self.job["steps"]
            if item.get("name") == "Record and assert tested SHA"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "baseline").mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            git = fake_bin / "git"
            git.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "rev-parse" ]; then printf "%s\\n" "$FAKE_HEAD"; exit 0; fi\n'
                'if [ "$1" = "log" ]; then printf "commit %s\\n" "$FAKE_HEAD"; exit 0; fi\n'
                "exit 2\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "EXPECTED_SHA": expected,
                "FAKE_HEAD": actual,
                "GITHUB_WORKSPACE": str(root),
                "GITHUB_EVENT_NAME": "pull_request",
                "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            })
            return subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_record_step_fails_closed_when_checkout_sha_mismatches(self) -> None:
        expected, actual = "a" * 40, "b" * 40
        result = self._run_record_step(expected, actual)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"expected_sha={expected} tested_sha={actual}", result.stdout)

    def test_record_step_accepts_and_records_matching_sha(self) -> None:
        expected = "a" * 40
        result = self._run_record_step(expected, expected)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"expected_sha={expected} tested_sha={expected}", result.stdout)

    def test_historic_fallback_regression_is_rejected(self) -> None:
        # Reverting EXPECTED_SHA to the old inputs.sha || literal fallback makes
        # setUpClass fail to find EXPECTED_SHA; removing it also fails the checkout tests.
        self.assertNotIn("github.event.inputs.sha || 'd2039f6b5d1f8b52ffa5a58754b46ff3f6b1a675'", self.text)
        self.assertEqual(self.expected, "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.event.inputs.sha }}")

    @classmethod
    def tearDownClass(cls) -> None:
        # Kept explicit so suite integrations that subclass this test remain predictable.
        super().tearDownClass()


if __name__ == "__main__":
    unittest.main()
