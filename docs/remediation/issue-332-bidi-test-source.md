# Issue #332 — make hostile Unicode evidence auditable

**Status: implementation pending. This file is a handoff, not a fix.**

Owner issue: https://github.com/AhmedMahmoud2222/DCIM/issues/332

## Verified defect
`backend/tests/unit/test_neighbor_evidence.py` contains literal U+202E in a test data string. The test needs the bidi character to exercise sanitization, but embedding it raw can reorder reviewer/editor display.

## Implementation contract
- Replace the raw code point with the explicit Python escape sequence `\\u202e` **inside the string literal**, preserving the Unicode value delivered to `parse_neighbor`.
- Do **not** delete the bidi input or weaken the test assertions. Review the original issue's suggested replacement carefully: removing the character changes the coverage.
- Add an appropriate source check that detects raw bidi control characters in tracked code while allowing explicit escape sequences.
- Verify this test and relevant neighbor ingestion tests pass; show both before/after runtime strings are identical.

## Acceptance gate
- No raw U+202E in the fixture source; actual test data still contains U+202E at runtime.
- Independent review on this same PR; no new review PR.
- CI and tests green before merge; planning-only commit does not confer readiness.
