"""Read-only SEC-ARCH-02 compatibility report, never a legacy authorization mode."""

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, is_scope_independent, load_effective_access
from app.domain.auth.models import User


def _discard_partial(path: Path, identity: tuple[int, int]) -> None:
    """Remove a report this call created but failed to write completely.

    The identity check (device + inode of the file ``os.open`` created) refuses to unlink
    a path that was swapped for a different file while we held it, so failure cleanup can
    never delete data we no longer own. Unlink failures are swallowed: the original write
    error is what the operator needs to see.
    """
    try:
        stat_result = os.stat(path, follow_symlinks=False)
        if (stat_result.st_dev, stat_result.st_ino) != identity:
            return
        os.unlink(path)
    except OSError:
        pass


def write_report_file(path: Path, payload: str) -> None:
    """Create the report owner-only (0600), failing if the target already exists or is a link.

    O_EXCL refuses an existing file or symlink, so a report is never written through a
    planted link or into a file whose permissions are looser than intended. POSIX modes
    are not enforced by Windows; there, rely on the directory's ACL (see the runbook).

    Every failure after creation (descriptor wrap, partial write, flush on close) removes
    the incomplete file instead of leaving access-control fragments on disk for the next
    run to refuse with a confusing FileExistsError.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    stat_result = os.fstat(descriptor)
    identity = (stat_result.st_dev, stat_result.st_ino)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(descriptor)  # fdopen failed before taking ownership
        except OSError:
            pass
        _discard_partial(path, identity)
        raise
    try:
        with handle:
            handle.write(payload)
    except BaseException:
        _discard_partial(path, identity)
        raise


def scope_record(scope: AccessScope) -> dict:
    """Raw, deterministic grant description, including latent selected-rack grants."""
    return {
        "unrestricted": scope.unrestricted,
        "site_ids": sorted(map(str, scope.site_ids)),
        "full_site_ids": sorted(map(str, scope.full_site_ids)),
        "selected_racks_by_site": {
            str(site): sorted(map(str, racks)) for site, racks in sorted(
                scope.selected_racks_by_site.items(), key=lambda item: str(item[0])
            )
        },
    }


async def access_change_report(db: AsyncSession) -> list[dict]:
    """Compare the pinned SEC-ARCH-01 cross-product contract to approved Option A.

    Role eligibility/aggregate reach are deliberately unchanged, so legacy access
    is reconstructed from those inputs without a runnable legacy evaluator.
    CLI callers must wrap the entire report in a READ ONLY REPEATABLE READ transaction.
    Source names and grant IDs are security-sensitive; restrict access to the report.
    """
    users = list((await db.execute(select(User.id).order_by(User.id))).scalars())
    changes = []
    for offset in range(0, len(users), 500):
        accesses = await load_effective_access(db, users[offset:offset + 500])
        for uid, access in accesses.items():
            for code in sorted(access.permission_codes | access.inactive_permissions):
                before_active = access.scope.unrestricted or is_scope_independent(code)
                after_active = code in access.permission_codes
                after = access.scope_for(code, include_inactive=True)
                same_scope = access.scope.contains(after) and after.contains(access.scope)
                if before_active == after_active and same_scope:
                    continue
                changes.append({
                    "user_id": str(uid), "permission": code, "sources": access.sources[code],
                    "before_active": before_active, "after_active": after_active,
                    "before_scope": scope_record(access.scope), "after_scope": scope_record(after),
                    "proposed_narrow_grant": {
                        "permission": code, "scope": scope_record(after), "active": after_active,
                        "apply_automatically": False,
                    },
                    "review_action": "Confirm the source-bound entitlement; approve any additional intended grant explicitly.",
                })
    return changes
