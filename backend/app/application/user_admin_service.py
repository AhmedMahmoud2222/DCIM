"""Shared safeguards for user/group administration: privilege-escalation checks, the
last-administrator invariant, and input normalisation. Every rule here is enforced
server-side and independent of what the frontend shows."""

import dataclasses
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import (
    AccessScope,
    EffectiveAccess,
    active_administrator_ids,
    load_effective_access,
    load_group_scopes,
    visible_rack_ids_query,
)
from app.application.authority_lock import ADMIN_INVARIANT_LOCK_NAME, acquire_authority_lock
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
    UserGroupRackAccess,
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


async def scope_contains(db: AsyncSession, outer: AccessScope, inner: AccessScope, *, current_only: bool = False) -> bool:
    """True when everything `inner` can see, `outer` can see too: every site, every
    `rack_scope=all` site, and every individually selected rack. Compared on granted
    scope, so a wider rack scope inside a shared site is correctly reported as wider.

    A selected-rack grant can outlive the rack's placement (the rack moved to a site the grant's holder is not granted):
    it confers nothing today but revives if the rack returns. The default comparison keeps those raw ids, so delegation
    (group assignment, grant checks, post-state) never lets an actor confer a latent grant it does not hold itself.
    `current_only=True` drops the stale ids of `inner` and is for the strict-outranking peer test only, where a stale
    grant must not make a peer look lower."""
    if outer.unrestricted:
        return True
    if inner.unrestricted:
        return False
    if not inner.site_ids <= outer.site_ids or not inner.full_site_ids <= outer.full_site_ids:
        return False
    inner_racks = set(inner.rack_ids)
    if current_only and inner_racks:
        inner_visible = visible_rack_ids_query(inner).subquery()
        inner_racks = set(
            (await db.execute(select(inner_visible.c.rack_id).where(inner_visible.c.rack_id.in_(inner_racks)))).scalars()
        )
    wanted = inner_racks - outer.rack_ids
    if not wanted:
        return True
    visible_sub = visible_rack_ids_query(outer).subquery()
    seen: set[uuid.UUID] = set(
        (await db.execute(select(visible_sub.c.rack_id).where(visible_sub.c.rack_id.in_(wanted)))).scalars()
    )
    return wanted <= seen


def scope_from_entries(entries: list[tuple[uuid.UUID, str, list[uuid.UUID]]]) -> AccessScope:
    """Builds the scope a set of (site_id, rack_scope, rack_ids) grants would confer."""
    return AccessScope(
        unrestricted=False,
        site_ids=frozenset(site for site, _, _ in entries),
        full_site_ids=frozenset(site for site, scope, _ in entries if scope == "all"),
        rack_ids=frozenset(rack for _, scope, racks in entries if scope != "all" for rack in racks),
    )


async def group_scope(db: AsyncSession, group_id: uuid.UUID) -> AccessScope:
    rows = (
        await db.execute(
            select(UserGroupSiteAccess.id, UserGroupSiteAccess.site_id, UserGroupSiteAccess.rack_scope).where(
                UserGroupSiteAccess.group_id == group_id
            )
        )
    ).all()
    racks: dict[uuid.UUID, list[uuid.UUID]] = {}
    if rows:
        for access_id, rack_id in (
            await db.execute(
                select(UserGroupRackAccess.site_access_id, UserGroupRackAccess.rack_id).where(
                    UserGroupRackAccess.site_access_id.in_([r[0] for r in rows])
                )
            )
        ).all():
            racks.setdefault(access_id, []).append(rack_id)
    return scope_from_entries([(site, scope, racks.get(access_id, [])) for access_id, site, scope in rows])


async def assert_can_grant_sites(
    db: AsyncSession, ctx: AuthContext, entries: list[tuple[uuid.UUID, str, list[uuid.UUID]]]
) -> None:
    """The actor's own site/rack scope bounds what they can hand out, including the rack
    scope inside a shared site (an actor limited to some racks cannot grant `all`)."""
    site_ids = {site for site, _, _ in entries}
    if site_ids:
        existing = set((await db.execute(select(Site.id).where(Site.id.in_(site_ids)))).scalars())
        if existing != site_ids:
            raise ValidationFailed("One or more sites do not exist.")
    if ctx.scope.unrestricted:
        return
    if not await scope_contains(db, ctx.scope, scope_from_entries(entries)):
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
    already hold all of it. Members of a group cannot manage that group's membership."""
    codes, _ = await group_grants(db, group_id)
    assert_can_grant_permissions(ctx, codes)
    if not ctx.scope.unrestricted and not await scope_contains(db, ctx.scope, await group_scope(db, group_id)):
        raise ForbiddenError("You cannot assign a group that grants site or rack access beyond your own access.")


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
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": ADMIN_INVARIANT_LOCK_NAME})
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


async def _covers(
    db: AsyncSession, outer: EffectiveAccess | AuthContext, inner: EffectiveAccess, *, current_only: bool = False
) -> bool:
    """`inner` <= `outer` in the authority order: `inner` holds no permission and no
    site/rack scope that `outer` lacks."""
    return inner.permission_codes <= outer.permission_codes and await scope_contains(
        db, outer.scope, inner.scope, current_only=current_only
    )


