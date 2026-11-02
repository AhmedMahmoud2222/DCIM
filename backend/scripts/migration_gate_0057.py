"""Fail-closed release gate for Alembic migration 0057 (issue #214).

Used as the production Compose ``migrate`` command. It never modifies the database
itself unless the operator approval is present; it either exits non-zero *before*
any ``alembic upgrade`` or runs ``alembic upgrade head`` unchanged. It never performs a
partial upgrade to an earlier revision, so a blocked start leaves the schema untouched
and every dependent service (bootstrap, backend, workers) stays down.

Approval requires BOTH environment variables:
  DCIM_APPROVE_MIGRATION_0057=I_ACCEPT_PRODUCTION_INDEX_BUILD
  DCIM_MIGRATION_0057_CHANGE_ID=<change reference matching (CHG|CR|RFC)-<4-10 digits>>
The reference is format-checked only; the gate cannot verify it against a change system.

Fail-closed: any inability to determine the current revision, the head, or whether 0057
is pending blocks the migration.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

GATED_REVISION = "0057_fk_indexes"
APPROVAL_VAR = "DCIM_APPROVE_MIGRATION_0057"
APPROVAL_VALUE = "I_ACCEPT_PRODUCTION_INDEX_BUILD"
CHANGE_ID_VAR = "DCIM_MIGRATION_0057_CHANGE_ID"
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
CHANGE_ID_RE = re.compile(r"(CHG|CR|RFC)-[0-9]{4,10}")  # exact match, no surrounding whitespace
_REV_RE =re.compile(r"^([0-9A-Za-z_]+)\b")


class GateError(Exception):
    """The gate could not prove it is safe to proceed."""


def approved(env: dict[str, str]) -> bool:
    """Operator acknowledgement plus a well-formed change reference.

    This is a format-checked operator acknowledgement, NOT proof of authority: the container
    has no access to the change-management system. Owner/DBA approval is established out of
    band, and only that approval process may place these variables in the deploy environment.
    """
    return env.get(APPROVAL_VAR) == APPROVAL_VALUE and CHANGE_ID_RE.fullmatch(env.get(CHANGE_ID_VAR, "")) is not None


def _alembic(*args: str) -> str:
    result = subprocess.run(["alembic", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GateError(f"`alembic {' '.join(args)}` failed (exit {result.returncode})")
    return result.stdout


def current_revisions() -> list[str]:
    revs = []
    for line in _alembic("current").splitlines():
        match = _REV_RE.match(line.strip())
        if match:
            revs.append(match.group(1))
    return revs


def _literal_ids(value: ast.expr | None) -> list[str]:
    if value is None:
        return []
    literal = ast.literal_eval(value)
    if literal is None:
        return []
    if isinstance(literal, str):
        return [literal]
    if isinstance(literal, (tuple, list)) and all(isinstance(x, str) for x in literal):
        return list(literal)
    raise GateError(f"unsupported revision literal {literal!r}")


def load_graph(versions: Path = VERSIONS_DIR) -> dict[str, list[str]]:
    """``{revision: [down_revisions]}`` read statically from the revision files.

    Independent of the ``alembic`` package so the graph logic is testable without it; any
    unreadable or ambiguous file raises ``GateError`` (fail closed).
    """
    graph: dict[str, list[str]] = {}
    files = sorted(versions.glob("*.py"))
    if not files:
        raise GateError(f"no migration files found in {versions}")
    for path in files:
        values: dict[str, ast.expr | None] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                values[node.targets[0].id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                values[node.target.id] = node.value
        rev = _literal_ids(values.get("revision"))
        if len(rev) != 1:
            raise GateError(f"{path.name}: cannot determine revision id")
        if rev[0] in graph:
            raise GateError(f"duplicate revision {rev[0]}")
        graph[rev[0]] = _literal_ids(values.get("down_revision"))
    return graph


def _ancestors(graph: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        rev = stack.pop()
        if rev in seen:
            continue
        if rev not in graph:
            raise GateError(f"revision {rev} is unknown to this release")
        seen.add(rev)
        stack.extend(graph[rev])
    return seen


def pending_revisions(current: list[str], versions: Path = VERSIONS_DIR) -> list[str]:
    """Revisions that ``upgrade head`` would apply."""
    graph = load_graph(versions)
    parents = {p for downs in graph.values() for p in downs}
    heads = [r for r in graph if r not in parents]
    if len(heads) != 1:
        raise GateError(f"expected exactly one Alembic head, found {len(heads)}")
    if len(current) > 1:
        raise GateError(f"database reports multiple current revisions: {current}")
    done = _ancestors(graph, current[0]) if current else set()
    return sorted(set(graph) - done)


def run(
    env: dict[str, str],
    current_fn: Callable[[], list[str]] = current_revisions,
    pending_fn: Callable[[list[str]], list[str]] = pending_revisions,
    upgrade_fn: Callable[[], int] | None = None,
) -> int:
    upgrade = upgrade_fn or (lambda: subprocess.run(["alembic", "upgrade", "head"], check=False).returncode)
    try:
        pending = pending_fn(current_fn())
    except Exception as exc:  # fail closed on ANY uncertainty
        print(f"BLOCKED: cannot determine whether migration 0057 is pending ({exc}). "
              "No migration was run.", file=sys.stderr)
        return 3
    if GATED_REVISION in pending and not approved(env):
        print(
            f"BLOCKED: migration {GATED_REVISION} is pending and has not been approved for this "
            f"environment (issue #214). No migration was run and the schema is unchanged. "
            f"To proceed after owner/DBA approval set {APPROVAL_VAR}={APPROVAL_VALUE} and "
            f"{CHANGE_ID_VAR}=<CHG|CR|RFC>-<digits>. See docs/FK_INDEX_ROLLOUT_0057.md.",
            file=sys.stderr,
        )
        return 2
    if GATED_REVISION in pending:
        print(f"Migration {GATED_REVISION} approved; change id: {env[CHANGE_ID_VAR].strip()}")
    return upgrade()


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
