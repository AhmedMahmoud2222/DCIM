"""Tracked text files hold no raw Unicode bidirectional control character (issue #332).

U+202A to U+202E and U+2066 to U+2069 reorder the text an editor or a diff view shows, so a
reviewer can read a different line than the interpreter runs (Trojan Source, CVE-2021-42574).
A test that needs one of these characters as data writes it as an escape such as "\\u202e".
"""

from __future__ import annotations

import subprocess
import unittest

from ci_support import ROOT

BIDI_CONTROLS = tuple(chr(point) for point in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def raw_bidi_controls(data: bytes) -> list[str]:
    """`line N: U+XXXX` for each raw control in a UTF-8 text file; empty for binary files."""
    if b"\0" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [
        f"line {number}: U+{ord(char):04X}"
        for number, line in enumerate(text.splitlines(), 1)
        for char in line
        if char in BIDI_CONTROLS
    ]


class BidiControlTests(unittest.TestCase):
    def test_scanner_reports_each_control_in_both_ranges(self):
        self.assertEqual(len(BIDI_CONTROLS), 9)
        for char in BIDI_CONTROLS:
            self.assertEqual(raw_bidi_controls(f"ok\nx{char}y\n".encode()), [f"line 2: U+{ord(char):04X}"])

    def test_scanner_ignores_escapes_neighbouring_code_points_and_binary_files(self):
        self.assertEqual(raw_bidi_controls(b'name = "n\\u202e"\n'), [])
        self.assertEqual(raw_bidi_controls("\u2029\u202f\u2065\u206a\u200f".encode()), [])
        self.assertEqual(raw_bidi_controls(b"\x89PNG\0" + "\u202e".encode()), [])

    def test_tracked_text_files_hold_no_raw_bidi_control(self):
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode().split("\0")
        tracked = [ROOT / name for name in listed if name and (ROOT / name).is_file()]
        self.assertGreater(len(tracked), 100)
        found = {
            str(path.relative_to(ROOT)): hits for path in tracked if (hits := raw_bidi_controls(path.read_bytes()))
        }
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
