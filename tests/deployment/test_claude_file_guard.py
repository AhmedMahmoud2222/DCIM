"""Adversarial tests for .github/scripts/claude_file_guard.py (the model file-tool boundary)."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'claude_file_guard', Path(__file__).resolve().parents[2] / '.github/scripts/claude_file_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class FileGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = base / 'pr'
        (self.root / 'src').mkdir(parents=True)
        (self.root / '.git').mkdir()
        (self.root / '.github/workflows').mkdir(parents=True)
        (self.root / 'src/a.py').write_text('x')
        (base / 'secret').write_text('token')
        os.symlink(base / 'secret', self.root / 'src/link')
        os.symlink(base, self.root / 'src/dirlink')

    def ok(self, tool, **args):
        return guard.allowed({'tool_name': tool, 'tool_input': args}, self.root)

    def test_happy_paths(self):
        self.assertTrue(self.ok('Read', file_path=str(self.root / 'src/a.py')))
        self.assertTrue(self.ok('Read', file_path='src/a.py'))
        self.assertTrue(self.ok('Write', file_path=str(self.root / 'src/new.py'), content='y'))
        self.assertTrue(self.ok('Edit', file_path='src/a.py', old_string='x', new_string='z'))
        self.assertTrue(self.ok('Glob', pattern='**/*.py', path=str(self.root)))
        self.assertTrue(self.ok('Grep', pattern='x', path=str(self.root / 'src')))

    def test_outside_root_and_traversal(self):
        for p in ('/etc/passwd', '/proc/self/environ', '../secret', 'src/../../secret',
                  str(self.root.parent / 'secret'), '/home/runner/.config/gh/hosts.yml'):
            for tool in ('Read', 'Write'):
                self.assertFalse(self.ok(tool, file_path=p), (tool, p))

    def test_symlink_escape(self):
        self.assertFalse(self.ok('Read', file_path='src/link'))
        self.assertFalse(self.ok('Write', file_path='src/link', content='y'))
        self.assertFalse(self.ok('Read', file_path='src/dirlink/secret'))
        self.assertFalse(self.ok('Write', file_path='src/dirlink/new', content='y'))
        self.assertFalse(self.ok('Grep', pattern='t', path='src/dirlink'))

    def test_protected_paths(self):
        for p in ('.git/config', '.claude/settings.json', '.mcp.json', 'a/.git/HEAD'):
            self.assertFalse(self.ok('Read', file_path=p), p)
        for p in ('.github/workflows/x.yml', 'CLAUDE.md', 'sub/claude.md'):
            self.assertFalse(self.ok('Write', file_path=p, content='y'), p)
        self.assertTrue(self.ok('Read', file_path='.github/workflows'.rstrip('/') + '/../src/a.py') is True)

    def test_glob_grep_need_explicit_safe_path(self):
        self.assertFalse(self.ok('Glob', pattern='**/*'))
        self.assertFalse(self.ok('Grep', pattern='x'))
        self.assertFalse(self.ok('Glob', pattern='/etc/*', path=str(self.root)))
        self.assertFalse(self.ok('Glob', pattern='../*', path=str(self.root)))
        self.assertFalse(self.ok('Grep', pattern='x', path='/', glob='*'))
        self.assertFalse(self.ok('Grep', pattern='x', path=str(self.root), glob='../*'))

    def test_unknown_tools_args_and_malformed_events(self):
        self.assertFalse(self.ok('Bash', command='env'))
        self.assertFalse(self.ok('WebFetch', url='http://x'))
        self.assertFalse(self.ok('Read', file_path='src/a.py', extra=1))
        self.assertFalse(self.ok('Read', file_path='src/a\x00.py'))
        self.assertFalse(self.ok('Read', file_path=''))
        self.assertFalse(self.ok('Read', file_path=['src/a.py']))
        for bad in (None, [], 'x', {}, {'tool_name': 'Read'}, {'tool_name': 'Read', 'tool_input': 'x'},
                    {'tool_name': 'Read', 'tool_input': {'file_path': 'src/a.py'},
                     'hook_event_name': 'PostToolUse'}):
            self.assertFalse(guard.allowed(bad, self.root))


if __name__ == '__main__':
    unittest.main()
