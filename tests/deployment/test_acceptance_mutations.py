"""Weaken actual gate controls in disposable copies; require assertion failures."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MUTANTS = (
    ('review SHA binding', 'acceptance_core.py', 'isinstance(value, dict) and value == expected', 'isinstance(value, dict)'),
    ('provider App identity', 'acceptance_core.py', "app.get('id') == provider['app_id']", 'True'),
    ('Actions App identity', 'acceptance_core.py', "check.get('app', {}).get('id') == policy['actions_app']['id']", 'True'),
    ('signed range', 'acceptance_core.py', "v.get('reason') == 'valid', 'invalid commit signature'", "True, 'invalid commit signature'"),
    ('owner actor', 'acceptance_core.py', "run.get(actor, {}).get('id') == policy['owner']['id']", 'True'),
    ('trusted workflow revision', 'acceptance_core.py', "and run.get('head_sha') == trust_sha", ''),
    ('source execution outcome', 'acceptance_core.py', "run.get('status') == 'completed' and run.get('conclusion') == 'success'", 'True'),
    ('open findings', 'acceptance_gate.py', "review['review']['verdict'] == 'ACCEPT' and not outstanding", 'True'),
    ('duplicate artifacts', 'acceptance_api.py', "len(matches) == 1, 'missing/duplicate evidence artifact'", "len(matches) >= 1, 'missing/duplicate evidence artifact'"),
    ('workflow source bytes', 'acceptance_api.py', "self.file(path, sha) == (self.root / path).read_bytes()", 'True'),
)


class AcceptanceMutationTests(unittest.TestCase):
    def test_real_gate_mutants_are_caught_without_infrastructure_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / '.github', root / '.github',
                            ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
            tests = root / 'tests/deployment'; tests.mkdir(parents=True)
            shutil.copyfile(ROOT / 'tests/deployment/test_acceptance_gate.py', tests / 'test_acceptance_gate.py')
            shutil.copytree(ROOT / 'tests/deployment/opencode_validation', tests / 'opencode_validation',
                            ignore=shutil.ignore_patterns('__pycache__'))
            self.assertEqual(self.run_suite(root).returncode, 0)
            for name, filename, old, new in MUTANTS:
                with self.subTest(control=name):
                    path = root / '.github/scripts' / filename
                    original = path.read_text()
                    self.assertEqual(original.count(old), 1, name)
                    modified = original.replace(old, new, 1)
                    compile(modified, filename, 'exec')
                    path.write_text(modified)
                    try:
                        result = self.run_suite(root)
                        self.assertNotEqual(result.returncode, 0, name)
                        self.assertIn('failures=', result.stdout, result.stdout)
                        self.assertNotIn('errors=', result.stdout, result.stdout)
                    finally:
                        path.write_text(original)

    @staticmethod
    def run_suite(root):
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                               '-s', 'tests/deployment', '-p', 'test_acceptance_gate.py'], cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45)
