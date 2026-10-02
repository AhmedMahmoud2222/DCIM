"""Shared safeguards for user/group administration: privilege-escalation checks, the
last-administrator invariant, and input normalisation. Every rule here is enforced
server-side and independent of what the frontend shows."""

import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import (
    active_administrator_ids,
    build_scope,
    load_effective_access,
    load_group_scopes,
)
from app.application.rbac import AuthContext
from app.core.errors import ApiError, ConflictError, ForbiddenError, NotFoundError
from app.domain.auth.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
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
    db: AsyncSession, ctx: AuthContext, entries: list[tuple[uuid.UUID, str, list[uuid.UUID]]]
) -> None:
    """entries: (site_id, rack_scope, rack_ids). The actor's own scope must contain
    everything being handed out: a site, `all` racks on it only if the actor has `all`, and
    selected racks only if the actor can already see them on that site."""
    site_ids = {site_id for site_id, _scope, _racks in entries}
    if site_ids:
        existing = set((await db.execute(select(Site.id).where(Site.id.in_(site_ids)))).scalars())
        if existing != site_ids:
            raise ValidationFailed("One or more sites do not exist.")
    if ctx.scope.unrestricted:
        return
    access_ids = [uuid.uuid4() for _ in entries]
    proposed = build_scope(
        [(access_id, site_id, rack_scope) for access_id, (site_id, rack_scope, _racks) in zip(access_ids, entries, strict=True)],
        {access_id: set(racks) for access_id, (_site_id, _scope, racks) in zip(access_ids, entries, strict=True)},
    )
    if not ctx.scope.contains(proposed):
        raise ForbiddenError("You cannot grant site or rack access beyond your own access.")


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
    already hold all of it: every allowed permission and the whole site/rack scope."""
    codes, _sites = await group_grants(db, group_id)
    assert_can_grant_permissions(ctx, codes)
    scope = (await load_group_scopes(db, [group_id]))[group_id]
    if not ctx.scope.contains(scope):
        raise ForbiddenError("You cannot assign a group that grants site or rack access beyond your own.")


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
    """A user may not administer someone holding permissions or site/rack access the actor
    lacks (prevents a delegated admin from disabling, re-passwording, regrouping or
    deleting a more powerful user, or taking over an account with a wider scope)."""
    if target_user_id == ctx.user.id:
        return
    access = (await load_effective_access(db, [target_user_id]))[target_user_id]
    if not access.permission_codes <= ctx.permission_codes:
        raise ForbiddenError("You cannot administer a user who holds permissions you do not hold.")
    if not ctx.scope.contains(access.scope):
        raise ForbiddenError("You cannot administer a user with site or rack access beyond your own.")


async def assert_user_visible(db: AsyncSession, ctx: AuthContext, target_user_id: uuid.UUID) -> None:
    """Site-restricted administrators only see users whose whole scope lies inside theirs.
    Reported as 404 so ids outside the actor's scope cannot be probed."""
    if ctx.scope.unrestricted or target_user_id == ctx.user.id:
        return
    access = (await load_effective_access(db, [target_user_id]))[target_user_id]
    if not ctx.scope.contains(access.scope):
        raise NotFoundError(f"User {target_user_id} not found.")


async def assert_group_visible(db: AsyncSession, ctx: AuthContext, group_id: uuid.UUID) -> None:
    """Site-restricted administrators only see groups whose grants lie inside their scope."""
    if ctx.scope.unrestricted:
        return
    scope = (await load_group_scopes(db, [group_id]))[group_id]
    if not ctx.scope.contains(scope):
        raise NotFoundError(f"Group {group_id} not found.")


async def assert_can_modify_group(db: AsyncSession, ctx: AuthContext, group_id: uuid.UUID) -> None:
    """Gate for every change to a group (rename, delete, members, permissions, sites).

    * the group must be visible to the actor (scope containment);
    * the actor must not be a member (no editing the group that grants you access);
    * the actor must outrank every current member, because any change here can strip or
      add grants for all of them (e.g. adding a deny to a group holding an Administrator).
    """
    await assert_group_visible(db, ctx, group_id)
    members = list((await db.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id))).scalars())
    if ctx.user.id in members:
        raise ForbiddenError("You cannot modify a group you are a member of.")
    for member_id in members:
        await assert_actor_outranks_target(db, ctx, member_id)
