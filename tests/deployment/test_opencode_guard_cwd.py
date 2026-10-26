"""Regression coverage for the OpenCode guard's subprocess working directory."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
guard = importlib.import_module('opencode_guard')


class GuardWorkingDirectoryTests(unittest.TestCase):
    def test_open_code_subprocess_starts_in_authorized_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = str(Path(tmp) / 'sandbox')
            Path(workdir).mkdir()
            config = Path(tmp) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            result = Path(tmp) / 'result.json'
            completed = types.SimpleNamespace(stdout='', stderr='', returncode=1)
            with patch.object(guard.subprocess, 'run', return_value=completed) as run, \
                    patch.object(guard, 'reap_agent_processes', return_value=''):
                exit_code = guard.main([
                    'opencode_guard.py', str(result), 'model', str(config),
                    str(Path(tmp) / 'home'), workdir, '1',
                ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(run.call_args.kwargs['cwd'], workdir)
            self.assertNotEqual(run.call_args.kwargs['cwd'], str(Path.cwd()))


if __name__ == '__main__':
    unittest.main()