async def actor_strictly_outranks(db: AsyncSession, ctx: AuthContext, target: EffectiveAccess) -> str | None:
    """Delegated-administration rule. Effective authority is the pair (permissions, scope),
    ordered component-wise: permissions by set inclusion, scope by `scope_contains`. That is
    a partial order, so no numeric rank is invented. The actor strictly outranks the target
    only when target <= actor AND NOT actor <= target. Returns None on success, else the
    reason:
      * "exceeds"    the target holds a permission or scope the actor lacks, which also
                     covers incomparable principals (safe default: reject);
      * "equal"      identical effective authority (a peer)."""
    if not await _covers(db, ctx, target):
        return "exceeds"
    actor_as_access = EffectiveAccess(
        user_id=ctx.user.id, permission_codes=ctx.permission_codes, role_names=ctx.role_names, scope=ctx.scope
    )
    # Reverse half only: a stale selected-rack grant held by the actor must not make a peer look lower.
    if await _covers(db, target, actor_as_access, current_only=True):
        return "equal"
    return None


async def assert_actor_outranks_users(db: AsyncSession, ctx: AuthContext, user_ids: set[uuid.UUID]) -> None:
    """A user may administer another user only when they STRICTLY outrank them (see
    `actor_strictly_outranks`): the target's permissions and site/rack scope lie within the
    actor's and the two are not identical. Peers, wider, and incomparable principals are all
    refused. Every route that changes what another user can do must go through this check,
    before it is made. Granting a user permissions or scope the actor holds is still delegation
    (see `assert_can_grant_*`); it never lets the actor create authority beyond their own."""
    others = user_ids - {ctx.user.id}
    if not others:
        return
    for access in (await load_effective_access(db, list(others))).values():
        reason = await actor_strictly_outranks(db, ctx, access)
        if reason == "exceeds":
            raise ForbiddenError(
                "You cannot administer a user whose permissions or site/rack access exceed or differ from yours."
            )
        if reason == "equal":
            raise ForbiddenError("You cannot administer a user with the same effective authority as yours.")


async def assert_resulting_authority_within_actor(db: AsyncSession, ctx: AuthContext, user_ids: set[uuid.UUID]) -> None:
    """Post-state guard, called after the change is flushed and before anything is committed.
    The pre-mutation check (`assert_actor_outranks_users`) only proves the target is below the
    actor NOW; removing a deny grant, a deny membership or a whole group can restore authority
    the target already held elsewhere. Every surviving principal whose effective access the
    change can alter must still satisfy `target <= actor` afterwards. Equality is allowed (that
    is delegation: the actor conferred what they hold); exceeding the actor, or becoming
    incomparable with the actor, is not. On failure the whole transaction (membership and grant
    rows, password or token changes, audit rows) is rolled back and the locks are released."""
    others = user_ids - {ctx.user.id}
    if not others:
        return
    for access in (await load_effective_access(db, list(others))).values():
        if not await _covers(db, ctx, access):
            await db.rollback()
            raise ForbiddenError(
                "This change would give a user permissions or site/rack access that exceed or differ from yours."
            )


def assert_not_changing_own_membership(ctx: AuthContext, user_ids: set[uuid.UUID]) -> None:
    """Nobody may add themselves to, or remove themselves from, a group. A group can hold a
    deny grant, so a self-addition is a self-modification of effective permissions, and the
    outranking check deliberately skips the actor."""
    if ctx.user.id in user_ids:
        raise ForbiddenError("You cannot change your own group memberships.")


async def begin_authority_change(db: AsyncSession, ctx: AuthContext, permission: str) -> AuthContext:
    """Serialises every authority-changing mutation and returns the actor's CURRENT context.
    The context resolved at request start can be stale by the time the mutation commits (a
    concurrent membership/grant change could shrink the actor or promote the target after the
    check). A transaction-scoped advisory lock makes such mutations run one at a time, and the
    actor is re-read after the lock is held, so every later check sees all earlier commits
    (READ COMMITTED). Rack-placement writers take the same lock shared, so a rack relocation
    cannot commit between this decision and its commit either (see authority_lock.py for the
    full lock order). The lock is released at commit/rollback."""
    await acquire_authority_lock(db)
    active = (
        await db.execute(select(User.is_active).where(User.id == ctx.user.id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if not active:
        raise ForbiddenError("Your account is no longer active.")
    access = (await load_effective_access(db, [ctx.user.id]))[ctx.user.id]
    if permission not in access.permission_codes:
        raise ForbiddenError(f"Permission {permission} is required.")
    return dataclasses.replace(
        ctx,
        permission_codes=access.permission_codes,
        role_names=access.role_names,
        scope=access.scope,
        inactive_permissions=access.inactive_permissions,
    )


async def assert_actor_outranks_target(db: AsyncSession, ctx: AuthContext, target_user_id: uuid.UUID) -> None:
    await assert_actor_outranks_users(db, ctx, {target_user_id})


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
    """Changing a group changes every member's access, so the actor must strictly outrank all
    current members, and (if site-restricted) the group's own site grants must lie within
    the actor's scope. Closes cross-site group tampering and deny-group lockouts."""
    members = set((await db.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id))).scalars())
    await assert_actor_outranks_users(db, ctx, members)
    if not ctx.scope.unrestricted and not await scope_contains(db, ctx.scope, await group_scope(db, group_id)):
        raise ForbiddenError("You cannot modify a group that grants site or rack access beyond your own access.")
