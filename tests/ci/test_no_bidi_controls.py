"""Files in the checkout hold no raw Unicode bidirectional control character (issue #332).

U+202A to U+202E and U+2066 to U+2069 reorder the text an editor or a diff view shows, so a
reviewer can read a different line than the interpreter runs (Trojan Source, CVE-2021-42574).
A test that needs one of these characters as data writes it as an escape such as "\\u202e".

The scan walks the checkout and skips Git's own directory and the generated directories that
.gitignore names. In CI that is the set of tracked files; a local run also covers untracked files.
Controls are matched in the raw bytes, so a NUL byte or invalid UTF-8 elsewhere in a file
cannot hide one.
"""

from __future__ import annotations

import os
import re
import unittest

from ci_support import ROOT

BIDI_CONTROLS = tuple(chr(point) for point in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
BIDI_CONTROL_PATTERN = re.compile(
    b"|".join(re.escape(char.encode("utf-8")) for char in BIDI_CONTROLS)
)
SKIPPED_DIRS = frozenset({
    ".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist",
    "test-results", "playwright-report", "blob-report",
})


def checkout_files():
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIPPED_DIRS and not name.endswith(".egg-info")]
        for name in files:
            path = ROOT / current / name
            if path.is_file():
                yield path


def raw_bidi_controls(data: bytes) -> list[str]:
    """`line N: U+XXXX` for each raw control in a file; binary files are not exempt."""
    return [
        f"line {number}: U+{ord(match.group().decode('utf-8')):04X}"
        for number, line in enumerate(data.splitlines(), 1)
        for match in BIDI_CONTROL_PATTERN.finditer(line)
    ]


class BidiControlTests(unittest.TestCase):
    def test_scanner_reports_each_control_in_both_ranges(self):
        self.assertEqual(len(BIDI_CONTROLS), 9)
        for char in BIDI_CONTROLS:
            self.assertEqual(raw_bidi_controls(f"ok\nx{char}y\n".encode()), [f"line 2: U+{ord(char):04X}"])

    def test_scanner_ignores_escapes_and_neighbouring_code_points(self):
        self.assertEqual(raw_bidi_controls(b'name = "n\\u202e"\n'), [])
        self.assertEqual(raw_bidi_controls("\u2029\u202f\u2065\u206a\u200f".encode()), [])

    def test_scanner_detects_control_in_nul_and_invalid_utf8_files(self):
        self.assertEqual(
            raw_bidi_controls(b"plain\0text\n" + "\u202e".encode() + b"\n"),
            ["line 2: U+202E"],
        )
        self.assertEqual(
            raw_bidi_controls(b"\x89PNG\0" + "\u202e".encode()),
            ["line 1: U+202E"],
        )

    def test_checkout_files_hold_no_raw_bidi_control(self):
        scanned = list(checkout_files())
        self.assertIn(ROOT / "backend" / "tests" / "unit" / "test_neighbor_evidence.py", scanned)
        self.assertGreater(len(scanned), 1000)
        found = {
            str(path.relative_to(ROOT)): hits for path in scanned if (hits := raw_bidi_controls(path.read_bytes()))
        }
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
