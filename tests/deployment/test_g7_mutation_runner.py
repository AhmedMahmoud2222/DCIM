"""Unit tests for the G7 mutation runner's detection classifier."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[2] / ".github" / "g7_mutation_runner.py"
_spec = importlib.util.spec_from_file_location("g7_mutation_runner", _PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

ASSERTION_TRACE = "E   AssertionError: assert 'pinned' == 'inferred'\n"
FAILURE_SUMMARY = (
    f"FAILED {runner.TEST}::test_provenance_python_sql_parity_postgres - AssertionError\n"
    "1 failed, 19 passed in 4.2s\n"
)
REAL_FAILURE = ASSERTION_TRACE + FAILURE_SUMMARY
FIXTURE_WARNING = (
    "=============================== warnings summary ===============================\n"
    "tests/integration/test_g7_provenance_parity_postgres.py::test_provenance_python_sql_parity_postgres\n"
    "  /opt/hostedtoolcache/Python/3.12.*/site-packages/pytest_asyncio/plugin.py:216:\n"
    "    PytestDeprecationWarning: The configuration option \"asyncio_default_fixture_loop_scope\" is unset.\n"
    "    The event loop scope for asynchronous fixtures will default to the fixture caching scope.\n"
)


def failed(test: str, kind: str = "AssertionError") -> str:
    return (
        f"E   {kind}: simulated test failure\n"
        f"FAILED {test}::test_example - {kind}\n"
        "1 failed, 19 passed in 4.2s\n"
    )


class MutationClassifierTests(unittest.TestCase):
    def test_genuine_parity_assertion_failure_without_warnings_is_detected(self) -> None:
        self.assertTrue(runner.is_assertion_failure(1, REAL_FAILURE))

    def test_fixture_deprecation_warning_does_not_mask_parity_assertion(self) -> None:
        self.assertTrue(
            runner.is_assertion_failure(1, ASSERTION_TRACE + FIXTURE_WARNING + FAILURE_SUMMARY)
        )

    def test_fixture_setup_failure_is_inconclusive(self) -> None:
        output = (
            "ERROR at setup of test_provenance_python_sql_parity_postgres\n"
            "E   RuntimeError: fixture initialization failed\n"
            + REAL_FAILURE
        )
        self.assertFalse(runner.is_assertion_failure(1, output))

    def test_missing_fixture_is_inconclusive(self) -> None:
        output = (
            "E   fixture 'postgres_db' not found\n"
            f"ERROR {runner.TEST}::test_provenance_python_sql_parity_postgres\n"
            + REAL_FAILURE
        )
        self.assertFalse(runner.is_assertion_failure(1, output))

    def test_collection_error_is_inconclusive(self) -> None:
        output = "ERROR collecting tests/integration/test_g7_provenance_parity_postgres.py\n" + REAL_FAILURE
        self.assertFalse(runner.is_assertion_failure(1, output))

    def test_internal_error_and_plugin_failure_are_inconclusive(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, "INTERNALERROR> RuntimeError: plugin crashed\n" + REAL_FAILURE))
        plugin_error = "pluggy._manager.PluginValidationError: plugin raised an error\n" + REAL_FAILURE
        self.assertFalse(runner.is_assertion_failure(1, plugin_error))

    def test_postgres_operational_error_is_inconclusive(self) -> None:
        output = "E   sqlalchemy.exc.OperationalError: connection refused\n" + REAL_FAILURE
        self.assertFalse(runner.is_assertion_failure(1, output))

    def test_unrelated_test_assertion_is_inconclusive(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, failed("tests/other/test_other.py")))

    def test_non_assertion_parity_failure_is_inconclusive(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, failed(runner.TEST, "RuntimeError")))

    def test_other_exit_codes_are_inconclusive(self) -> None:
        for code in (0, 2, 3, 4, 5, 124, 137):
            self.assertFalse(runner.is_assertion_failure(code, REAL_FAILURE))

    def test_setup_errors_are_rejected(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, REAL_FAILURE + "E   OperationalError: refused"))

    def test_failure_without_parity_test_or_assertion_is_rejected(self) -> None:
        self.assertFalse(runner.is_assertion_failure(1, "FAILED tests/other.py::t - AssertionError"))
        self.assertFalse(runner.is_assertion_failure(1, f"FAILED {runner.TEST}::t - ValueError"))

