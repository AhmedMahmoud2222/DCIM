"""Trojan Source guard (issue #332).

Raw bidirectional control characters (U+202A to U+202E, U+2066 to U+2069) reorder text in
editors and diff views, which hides what a line of source does from a reviewer. Tests that
need such a character in their data must build it from its code point (``chr(0x202E)``)
or write it as an escape sequence in source.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Built from code points so this file never contains a raw control character itself.
BIDI_CONTROLS = frozenset(chr(cp) for cp in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def raw_bidi_hits(text: str) -> list[tuple[int, str]]:
    """Return (line number, code point label) for each raw bidi control in ``text``."""
    return [
        (number, f"U+{ord(char):04X}")
        for number, line in enumerate(text.splitlines(), start=1)
        for char in line
        if char in BIDI_CONTROLS
    ]


def test_detector_flags_raw_character_and_allows_escape_sequence():
    raw = "name = 'n" + chr(0x202E) + "'"
    escaped = "name = 'n" + chr(0x5C) + "u202e'"
    assert raw_bidi_hits(raw) == [(1, "U+202E")]
    assert raw_bidi_hits(escaped) == []


def test_fixture_built_from_code_point_keeps_the_control_character_at_runtime():
    fixture = "n" + chr(0x202E)
    assert [ord(char) for char in fixture] == [0x6E, 0x202E]
    assert len(fixture * 400) == 800
    assert raw_bidi_hits(fixture) == [(1, "U+202E")]


def test_tracked_text_files_hold_no_raw_bidi_controls():
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    offenders = []
    for raw_name in filter(None, listing):
        path = REPO_ROOT / raw_name.decode()
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary file
        offenders.extend(f"{path.relative_to(REPO_ROOT)}:{n} {label}" for n, label in raw_bidi_hits(text))
    assert offenders == []
