"""Tests for the CI test-file sharding (.github/scripts/ci_shard.py).

The shards of one selection must cover every test file exactly once. A file that no
shard runs would pass CI without being tested, so these tests fail on any gap.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci_support import ROOT, SCRIPTS, load_module

shard = load_module("ci_shard", SCRIPTS / "ci_shard.py")

RETENTION = "tests/integration/test_mvp_retention_hostile.py"
EXTRACTION = ("tests/api/test_catalog_extraction_apply.py", "tests/unit/test_catalog_apply_conversion.py")
COUNTS = (1, 2, 3, 4, 5, 8)


class RepoShardTests(unittest.TestCase):
    """The real backend/tests tree."""

    @classmethod
    def setUpClass(cls):
        import os

        cls.cwd = os.getcwd()
        os.chdir(ROOT / "backend")
        cls.all_files = shard.discover(Path("tests"))

    @classmethod
    def tearDownClass(cls):
        import os

        os.chdir(cls.cwd)

    def selection(self, count):
        return shard.select(Path("tests"), count, [RETENTION], [RETENTION, *EXTRACTION])

    def test_named_special_files_exist(self):
        for name in (RETENTION, *EXTRACTION):
            self.assertIn(name, self.all_files)

    def test_every_count_covers_every_file_exactly_once(self):
        expected = sorted(set(self.all_files) - {RETENTION, *EXTRACTION})
        for count in COUNTS:
            with self.subTest(count=count):
                shards = self.selection(count)
                flat = [path for part in shards for path in part]
                self.assertEqual(len(flat), len(set(flat)), "a file is in two shards")
                self.assertEqual(sorted(flat), expected)
                self.assertTrue(all(shards), "a shard is empty")

    def test_special_files_are_in_no_shard(self):
        flat = {path for part in self.selection(4) for path in part}
        for name in (RETENTION, *EXTRACTION):
            self.assertNotIn(name, flat)

    def test_selection_is_deterministic(self):
        self.assertEqual(self.selection(4), self.selection(4))

    def test_shard_zero_is_lighter_because_it_runs_the_special_steps(self):
        shards = self.selection(4)
        loads = [sum(shard.weight(path) for path in part) for part in shards]
        special = sum(shard.weight(name) for name in (RETENTION, *EXTRACTION))
        loads[0] += special
        # Within the size of the largest single file, the loads are even.
        biggest = max(shard.weight(path) for path in self.all_files)
        self.assertLessEqual(max(loads) - min(loads), biggest)

    def test_a_dropped_file_is_detected(self):
        shards = self.selection(4)
        expected = sorted(set(self.all_files) - {RETENTION, *EXTRACTION})
        shards[2] = shards[2][1:]  # mutation: one shard loses a file
        flat = sorted(path for part in shards for path in part)
        self.assertNotEqual(flat, expected)

    def test_command_line_prints_only_this_shards_files(self):
        script = SCRIPTS / "ci_shard.py"
        base = [sys.executable, str(script), "--count", "4", "--root", "tests", "--exclude", RETENTION,
                "--preload", RETENTION, "--preload", EXTRACTION[0], "--preload", EXTRACTION[1]]
        outputs = []
        for index in range(4):
            result = subprocess.run([*base, "--index", str(index)], cwd=ROOT / "backend",
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout.split())
        self.assertEqual(outputs, [list(part) for part in self.selection(4)])


class SyntheticShardTests(unittest.TestCase):
    """A small tree, so that each rule is checked on known input."""

    def make_tree(self, names):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, size in names.items():
            path = root / "tests" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x" * size)
        return root

    def run_script(self, root, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "ci_shard.py"), "--root", "tests", *args],
                              cwd=root, capture_output=True, text=True, check=False)

    def test_discovery_matches_the_pytest_defaults(self):
        root = self.make_tree({"test_a.py": 1, "b_test.py": 1, "helper.py": 1, "conftest.py": 1,
                               "sub/test_c.py": 1, "__pycache__/test_d.py": 1})
        found = shard.discover(root / "tests")
        self.assertEqual(found, sorted(found))
        self.assertEqual({Path(path).name for path in found}, {"b_test.py", "test_a.py", "test_c.py"})
        self.assertEqual(len(found), 3)

    def test_empty_shard_fails_instead_of_selecting_everything(self):
        root = self.make_tree({"test_a.py": 5, "test_b.py": 5})
        result = self.run_script(root, "--index", "0", "--count", "3")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("a shard is empty", result.stderr)

    def test_index_outside_the_count_fails(self):
        root = self.make_tree({"test_a.py": 5, "test_b.py": 5})
        for index in ("-1", "2", "9"):
            with self.subTest(index=index):
                result = self.run_script(root, "--index", index, "--count", "2")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")

    def test_a_named_file_that_does_not_exist_fails(self):
        root = self.make_tree({"test_a.py": 5, "test_b.py": 5})
        for flag in ("--exclude", "--preload"):
            with self.subTest(flag=flag):
                result = self.run_script(root, "--index", "0", "--count", "2", flag, "tests/test_gone.py")
                self.assertEqual(result.returncode, 1)
                self.assertIn("not a discovered test file", result.stderr)

    def test_an_empty_selection_fails(self):
        root = self.make_tree({"test_a.py": 5})
        result = self.run_script(root, "--index", "0", "--count", "1", "--exclude", "tests/test_a.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("selection is empty", result.stderr)

    def test_a_missing_root_fails(self):
        root = self.make_tree({"test_a.py": 5})
        result = subprocess.run([sys.executable, str(SCRIPTS / "ci_shard.py"), "--root", "nope", "--index", "0",
                                 "--count", "1"], cwd=root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)

    def test_the_heaviest_files_land_in_different_shards(self):
        root = self.make_tree({"test_big1.py": 100, "test_big2.py": 100, "test_small1.py": 1, "test_small2.py": 1})
        shards = shard.select(root / "tests", 2, [], [])
        names = [{Path(path).name for path in part} for part in shards]
        self.assertTrue(all(len(part & {"test_big1.py", "test_big2.py"}) == 1 for part in names))

    def test_preload_pushes_work_off_shard_zero(self):
        root = self.make_tree({"test_pre.py": 25, "test_a.py": 10, "test_b.py": 10, "test_c.py": 10, "test_d.py": 10})
        shards = shard.select(root / "tests", 2, [], [str(root / "tests" / "test_pre.py")])
        self.assertGreater(len(shards[1]), len(shards[0]))


if __name__ == "__main__":
    unittest.main()
