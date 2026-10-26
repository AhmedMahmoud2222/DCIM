"""Run negative tests against weakened production controls in disposable copies."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MUTANTS = (
    ('bot numeric identity', 'agent_authority.py', "user['id'] == item['id']", 'True',
     'test_agent_authority.py'),
    ('bot app identity', 'agent_authority.py', "app['id'] == item['app_id']", 'True',
     'test_agent_authority.py'),
    ('current HEAD', 'agent_authority.py',
     "if not (comment.get('body') or '').endswith('\\nDCIM-Head: ' + head):", 'if False:',
     'test_agent_authority.py'),
    ('atomic duplicate claim', 'agent_authority.py', "if post(comment_id, 'eyes') != 201:",
     "if post(comment_id, 'eyes') not in (200, 201):", 'test_agent_authority.py'),
    ('independent reviewer', 'orchestration_policy.py',
     "or facts.get('implementer') == review.get('reviewer'):", 'or False:',
     'test_orchestration_policy.py'),
    ('current review', 'orchestration_policy.py',
     "if review.get('head') != head or review.get('base') != base:", 'if False:',
     'test_orchestration_policy.py'),
    ('valid signature reason', 'orchestration_policy.py',
     "or c.get('verification', {}).get('reason') != 'valid' for c in commits):",
     'or False for c in commits):', 'test_orchestration_policy.py'),
)


class AgentMutationTests(unittest.TestCase):
    def test_negative_cases_detect_weakened_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / '.github/scripts', root / '.github/scripts',
                            ignore=shutil.ignore_patterns('__pycache__'))
            tests = root / 'tests/deployment'; tests.mkdir(parents=True)
            shutil.copytree(ROOT / 'tests/deployment/fixtures/agent_comments',
                            tests / 'fixtures/agent_comments')
            for pattern in {'test_agent_authority.py', 'test_orchestration_policy.py'}:
                shutil.copyfile(ROOT / 'tests/deployment' / pattern, tests / pattern)
            # Establish that the exact disposable test environment passes first.
            for pattern in {'test_agent_authority.py', 'test_orchestration_policy.py'}:
                result = self.run_tests(root, pattern)
                self.assertEqual(result.returncode, 0, result.stdout)
            for name, filename, old, new, pattern in MUTANTS:
                with self.subTest(control=name):
                    path = root / '.github/scripts' / filename
                    original = path.read_text()
                    self.assertEqual(original.count(old), 1, name)
                    mutated = original.replace(old, new, 1)
                    compile(mutated, filename, 'exec')
                    path.write_text(mutated)
                    try:
                        result = self.run_tests(root, pattern)
                        self.assertNotEqual(result.returncode, 0, name)
                        self.assertIn('failures=', result.stdout, result.stdout)
                        self.assertNotIn('errors=', result.stdout, result.stdout)
                    finally:
                        path.write_text(original)

    @staticmethod
    def run_tests(root, pattern):
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                               '-s', 'tests/deployment', '-p', pattern], cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=30)
