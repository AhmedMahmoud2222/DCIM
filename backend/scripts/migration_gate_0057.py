"""Fail-closed release gate for Alembic migration 0057 (issue #214).

Used as the production Compose ``migrate`` command. It never modifies the database
itself unless the operator approval is present; it either exits non-zero *before*
any ``alembic upgrade`` or runs ``alembic upgrade head`` unchanged. It never performs a
partial upgrade to an earlier revision, so a blocked start leaves the schema untouched
and every dependent service (bootstrap, backend, workers) stays down.

Approval requires BOTH environment variables:
  DCIM_APPROVE_MIGRATION_0057=I_ACCEPT_PRODUCTION_INDEX_BUILD
  DCIM_MIGRATION_0057_CHANGE_ID=<non-empty change/approval reference>

Fail-closed: any inability to determine the current revision, the head, or whether 0057
is pending blocks the migration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable

GATED_REVISION = "0057_fk_indexes"
APPROVAL_VAR = "DCIM_APPROVE_MIGRATION_0057"
APPROVAL_VALUE = "I_ACCEPT_PRODUCTION_INDEX_BUILD"
CHANGE_ID_VAR = "DCIM_MIGRATION_0057_CHANGE_ID"
_REV_RE = re.compile(r"^([0-9A-Za-z_]+)\b")


class GateError(Exception):
    """The gate could not prove it is safe to proceed."""


def approved(env: dict[str, str]) -> bool:
    return env.get(APPROVAL_VAR) == APPROVAL_VALUE and bool(env.get(CHANGE_ID_VAR, "").strip())


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


def pending_revisions(current: list[str]) -> list[str]:
    """Revisions that ``upgrade head`` would apply, newest first."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    if len(heads) != 1:
        raise GateError(f"expected exactly one Alembic head, found {len(heads)}")
    if len(current) > 1:
        raise GateError(f"database reports multiple current revisions: {current}")
    revs = [r.revision for r in script.walk_revisions()]
    if not current:
        return revs
    applied = current[0]
    if applied not in revs:
        raise GateError(f"database revision {applied} is unknown to this release")
    # `iterate_revisions(applied, "base")` yields applied and all its ancestors.
    done = {r.revision for r in script.iterate_revisions(applied, "base")}
    return [r for r in revs if r not in done]


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
            f"{CHANGE_ID_VAR}=<change reference>. See docs/FK_INDEX_ROLLOUT_0057.md.",
            file=sys.stderr,
        )
        return 2
    if GATED_REVISION in pending:
        print(f"Migration {GATED_REVISION} approved; change id: {env[CHANGE_ID_VAR].strip()}")
    return upgrade()


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
