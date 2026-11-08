"""Split the backend test files into shards for parallel CI jobs.

Each shard job runs its own Postgres service, so the shards share no database.
The split is a pure function of the test file list and the file sizes: the same
checkout always gives the same shards. File size is a proxy for run time. Change the
weights only with measured durations.

Usage (run from the directory that holds the tests, normally backend/):
  ci_shard.py --index I --count N --root tests \
      [--exclude PATH]... [--preload PATH]...

  --exclude  a test file that no shard runs through this selection (it runs elsewhere)
  --preload  a test file that shard 0 runs in a separate step. Its weight counts
             against shard 0, so shard 0 receives fewer files from the rest.

Output: the test files of shard I, one path per line, sorted.

Fail-closed rules:
* An empty selection, an empty shard or an index outside 0..N-1 exits non-zero. An
  empty file list would make `pytest` collect the whole suite, so it never reaches
  the caller.
* A named --exclude or --preload path that is not a discovered test file exits
  non-zero, so a renamed file cannot silently leave the selection.
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

PATTERNS = ("test_*.py", "*_test.py")  # the pytest defaults for python_files


class ShardError(Exception):
    pass


def discover(root: Path) -> list[str]:
    """Return every test file under root, as sorted POSIX paths relative to root's parent."""
    if not root.is_dir():
        raise ShardError(f"root is not a directory: {root}")
    found = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in PATTERNS):
            found.append(path.as_posix())
    return sorted(found)


def weight(path: str) -> int:
    return max(Path(path).stat().st_size, 1)


def partition(files: list[str], weights: dict[str, int], count: int, preload_weight: int = 0) -> list[list[str]]:
    """Longest-processing-time assignment. Shard 0 starts with preload_weight."""
    if count < 1:
        raise ShardError("count must be at least 1")
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0] * count
    loads[0] = preload_weight
    for path in sorted(files, key=lambda item: (-weights[item], item)):
        target = min(range(count), key=lambda index: (loads[index], index))
        shards[target].append(path)
        loads[target] += weights[path]
    return [sorted(shard) for shard in shards]


def select(root: Path, count: int, exclude: list[str], preload: list[str]) -> list[list[str]]:
    discovered = discover(root)
    known = set(discovered)
    for name in (*exclude, *preload):
        if name not in known:
            raise ShardError(f"named file is not a discovered test file: {name}")
    skipped = set(exclude) | set(preload)
    files = [path for path in discovered if path not in skipped]
    if not files:
        raise ShardError("the selection is empty")
    weights = {path: weight(path) for path in files}
    shards = partition(files, weights, count, sum(weight(path) for path in preload))
    if any(not shard for shard in shards):
        raise ShardError("a shard is empty: lower --count")
    return shards


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--root", default="tests")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--preload", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.index < args.count:
            raise ShardError(f"index {args.index} is outside 0..{args.count - 1}")
        shards = select(Path(args.root), args.count, args.exclude, args.preload)
    except ShardError as error:
        print(f"ci_shard: {error}", file=sys.stderr)
        return 1
    chosen = shards[args.index]
    print("\n".join(chosen))
    total = sum(len(shard) for shard in shards)
    print(f"ci_shard: shard {args.index}/{args.count} runs {len(chosen)} of {total} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
