"""Read-only SEC-ARCH-02 compatibility report, never a legacy authorization mode."""

import os
import stat
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, is_scope_independent, load_effective_access
from app.domain.auth.models import User


class PreflightReportSecurityError(OSError):
    """Raised when destination directory cannot meet security requirements."""


def _validate_directory_security(dir_path: Path) -> None:
    """Validate directory security using supported APIs.
    
    Trust model: directory must be controlled by the operator (same effective user on POSIX),
    not a symlink, not group/world writable on POSIX. On Windows, we enforce basic
    properties and fail closed if unclear. Unsupported platforms fail closed.
    """
    try:
        lstat = os.lstat(dir_path)
    except OSError as exc:
        raise PreflightReportSecurityError("Cannot access report directory") from exc
    
    if stat.S_ISLNK(lstat.st_mode):
        raise PreflightReportSecurityError("Report directory is a symlink")
    
    try:
        dir_stat = os.stat(dir_path)
    except OSError as exc:
        raise PreflightReportSecurityError("Cannot stat report directory") from exc
    
    if not stat.S_ISDIR(dir_stat.st_mode):
        raise PreflightReportSecurityError("Report path is not a directory")
    
    if os.name == "posix":
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            raise PreflightReportSecurityError("Cannot determine effective user")
        euid = geteuid()
        if dir_stat.st_uid != euid:
            raise PreflightReportSecurityError("Report directory not owned by current user")
        mode = stat.S_IMODE(dir_stat.st_mode)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PreflightReportSecurityError("Report directory is group/world writable")
    elif os.name == "nt":
        pass
    else:
        raise PreflightReportSecurityError("Unsupported platform for secure report storage")


def _discard_partial(path: Path, identity: tuple[int, int]) -> None:
    """Remove a report this call created but failed to write completely.
    
    Design: the directory must be validated as secure before writing; on failure
    we only delete if we can re-verify the entry is a regular file (not symlink),
    still has our exact (st_dev, st_ino) identity from creation, and the directory
    remains secure. We do not do repeated stat-then-unlink races in untrusted dirs;
    if directory security cannot be confirmed, we fail closed (do not delete).
    This avoids attacker-controlled pathname deletion by enforcing trust boundary
    at creation/cleanup validation time.
    """
    try:
        parent = path.parent
        try:
            _validate_directory_security(parent)
        except PreflightReportSecurityError:
            return
        except Exception:
            return
        
        try:
            lstat_res = os.lstat(path)
            if stat.S_ISLNK(lstat_res.st_mode):
                return
        except OSError:
            return
        
        try:
            st = os.stat(path)
        except OSError:
            return
        
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return
        if (st.st_dev, st.st_ino) != identity:
            return
        
        try:
            os.unlink(path)
        except OSError:
            pass
    except Exception:
        pass


def write_report_file(path: Path, payload: str) -> None:
    """Create the report owner-only (0600), failing if the target already exists or is a link.

    O_EXCL refuses an existing file or symlink, so a report is never written through a
    planted link or into a file whose permissions are looser than intended. POSIX modes
    are not enforced by Windows; there, rely on the directory's ACL (see the runbook).

    Directory must pass security validation before writing confidential data.
    Every failure after creation (descriptor wrap, partial write, flush on close) removes
    the incomplete file instead of leaving access-control fragments on disk for the next
    run to refuse with a confusing FileExistsError.
    """
    parent = path.parent
    try:
        _validate_directory_security(parent)
    except PreflightReportSecurityError as exc:
        raise OSError(str(exc)) from exc
    
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise
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
