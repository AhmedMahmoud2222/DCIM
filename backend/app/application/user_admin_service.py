"""Shared safeguards for user/group administration: privilege-escalation checks, the
last-administrator invariant, and input normalisation. Every rule here is enforced
server-side and independent of what the frontend shows."""

import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import (
    active_administrator_ids,
    load_effective_access,
    visible_rack_ids_query,
)
from app.application.rbac import AuthContext
from app.core.errors import ApiError, ConflictError, ForbiddenError, NotFoundError
from app.domain.auth.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserGroup,
    UserGroupPermission,
    UserGroupSiteAccess,
)
from app.domain.location.models import Site

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MIN_PASSWORD_LENGTH = 12


class ValidationFailed(ApiError):
    def __init__(self, detail: str):
        super().__init__(status_code=422, title="Unprocessable Entity", detail=detail)


def clean_text(value: str, *, field: str, max_length: int, allow_empty: bool = False) -> str:
    """Trims, rejects control characters and enforces length. Applied to every free-text
    admin field so audit logs and the UI never carry unprintable payloads."""
    cleaned = value.strip()
    if _CONTROL_CHARS.search(cleaned):
        raise ValidationFailed(f"{field} must not contain control characters.")
    if not cleaned and not allow_empty:
        raise ValidationFailed(f"{field} must not be empty.")
    if len(cleaned) > max_length:
        raise ValidationFailed(f"{field} must be at most {max_length} characters.")
    return cleaned


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 256:
        raise ValidationFailed("Password must be at most 256 characters.")


async def resolve_permission_codes(db: AsyncSession, codes: list[str]) -> dict[str, Permission]:
    unique = sorted(set(codes))
    if not unique:
        return {}
    rows = (await db.execute(select(Permission))).scalars().all()
    known = {p.code: p for p in rows}
    unknown = [c for c in unique if c not in known]
    if unknown:
        raise ValidationFailed(f"Unknown permission code(s): {', '.join(unknown[:10])}")
    return {c: known[c] for c in unique}


def assert_can_grant_permissions(ctx: AuthContext, codes: set[str]) -> None:
    """Privilege-escalation guard: an actor can allow-grant only permissions they hold
    themselves (deny grants only remove access and are always permitted)."""
    missing = sorted(codes - ctx.permission_codes)
    if missing:
        raise ForbiddenError(f"You cannot grant permissions you do not hold: {', '.join(missing[:10])}")


async def assert_can_grant_sites(
    db: AsyncSession, ctx: AuthContext, site_ids: set[uuid.UUID], rack_ids: set[uuid.UUID]
) -> None:
    """The actor's own site/rack scope bounds what they can hand out."""
    if site_ids:
        existing = set((await db.execute(select(Site.id).where(Site.id.in_(site_ids)))).scalars())
        if existing != site_ids:
            raise ValidationFailed("One or more sites do not exist.")
    if ctx.scope.unrestricted:
        return
    outside = [s for s in site_ids if s not in ctx.scope.site_ids]
    if outside:
        raise ForbiddenError("You cannot grant access to sites outside your own access.")
    if rack_ids:
        visible = set((await db.execute(select(visible_rack_ids_query(ctx.scope).subquery().c.rack_id))).scalars())
        if not rack_ids <= visible:
            raise ForbiddenError("You cannot grant access to racks outside your own access.")


async def group_grants(db: AsyncSession, group_id: uuid.UUID) -> tuple[set[str], set[uuid.UUID]]:
    """(allow-permission codes, site ids) a group confers on its members."""
    perms = (
        await db.execute(
            select(Permission.resource, Permission.action)
            .join(UserGroupPermission, UserGroupPermission.permission_id == Permission.id)
            .where(UserGroupPermission.group_id == group_id, UserGroupPermission.effect == "allow")
        )
    ).all()
    sites = set(
        (await db.execute(select(UserGroupSiteAccess.site_id).where(UserGroupSiteAccess.group_id == group_id))).scalars()
    )
    return {f"{r}:{a}" for r, a in perms}, sites


async def assert_can_assign_group(db: AsyncSession, ctx: AuthContext, group_id: uuid.UUID) -> None:
    """Adding a user to a group hands them everything the group confers, so the actor must
    already hold all of it. Members of a group cannot manage that group's membership."""
    codes, sites = await group_grants(db, group_id)
    assert_can_grant_permissions(ctx, codes)
    if not ctx.scope.unrestricted and not sites <= ctx.scope.site_ids:
        raise ForbiddenError("You cannot assign a group that grants sites outside your own access.")


async def assert_can_assign_role(db: AsyncSession, ctx: AuthContext, role: Role) -> None:
    codes = {
        f"{r}:{a}"
        for r, a in (
            await db.execute(
                select(Permission.resource, Permission.action)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            )
        ).all()
    }
    assert_can_grant_permissions(ctx, codes)
    if not ctx.scope.unrestricted:
        raise ForbiddenError("Site-restricted administrators cannot assign global roles.")


async def assert_administrator_remains(db: AsyncSession) -> None:
    """Last-administrator protection: after any change that could remove `user:manage` or
    `group:manage` from someone (deactivate/delete/membership/permission/role changes),
    at least one active user must still hold both. Runs inside the caller's transaction
    after flush; the caller rolls back on failure. A transaction-scoped advisory lock
    serialises concurrent admin-affecting changes so two requests cannot each see the other
    administrator still present and both proceed."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dcim.admin_invariant'))"))
    if not await active_administrator_ids(db):
        raise ConflictError(
            "This change would leave no active administrator (a user holding both user:manage and group:manage)."
        )


async def get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


async def get_group_or_404(db: AsyncSession, group_id: uuid.UUID) -> UserGroup:
    group = await db.get(UserGroup, group_id)
    if group is None:
        raise NotFoundError(f"Group {group_id} not found.")
    return group


async def assert_actor_outranks_target(db: AsyncSession, ctx: AuthContext, target_user_id: uuid.UUID) -> None:
    """A user may not administer someone holding permissions they lack (prevents a
    delegated admin from disabling, re-passwording or re-grouping a more powerful user)."""
    if target_user_id == ctx.user.id:
        return
    access = (await load_effective_access(db, [target_user_id]))[target_user_id]
    if not access.permission_codes <= ctx.permission_codes:
        raise ForbiddenError("You cannot administer a user who holds permissions you do not hold.")
    if not ctx.scope.unrestricted and access.scope.unrestricted:
        raise ForbiddenError("You cannot administer an unrestricted user.")

