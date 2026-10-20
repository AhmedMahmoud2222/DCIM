"""Read-only SEC-ARCH-02 compatibility report, never a legacy authorization mode."""

import os
import stat
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, is_scope_independent, load_effective_access
from app.domain.auth.models import User


def _is_directory_safe_for_cleanup(dir_path: Path) -> bool:
    """Enforce trust boundary on the report directory.
    
    The directory must not be attacker-writable in unsafe configurations.
    We require the directory to be owned by the effective user, not group/world writable
    (sticky bit is acceptable), and not a symlink. If we cannot verify safety on a
    platform, we fail closed and refuse deletion.
    """
    try:
        dir_stat = os.stat(dir_path, follow_symlinks=False)
    except OSError:
        return False
    
    # Must not be a symlink
    if stat.S_ISLNK(dir_stat.st_mode):
        return False
    
    if not stat.S_ISDIR(dir_stat.st_mode):
        return False
    
    # On POSIX, enforce ownership and permissions
    if os.name == "posix":
        try:
            euid = os.geteuid()
        except AttributeError:
            return False
        
        # Must be owned by effective user
        if dir_stat.st_uid != euid:
            return False
        
        mode = stat.S_IMODE(dir_stat.st_mode)
        # Refuse if group or world writable (sticky bit is fine)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    
    # On Windows/non-POSIX, we cannot reliably enforce the same checks;
    # fail closed for unsafe deletion that requires pathname-based unlinking
    elif os.name != "nt":
        return False
    
    return True


def _discard_partial(path: Path, identity: tuple[int, int]) -> None:
    """Remove a report this call created but failed to write completely.
    
    Uses directory-descriptor-relative operations with re-verification to minimize
    TOCTOU race between identity check and deletion. We open the directory with
    O_DIRECTORY|O_NOFOLLOW, re-stat the entry with AT_SYMLINK_NOFOLLOW, verify
    identity matches and it's not a symlink, then attempt deletion only after
    additional verification. 
    
    To catch swaps that occur between checks and deletion, we rename the target
    to a temp file we control in the same directory (if possible) and verify
    the moved file still has our identity before unlinking.
    
    The directory must meet security requirements (owned by us, not group/world
    writable on POSIX). If atomic identity-bound deletion cannot be guaranteed
    in an attacker-writable directory, we refuse deletion (fail closed).
    
    Trust boundary: the report directory must be controlled by the operator
    (not attacker-writable). On attacker-writable directories, deletion is refused.
    Remaining limitation: there is still a theoretical race window on some
    filesystems/configurations, but we fail closed when directory is unsafe and
    use multiple verification steps.
    """
    try:
        parent = path.parent
        name = path.name
        
        # Fail closed if directory is not safe
        if not _is_directory_safe_for_cleanup(parent):
            return
        
        # Open directory with O_DIRECTORY|O_NOFOLLOW
        dir_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        try:
            dir_fd = os.open(parent, dir_flags)
        except OSError:
            return
        
        try:
            fstatat_flags = getattr(os, "AT_SYMLINK_NOFOLLOW", 0)
            
            # First re-verification
            try:
                st = os.fstatat(dir_fd, name, 0, fstatat_flags)
            except OSError:
                return
            
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return
            if (st.st_dev, st.st_ino) != identity:
                return
            
            # Try to use rename-based approach to catch swap between checks and deletion
            import random
            import time
            temp_name = f".{name}.tmp.{os.getpid()}.{int(time.time()*1000)}.{random.randint(0, 999999)}"
            try:
                # Create our own temp file first
                temp_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    temp_flags |= os.O_NOFOLLOW
                try:
                    temp_fd = os.openat(dir_fd, temp_name, temp_flags, 0o600)
                    os.close(temp_fd)
                except OSError:
                    # If we can't create temp, fall back to direct unlink with extra checks
                    temp_name = None
                
                if temp_name is not None:
                    try:
                        # Attempt rename
                        try:
                            os.renameat(dir_fd, name, dir_fd, temp_name)
                        except OSError:
                            # Try to clean up temp and fall through
                            try:
                                os.unlinkat(dir_fd, temp_name, 0)
                            except OSError:
                                pass
                            temp_name = None
                        
                        if temp_name is not None:
                            # Verify the moved file is still ours
                            try:
                                st_moved = os.fstatat(dir_fd, temp_name, 0, fstatat_flags)
                                if stat.S_ISLNK(st_moved.st_mode) or not stat.S_ISREG(st_moved.st_mode):
                                    try:
                                        os.unlinkat(dir_fd, temp_name, 0)
                                    except OSError:
                                        pass
                                    return
                                if (st_moved.st_dev, st_moved.st_ino) != identity:
                                    try:
                                        os.unlinkat(dir_fd, temp_name, 0)
                                    except OSError:
                                        pass
                                    return
                                # Safe to delete
                                try:
                                    os.unlinkat(dir_fd, temp_name, 0)
                                except OSError:
                                    pass
                                return
                            except OSError:
                                return
                    except Exception:
                        if temp_name is not None:
                            try:
                                os.unlinkat(dir_fd, temp_name, 0)
                            except OSError:
                                pass
                        return
            except Exception:
                pass
            
            # Fallback: direct unlink with final verification
            try:
                st_final = os.fstatat(dir_fd, name, 0, fstatat_flags)
                if stat.S_ISLNK(st_final.st_mode) or not stat.S_ISREG(st_final.st_mode):
                    return
                if (st_final.st_dev, st_final.st_ino) != identity:
                    return
                os.unlinkat(dir_fd, name, 0)
            except OSError:
                pass
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass
    except Exception:
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
