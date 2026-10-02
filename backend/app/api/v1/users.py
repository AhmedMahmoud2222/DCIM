"""User administration (§13: authorization is enforced server-side via require_permission,
never by hiding a button). Create / edit / activate / deactivate / delete users and manage
their group memberships. Safeguards live in app/application/user_admin_service.py."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.access_control import load_effective_access, visible_rack_ids_in_site
from app.application.audit_service import write_audit_log
from app.application.rbac import AuthContext, require_permission
from app.application.user_admin_service import (
    assert_actor_outranks_target,
    assert_administrator_remains,
    assert_can_assign_group,
    assert_can_assign_role,
    assert_resulting_authority_within_actor,
    begin_authority_change,
    clean_text,
    get_user_or_404,
    validate_password,
)
from app.core.errors import ApiError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import hash_password
from app.domain.auth.models import RefreshToken, Role, RoleAssignment, User, UserGroup, UserGroupMember

router = APIRouter(prefix="/users", tags=["users"])

MAX_GROUPS_PER_USER = 100


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(max_length=200)
    password: str = Field(max_length=256)
    role_name: str | None = Field(default=None, max_length=64)
    """Optional legacy global role. Users created with groups only are site-restricted."""
    group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_GROUPS_PER_USER)
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    password: str | None = Field(default=None, max_length=256)
    group_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_GROUPS_PER_USER)


class GroupRef(BaseModel):
    id: uuid.UUID
    name: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: datetime
    role_names: list[str] = []
    groups: list[GroupRef] = []
    is_restricted: bool = False


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


async def _serialize(db: AsyncSession, users: list[User]) -> list[UserOut]:
    ids = [u.id for u in users]
    access = await load_effective_access(db, ids)
    group_rows = (
        await db.execute(
            select(UserGroupMember.user_id, UserGroup.id, UserGroup.name)
            .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
            .where(UserGroupMember.user_id.in_(ids))
            .order_by(UserGroup.name)
        )
    ).all()
    groups: dict[uuid.UUID, list[GroupRef]] = {}
    for uid, gid, gname in group_rows:
        groups.setdefault(uid, []).append(GroupRef(id=gid, name=gname))
    return [
        UserOut(
            id=u.id, email=u.email, full_name=u.full_name, is_active=u.is_active, created_at=u.created_at,
            role_names=sorted(access[u.id].role_names), groups=groups.get(u.id, []),
            is_restricted=not access[u.id].scope.unrestricted,
        )
        for u in users
    ]


async def _revoke_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    for token in (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)))
    ).scalars():
        token.revoked_at = now


async def _load_groups(db: AsyncSession, group_ids: list[uuid.UUID]) -> list[UserGroup]:
    unique = list(dict.fromkeys(group_ids))
    if not unique:
        return []
    groups = list((await db.execute(select(UserGroup).where(UserGroup.id.in_(unique)))).scalars())
    if len(groups) != len(unique):
        raise NotFoundError("One or more groups were not found.")
    return groups


async def _set_memberships(
    db: AsyncSession, ctx: AuthContext, user: User, new_ids: list[uuid.UUID]
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    if user.id == ctx.user.id:
        raise ForbiddenError("You cannot change your own group memberships.")
    await _load_groups(db, new_ids)
    current = set((await db.execute(select(UserGroupMember.group_id).where(UserGroupMember.user_id == user.id))).scalars())
    target = set(new_ids)
    for gid in target - current:
        await assert_can_assign_group(db, ctx, gid)
    # Removing a membership needs no grant check; the caller already verified the actor
    # may administer this user.
    for gid in target - current:
        db.add(UserGroupMember(group_id=gid, user_id=user.id))
    for gid in current - target:
        member = await db.get(UserGroupMember, (gid, user.id))
        if member is not None:
            await db.delete(member)
    await db.flush()
    return current, target


@router.get("", response_model=Page[UserOut])
async def list_users(
    q: str | None = Query(default=None, max_length=200),
    is_active: bool | None = None,
    group_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("user:read")),
) -> Page:
    conds = []
    if q:
        needle = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        conds.append(
            or_(func.lower(User.email).like(like, escape="\\"), func.lower(User.full_name).like(like, escape="\\"))
        )
    if is_active is not None:
        conds.append(User.is_active.is_(is_active))
    if group_id is not None:
        conds.append(User.id.in_(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id)))
    total = (await db.execute(select(func.count()).select_from(User).where(*conds))).scalar_one()
    users = list(
        (await db.execute(select(User).where(*conds).order_by(User.email).offset(pagination.offset).limit(pagination.limit)))
        .scalars()
    )
    return Page(items=await _serialize(db, users), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("user:read"))
) -> UserOut:
    user = await get_user_or_404(db, user_id)
    return (await _serialize(db, [user]))[0]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("user:manage")),
) -> UserOut:
    ctx = await begin_authority_change(db, ctx, "user:manage")
    full_name = clean_text(body.full_name, field="full_name", max_length=200)
    validate_password(body.password)
    email = body.email.lower()
    if (await db.execute(select(User.id).where(User.email == email))).first() is not None:
        raise ConflictError(f"A user with email {email} already exists.")

    role = None
    if body.role_name is not None:
        role = (await db.execute(select(Role).where(Role.name == body.role_name))).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"Role {body.role_name!r} not found.")
        await assert_can_assign_role(db, ctx, role)
    group_ids = list(dict.fromkeys(body.group_ids))
    await _load_groups(db, group_ids)
    for gid in group_ids:
        await assert_can_assign_group(db, ctx, gid)

    user = User(email=email, full_name=full_name, password_hash=hash_password(body.password), is_active=body.is_active)
    db.add(user)
    await db.flush()
    if role is not None:
        db.add(RoleAssignment(user_id=user.id, role_id=role.id, scope_type="global", scope_id=None))
    for gid in group_ids:
        db.add(UserGroupMember(group_id=gid, user_id=user.id))
    await db.flush()
    await assert_resulting_authority_within_actor(db, ctx, {user.id})

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="user.create", entity_type="user", entity_id=user.id,
        request_id=request_id, correlation_id=correlation_id,
        after={
            "email": user.email, "role": body.role_name, "group_ids": [str(g) for g in group_ids],
            "is_active": user.is_active,
        },
    )
    await db.commit()
    await db.refresh(user)
    return (await _serialize(db, [user]))[0]


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("user:manage")),
) -> UserOut:
    ctx = await begin_authority_change(db, ctx, "user:manage")
    user = await get_user_or_404(db, user_id)
    await assert_actor_outranks_target(db, ctx, user.id)
    before = {"full_name": user.full_name, "is_active": user.is_active}
    after: dict = {}

    if user.id == ctx.user.id and body.group_ids is not None:
        raise ForbiddenError("You cannot change your own group memberships.")
    try:
        if body.full_name is not None:
            user.full_name = clean_text(body.full_name, field="full_name", max_length=200)
            after["full_name"] = user.full_name
        if body.is_active is not None and body.is_active != user.is_active:
            if user.id == ctx.user.id and not body.is_active:
                raise ForbiddenError("You cannot deactivate your own account.")
            user.is_active = body.is_active
            after["is_active"] = user.is_active
            if not user.is_active:
                await _revoke_refresh_tokens(db, user.id)
        if body.password is not None:
            validate_password(body.password)
            user.password_hash = hash_password(body.password)
            after["password"] = "changed"
            await _revoke_refresh_tokens(db, user.id)
        if body.group_ids is not None:
            old, new = await _set_memberships(db, ctx, user, body.group_ids)
            before["group_ids"] = sorted(str(g) for g in old)
            after["group_ids"] = sorted(str(g) for g in new)
        await db.flush()
        if body.group_ids is not None:
            await assert_resulting_authority_within_actor(db, ctx, {user.id})
        await assert_administrator_remains(db)
    except ApiError:
        # Any refusal after the first write (authority, last-administrator, validation) must
        # discard the password, token, active-flag and membership changes made so far.
        await db.rollback()
        raise
    request_id, correlation_id = _request_ids(request)
    action = "user.update"
    if after.get("is_active") is False:
        action = "user.deactivate"
    elif after.get("is_active"):
        action = "user.activate"
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action=action, entity_type="user", entity_id=user.id,
        request_id=request_id, correlation_id=correlation_id, before=before, after=after,
    )
    await db.commit()
    await db.refresh(user)
    return (await _serialize(db, [user]))[0]


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("user:manage")),
) -> None:
    ctx = await begin_authority_change(db, ctx, "user:manage")
    user = await get_user_or_404(db, user_id)
    if user.id == ctx.user.id:
        raise ForbiddenError("You cannot delete your own account.")
    await assert_actor_outranks_target(db, ctx, user.id)
    snapshot = {"email": user.email, "full_name": user.full_name, "is_active": user.is_active}
    try:
        await db.delete(user)
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            "This user owns records (imports, catalog revisions) and cannot be deleted; deactivate instead."
        ) from exc
    try:
        await assert_administrator_remains(db)
    except ConflictError:
        await db.rollback()
        raise
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="user.delete", entity_type="user", entity_id=user_id,
        request_id=request_id, correlation_id=correlation_id, before=snapshot,
    )
    await db.commit()


class EffectiveSite(BaseModel):
    site_id: uuid.UUID
    code: str
    name: str
    rack_scope: str
    rack_ids: list[uuid.UUID]


class EffectiveAccessOut(BaseModel):
    user_id: uuid.UUID
    is_active: bool
    unrestricted: bool
    role_names: list[str]
    groups: list[GroupRef]
    permissions: dict[str, list[str]]
    """Effective permission code -> the roles/groups granting it."""
    denied_permissions: list[str]
    inactive_permissions: list[str]
    """Granted but not honoured: the endpoint is not site-scoped, so a restricted user
    cannot use it (fail closed)."""
    sites: list[EffectiveSite]


@router.get("/{user_id}/effective-access", response_model=EffectiveAccessOut)
async def get_effective_access(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("user:read")),
) -> EffectiveAccessOut:
    from app.domain.location.models import Site

    user = await get_user_or_404(db, user_id)
    access = (await load_effective_access(db, [user.id]))[user.id]
    groups = (await _serialize(db, [user]))[0].groups
    sites: list[EffectiveSite] = []
    if access.scope.unrestricted:
        pass
    else:
        rows = (await db.execute(select(Site).where(Site.id.in_(access.scope.site_ids)).order_by(Site.code))).scalars().all()
        for site in rows:
            if not ctx.scope.allows_site(site.id):
                continue  # never disclose another site's grants to a site-restricted actor
            full = site.id in access.scope.full_site_ids
            sites.append(
                EffectiveSite(
                    site_id=site.id, code=site.code, name=site.name, rack_scope="all" if full else "selected",
                    rack_ids=[] if full else sorted(await _selected_racks(db, access, site.id, ctx.scope), key=str),
                )
            )
    return EffectiveAccessOut(
        user_id=user.id, is_active=user.is_active, unrestricted=access.scope.unrestricted,
        role_names=sorted(access.role_names), groups=groups,
        permissions={c: access.sources[c] for c in sorted(access.permission_codes)},
        denied_permissions=sorted(access.denied_permissions), inactive_permissions=sorted(access.inactive_permissions),
        sites=sites,
    )


async def _selected_racks(db: AsyncSession, access, site_id: uuid.UUID, actor_scope=None) -> list[uuid.UUID]:
    racks = await visible_rack_ids_in_site(db, access.scope, site_id)
    if actor_scope is not None and not actor_scope.unrestricted:
        allowed = set(await visible_rack_ids_in_site(db, actor_scope, site_id))
        racks = [r for r in racks if r in allowed]
    return racks
