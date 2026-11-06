"""Queue recovery for the Claude agent (tests v1.0).

GitHub keeps one pending job per concurrency group, so a newer request cancels a pending one after the gate
has already claimed its comment. The release job must hand that claim back, and only for cancellations.
"""
import importlib.util
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('claude_agent_helpers', HERE / 'test_claude_agent.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class ReleaseJobTests(unittest.TestCase):
    def setUp(self):
        self.wf = base.load_workflow()
        self.job = self.wf['jobs']['release']
        self.run = self.job['steps'][0]['run']

    def test_release_runs_only_for_authorized_requests_whose_queued_job_was_cancelled(self):
        condition = self.job['if']
        self.assertIn('always()', condition)
        self.assertIn("needs.gate.outputs.authorized == 'true'", condition)
        self.assertIn("needs.claude.result == 'cancelled'", condition)
        self.assertIn("needs.publish.result == 'cancelled'", condition)
        self.assertNotIn('failure', condition)  # a failed run keeps its claim
        self.assertEqual(self.job['needs'], ['gate', 'claude', 'publish'])

    def test_release_holds_no_secret_no_claude_and_no_pr_code(self):
        self.assertEqual(self.job['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        text = base.yaml.safe_dump(self.job)
        for forbidden in ('secrets.', 'anthropics/', 'actions/checkout', 'needs.gate.outputs.task',
                          'authorized_sha', 'head_ref'):
            self.assertNotIn(forbidden, text)
        self.assertNotIn('concurrency', self.job)  # the release itself must never be queued or replaced

    def test_release_only_removes_its_own_eyes_claim_and_validates_ids(self):
        self.assertIn('.content == "eyes" and .user.login == "github-actions[bot]"', self.run)
        self.assertIn('-X DELETE', self.run)
        self.assertRegex(self.run, r'\^\[0-9\]\+\$')
        env = self.job['steps'][0]['env']
        self.assertEqual(env['COMMENT_ID'], '${{ needs.gate.outputs.comment_id }}')
        self.assertEqual(env['ISSUE_NUMBER'], '${{ needs.gate.outputs.number }}')
        self.assertNotRegex(self.run, r'\$\{\{')  # nothing is interpolated into the script body

    def test_notice_does_not_look_like_a_command(self):
        body = re.search(r'body="([^"]+)"', self.run).group(1)
        self.assertFalse(body.startswith('@claude'))
        self.assertNotIn('@claude', body)

    def test_existing_isolation_still_holds(self):
        self.assertEqual(self.wf['permissions'], {})
        self.assertEqual(self.wf['jobs']['claude']['concurrency']['cancel-in-progress'], False)
        self.assertEqual(self.wf['jobs']['publish']['concurrency']['cancel-in-progress'], False)
        self.assertEqual(self.wf['jobs']['gate']['concurrency']['cancel-in-progress'], False)


if __name__ == '__main__':
    unittest.main()
