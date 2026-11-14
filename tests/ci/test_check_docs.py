"""Offline fixtures prove broken links fail without application dependencies."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/check_docs.py'
SPEC = importlib.util.spec_from_file_location('check_docs', SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class DocumentationTests(unittest.TestCase):
    def audit(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in files.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(value, encoding='utf-8')
            return checker.audit(root, list(files))

    def test_valid_local_links_images_titles_and_encoded_spaces(self):
        result = self.audit({'README.md': '[Guide](docs/a%20b.md#hello-world "title") ![image](assets/x.svg) [folder](docs/)',
                             'docs/a b.md': '# Hello *world*\n[home](../README.md)', 'assets/x.svg': '<svg/>'})
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['local_references_checked'], 4)

    def test_missing_file_wrong_relative_case_and_anchor_fail(self):
        for target in ['missing.md', '../guide.md', 'Guide.md', 'guide.md#absent']:
            with self.subTest(target=target):
                result = self.audit({'README.md': f'[bad]({target})', 'guide.md': '# Good'})
                self.assertEqual(len(result['errors']), 1)

    def test_reference_styles_and_undefined_label(self):
        result = self.audit({'README.md': '[full][g] [g][] [g]\n[g]: guide.md#ok\n[x][undefined]', 'guide.md': '# Ok'})
        self.assertEqual(result['local_references_checked'], 4)
        self.assertEqual(result['errors'][0]['reason'], 'undefined reference label')

    def test_html_anchors_and_links(self):
        result = self.audit({'README.md': '<a href="guide.md#custom">go</a><img src="missing.png">',
                             'guide.md': '<a id="custom"></a>'})
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['errors'][0]['target'], 'missing.png')

    def test_duplicate_setext_unicode_and_code_headings(self):
        text = '# Same\n# Same\n# Same-1\nHeading\n=======\n## `Café` API\n```\n# Hidden\n```'
        self.assertEqual(checker.anchors(text), {'same', 'same-1', 'same-1-1', 'heading', 'café-api'})

    def test_balanced_parentheses_and_angle_destination(self):
        result = self.audit({'README.md': '[one](doc(1).md) [two](<space name.md>)', 'doc(1).md': '', 'space name.md': ''})
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['local_references_checked'], 2)

    def test_code_comments_external_and_vendor_are_excluded(self):
        result = self.audit({'README.md': '```md\n[x](missing.md)\n```\n`[x](missing.md)`\n<!-- [x](missing.md) -->\n[x](https://example.test/x.md#x)\n[x](mailto:a@example.test)',
                             '.claude/skills/vendor/README.md': '[bad](missing.md)'})
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['external_references_skipped'], 2)
        self.assertEqual(len(result['excluded_documents']), 1)

    def test_untracked_file_cannot_hide_missing_tracked_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('[x](untracked.md)')
            (root / 'untracked.md').write_text('# Exists locally only')
            self.assertEqual(len(checker.audit(root, ['README.md'])['errors']), 1)

    def test_cli_exit_status_fails_on_deliberate_break(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'README.md').write_text('[bad](missing.md)')
            subprocess.run(['git', '-C', str(root), 'add', 'README.md'], check=True)
            result = subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), '--json'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('missing tracked target', result.stdout)

    def test_source_path_report_finds_missing_and_moved_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'docs').mkdir()
            (root / 'docs/documentation-moves.json').write_text('{"OLD.md": "docs/new.md"}')
            (root / 'code.py').write_text('# OLD.md and docs/missing.md; https://example.test/remote.md')
            result = checker.path_references(root, ['code.py', 'docs/documentation-moves.json'])
            self.assertEqual([x['reason'] for x in result], ['relocated literal path', 'unresolved literal path'])


if __name__ == '__main__':
    unittest.main()
