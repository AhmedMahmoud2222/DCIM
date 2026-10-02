"""Fail unless the Alembic revision graph has exactly one head.

Two pull requests that each add a revision on the same parent merge cleanly as text but
leave two heads, and `alembic upgrade head` then refuses to run. This check reads only the
revision files, so it needs no database and runs before any migration step in CI.

    python scripts/check_alembic_single_head.py [--script-location PATH]

Exit codes: 0 one head, 1 zero or several heads (each head and its file is printed).
"""

import argparse
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

DEFAULT_SCRIPT_LOCATION = Path(__file__).resolve().parents[1] / "migrations"


def find_heads(script_location: Path) -> list[tuple[str, str]]:
    """Returns `(revision, file name)` for every head under `script_location`."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    directory = ScriptDirectory.from_config(config)
    found = []
    for revision in directory.get_revisions(directory.get_heads()):
        found.append((revision.revision, Path(revision.path).name))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script-location", type=Path, default=DEFAULT_SCRIPT_LOCATION)
    args = parser.parse_args(argv)
    heads = find_heads(args.script_location)
    if len(heads) == 1:
        print(f"OK: single Alembic head {heads[0][0]}")
        return 0
    print(f"FAIL: expected exactly one Alembic head, found {len(heads)}:", file=sys.stderr)
    for revision, file_name in heads:
        print(f"  {revision}  ({file_name})", file=sys.stderr)
    print("Make the later migration revise the earlier one (set down_revision) before merging.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
