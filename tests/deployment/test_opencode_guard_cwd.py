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
            process = types.SimpleNamespace(returncode=1, communicate=lambda timeout=None: ('', ''))
            with patch.object(guard.subprocess, 'Popen', return_value=process) as popen, \
                    patch.object(guard, 'reap_agent_processes', return_value=''):
                exit_code = guard.main([
                    'opencode_guard.py', str(result), 'model', str(config),
                    str(Path(tmp) / 'home'), workdir, '1',
                ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(popen.call_args.kwargs['cwd'], workdir)
            self.assertNotEqual(popen.call_args.kwargs['cwd'], str(Path.cwd()))
            # v2 has no --dir flag: the work tree is selected by cwd (checked above) and by $PWD
            self.assertIn(f'PWD={workdir}', popen.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
