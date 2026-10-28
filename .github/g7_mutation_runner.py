#!/usr/bin/env python3
"""Deterministic G7 mutation experiments (M1-M4).

Each mutation edits backend/app/api/v1/telemetry.py in place, runs the
unmodified 20-case parity oracle, then restores the original file. A mutation
is only "detected" when the parity oracle exits non-zero with a real
classification assertion failure (not a setup/import error).

Usage: python .github/g7_mutation_runner.py <m1|m2|m3|m4>
"""

from __future__ import annotations

import difflib
import pathlib
import re
import subprocess
import sys

TARGET = pathlib.Path("backend/app/api/v1/telemetry.py")
TEST = "tests/integration/test_g7_provenance_parity_postgres.py"
BACKEND = pathlib.Path("backend")

# (name, description, exact_old, exact_new)
MUTATIONS = {
    "m1": (
        "remove recorded-revision guard in reading_provenance_status",
        'if row.mapping_revision_id is not None:\n        if row.contract_evidence == "pinned":',
        'if True:\n        if row.contract_evidence == "pinned":',
    ),
    "m2": (
        "swap inferred -> pinned classification in Python derivation",
        'if row.contract_evidence == "inferred_single_revision":\n            return "inferred"',
        'if row.contract_evidence == "inferred_single_revision":\n            return "pinned"',
    ),
    "m3": (
        "collapse registry-null legacy fallback in Python derivation",
        'return "unverified_legacy" if row.registry_version is None else "unverified_registry"',
        'return "unverified_registry"',
    ),
    "m4": (
        "swap SQL fallback unverified_registry -> unverified_legacy",
        '(TelemetryReading.registry_version.is_(None), "unverified_legacy"),\n        else_="unverified_registry",',
        '(TelemetryReading.registry_version.is_(None), "unverified_legacy"),\n        else_="unverified_legacy",',
    ),
}

SETUP_ERROR_MARKERS = (
    re.compile(r"(?im)^\s*(?:ERROR collecting\b|errors? during collection\b)"),
    re.compile(r"(?im)^\s*ERROR at setup of\b"),
    re.compile(r"(?im)^\s*ERROR\s+\S+::"),
    re.compile(r"(?im)^\s*(?:E\s+)?fixture ['\"].+?['\"] not found\b"),
    re.compile(r"(?i)\b(?:ImportError|ModuleNotFoundError)\b"),
    re.compile(r"(?i)\b(?:OperationalError|ProgrammingError)\b"),
    re.compile(r"(?i)\bsqlalchemy\.exc\.[A-Za-z_]+"),
    re.compile(r"(?i)\b(?:psycopg2?\.(?:OperationalError|InterfaceError)|asyncpg\.exceptions\.[A-Za-z_]*Connection[A-Za-z_]*Error)\b"),
    re.compile(r"(?i)\b(?:connection refused|could not connect to (?:server|database)|server closed the connection unexpectedly)\b"),
    re.compile(r"(?i)\b(?:TimeoutExpired|TIMEOUT after|timed out)\b"),
    re.compile(r"(?i)\bINTERNALERROR\b"),
    re.compile(r"(?i)\b(?:PluginValidationError|PluginRegistrationError)\b"),
)


PYTEST_TIMEOUT_SECONDS = 300
ASSERTION_FAILURE_LINE = re.compile(
    r"^FAILED " + re.escape(TEST) + r"::\S+\s+-\s+AssertionError\b", re.MULTILINE
)
ASSERTION_TRACEBACK = re.compile(r"(?m)^E\s+AssertionError(?::|\b)")


def run(cmd: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, f"TIMEOUT after {exc.timeout}s"
    return proc.returncode, proc.stdout + proc.stderr


def is_assertion_failure(code: int, output: str) -> bool:
    """True only when pytest ran the parity test and it failed an assertion.

    Requires pytest's failed-test summary and traceback to identify an
    AssertionError in the expected parity test. Targeted collection, fixture,
    dependency, database, timeout, and internal-error evidence invalidates the
    experiment. Warnings mentioning fixture configuration are not errors.
    """
    if code != 1:
        return False
    if any(marker.search(output) for marker in SETUP_ERROR_MARKERS):
        return False
    return bool(
        ASSERTION_FAILURE_LINE.search(output)
        and ASSERTION_TRACEBACK.search(output)
    )


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MUTATIONS:
        print(f"usage: {sys.argv[0]} <{'|'.join(MUTATIONS)}>")
        return 2
    key = sys.argv[1]
    name, old, new = MUTATIONS[key]

    original = TARGET.read_text()
    if old not in original:
        print(f"SETUP ERROR: {key} anchor not found in {TARGET}")
        return 3

    mutated = original.replace(old, new, 1)
    if mutated == original:
        print(f"SETUP ERROR: {key} produced no change")
        return 3

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f"a/{TARGET}",
            tofile=f"b/{TARGET}",
        )
    )
    print(f"=== MUTATION {key.upper()}: {name} ===")
    print(diff)

    TARGET.write_text(mutated)
    try:
        code, output = run(
            [sys.executable, "-m", "pytest", TEST, "-q", "-rf", "-p", "no:cacheprovider"],
            BACKEND,
        )
    finally:
        TARGET.write_text(original)

    print(output)
    restored = TARGET.read_text()
    if restored != original:
        print(f"RESTORE ERROR: {key} did not restore baseline")
        return 4

    if code == 0:
        print(f"RESULT {key.upper()}=SURVIVED (parity oracle still passed)")
        return 1

    if not is_assertion_failure(code, output):
        print(
            f"RESULT {key.upper()}=INCONCLUSIVE "
            f"(exit={code}; no assertion failure of the parity test)"
        )
        return 5

    print(f"RESULT {key.upper()}=DETECTED (classification assertion failure, exit={code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
