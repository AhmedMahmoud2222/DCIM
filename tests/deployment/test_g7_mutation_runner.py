"""Unit tests for the G7 mutation runner's detection classifier."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[2] / ".github" / "g7_mutation_runner.py"
_spec = importlib.util.spec_from_file_location("g7_mutation_runner", _PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

REAL_FAILURE = (
    "E   AssertionError: assert 'pinned' == 'inferred'\n"
    f"FAILED {runner.TEST}::test_case_01 - AssertionError\n"
    "1 failed, 19 passed in 4.2s\n"
)


class MutationClassifierTests(unittest.TestCase):
    def test_assertion_failure_is_detected(self) -> None:
        self.assertTrue(runner.is_assertion_failure(1, REAL_FAILURE))

    def test_internal_error_is_rejected(self) -> None:
        self.assertFalse(runner.is_assertion_failure(3, "INTERNALERROR> RuntimeError: plugin crashed"))
        self.assertFalse(runner.is_assertion_failure(1, "INTERNALERROR> " + REAL_FAILURE))

    def test_other_exit_codes_are_rejected(self) -> None:
        for code in (2, 4, 5, 124):
            self.assertFalse(runner.is_assertion_failure(code, REAL_FAILURE))

    def test_setup_errors_are_rejected(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, REAL_FAILURE + "OperationalError: refused"))

    def test_failure_without_parity_test_or_assertion_is_rejected(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, "FAILED tests/other.py::t - AssertionError"))
        self.assertFalse(runner.is_assertion_failure(1, f"FAILED {runner.TEST}::t - ValueError"))

