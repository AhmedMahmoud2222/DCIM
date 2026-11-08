"""Regression tests for the refresh-branch workflow (#242).

F3: the active workflow and its proposed copy must not drift; only line 1 may differ.
F6/F8: the gate step authorizes exactly one case-insensitive actor match and the
job-level pre-filter can only skip a run, never grant one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ROOT / ".github" / "workflows" / "refresh-branch.yml"
PROPOSED = ROOT / "proposed-workflows" / "refresh-branch.yml"
SHA = "a" * 40


def _gate_script() -> str:
    workflow = yaml.safe_load(ACTIVE.read_text())
    steps = workflow["jobs"]["gate"]["steps"]
    return next(step["run"] for step in steps if step.get("id") == "check")


def _run_gate(allowed: str, actor: str, body: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        script = Path(tmp) / "gate.sh"
        script.write_text(_gate_script())
        env = {
            "PATH": os.environ["PATH"],
            "ALLOWED": allowed,
            "ACTOR": actor,
            "COMMENT_BODY": body,
            "GITHUB_OUTPUT": str(out),
        }
        subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
        return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


class RefreshBranchSyncTests(unittest.TestCase):
    def test_active_and_proposed_differ_only_on_line_one(self) -> None:
        active = ACTIVE.read_text().split("\n", 1)
        proposed = PROPOSED.read_text().split("\n", 1)
        self.assertEqual(active[1], proposed[1])


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq and bash required")
class RefreshBranchGateTests(unittest.TestCase):
    def test_exact_actor_is_allowed(self) -> None:
        out = _run_gate('["Alice"]', "Alice", f"/refresh-branch {SHA}")
        self.assertEqual(out["allowed"], "true")

    def test_actor_case_difference_is_allowed(self) -> None:
        out = _run_gate('["Alice"]', "alice", f"/refresh-branch {SHA}")
        self.assertEqual(out["allowed"], "true")

    def test_other_actor_is_denied(self) -> None:
        out = _run_gate('["Alice"]', "mallory", f"/refresh-branch {SHA}")
        self.assertEqual(out["allowed"], "false")

    def test_duplicate_entries_differing_by_case_fail_closed(self) -> None:
        out = _run_gate('["Alice","alice"]', "ALICE", f"/refresh-branch {SHA}")
        self.assertEqual(out["allowed"], "false")

    def test_non_array_allowlist_is_denied(self) -> None:
        out = _run_gate('"alice"', "alice", f"/refresh-branch {SHA}")
        self.assertEqual(out["allowed"], "false")

    def test_marker_does_not_grant_access(self) -> None:
        body = f"<!-- orchestrator -->\n/refresh-branch {SHA}"
        denied = _run_gate('["Alice"]', "mallory", body)
        self.assertEqual((denied["allowed"], denied["marked"]), ("false", "true"))
        allowed = _run_gate('["Alice"]', "alice", body)
        self.assertEqual((allowed["allowed"], allowed["marked"]), ("true", "true"))

    def test_malformed_command_is_skipped(self) -> None:
        out = _run_gate('["Alice"]', "alice", "/refresh-branch abc123")
        self.assertEqual((out["allowed"], out["marked"]), ("false", "false"))


class RefreshBranchPrefilterTests(unittest.TestCase):
    def test_prefilter_is_a_job_condition_not_a_grant(self) -> None:
        workflow = yaml.safe_load(ACTIVE.read_text())
        condition = workflow["jobs"]["gate"]["if"]
        self.assertIn("contains(vars.REFRESH_ALLOWED_ACTORS, github.event.comment.user.login)", condition)
        # The authorizing decision stays in the step, and the secret job depends on it.
        self.assertEqual(workflow["jobs"]["refresh"]["if"], "needs.gate.outputs.allowed == 'true'")
        self.assertEqual(workflow["jobs"]["gate"]["permissions"], {})


if __name__ == "__main__":
    unittest.main()
