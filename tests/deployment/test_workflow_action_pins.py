"""Exercise the workflow policy with real workflow copies and unsafe mutations."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkflowActionPinsTests(unittest.TestCase):
    def run_check(self, mutation=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / '.github/workflows', root / '.github/workflows')
            if mutation:
                mutation(root / '.github/workflows/ci.yml')
            return subprocess.run(
                [sys.executable, str(ROOT / 'tests/deployment/check_workflows.py')],
                cwd=root, capture_output=True, text=True, check=False,
            )

    def test_pinned_workflows_pass(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutable_step_tag_is_rejected(self):
        def mutate(path):
            text = path.read_text()
            text = re.sub(r'actions/checkout@[0-9a-f]{40}', 'actions/checkout@v4', text, count=1)
            path.write_text(text)
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('external action must use a full commit SHA', result.stderr)

    def test_short_sha_is_rejected(self):
        def mutate(path):
            text = path.read_text()
            text = re.sub(r'actions/checkout@[0-9a-f]{40}', 'actions/checkout@11d5960', text, count=1)
            path.write_text(text)
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('external action must use a full commit SHA', result.stderr)

    def test_mutable_reusable_workflow_is_rejected(self):
        def mutate(path):
            with path.open('a') as stream:
                stream.write('\n  unsafe-reusable:\n    uses: example/repo/.github/workflows/test.yml@main\n')
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('external action must use a full commit SHA', result.stderr)

    def test_unapproved_upload_artifact_pin_is_rejected(self):
        def mutate(path):
            text = path.read_text()
            text = re.sub(r'actions/upload-artifact@[0-9a-f]{40}', 'actions/upload-artifact@' + '1' * 40, text, count=1)
            path.write_text(text)
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('artifact pin mismatch: upload-artifact', result.stderr)

    def test_unapproved_download_artifact_pin_is_rejected(self):
        def mutate(path):
            for workflow in path.parent.glob('*.yml'):
                text = workflow.read_text()
                text = re.sub(r'actions/download-artifact@[0-9a-f]{40}', 'actions/download-artifact@' + '2' * 40, text)
                workflow.write_text(text)
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('artifact pin mismatch: download-artifact', result.stderr)

    def test_older_artifact_pair_in_a_new_workflow_is_rejected(self):
        def mutate(path):
            (path.parent / 'new-workflow.yml').write_text(
                'name: new\non: workflow_dispatch\npermissions: {}\njobs:\n  a:\n    runs-on: ubuntu-latest\n'
                '    steps:\n      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n'
            )
        result = self.run_check(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('artifact pin mismatch: upload-artifact', result.stderr)


if __name__ == '__main__':
    unittest.main()
