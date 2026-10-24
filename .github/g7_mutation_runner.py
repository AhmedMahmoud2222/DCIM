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
    "ERROR collecting",
    "errors during collection",
    "ImportError",
    "ModuleNotFoundError",
    "OperationalError",
    "ProgrammingError",
    "sqlalchemy.exc",
    "connection refused",
    "fixture",
)


def run(cmd: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


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
            [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
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

    lowered = output.lower()
    if any(marker.lower() in lowered for marker in SETUP_ERROR_MARKERS):
        print(f"RESULT {key.upper()}=INCONCLUSIVE (setup/infrastructure error, not assertion)")
        return 5

    print(f"RESULT {key.upper()}=DETECTED (classification assertion failure, exit={code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
