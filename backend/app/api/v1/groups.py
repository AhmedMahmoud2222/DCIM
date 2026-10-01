"""Group administration: CRUD, membership, permission grants (allow/deny) and site/rack
access. Effective-permission semantics are documented in docs/USER_GROUP_MANAGEMENT.md."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.access_control import _current_rack_sites, is_scope_independent
from app.application.audit_service import write_audit_log
from app.application.rbac import AuthContext, require_permission
from app.application.user_admin_service import (
    ValidationFailed,
    assert_administrator_remains,
    assert_can_grant_permissions,
    assert_can_grant_sites,
    clean_text,
    get_group_or_404,
    resolve_permission_codes,
)
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domain.auth.models import (
    Permission,
    User,
    UserGroup,
    UserGroupMember,
    UserGroupPermission,
    UserGroupRackAccess,
    UserGroupSiteAccess,
)

router = APIRouter(prefix="/groups", tags=["groups"])

MAX_MEMBERS = 5000
MAX_SITES = 500
MAX_RACKS_PER_SITE = 2000


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class GroupIn(BaseModel):
    name: str = Field(max_length=100)
    description: str | None = Field(default=None, max_length=500)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class SiteAccessOut(BaseModel):
    site_id: uuid.UUID
    site_code: str
    site_name: str
    rack_scope: str
    rack_ids: list[uuid.UUID]


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    member_count: int
    site_count: int


class GroupDetailOut(GroupOut):
    member_ids: list[uuid.UUID]
    allow_permissions: list[str]
    deny_permissions: list[str]
    sites: list[SiteAccessOut]


class PermissionCatalogItem(BaseModel):
    code: str
    resource: str
    action: str
    description: str | None
    site_scoped: bool
    """True when a site-restricted member can use it; False means it is only honoured for
    unrestricted (global-role) users until that endpoint becomes site-aware."""


async def _detail(db: AsyncSession, group: UserGroup) -> GroupDetailOut:
    from app.domain.location.models import Site

    members = list((await db.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group.id))).scalars())
    perm_rows = (
        await db.execute(
            select(Permission.resource, Permission.action, UserGroupPermission.effect)
            .join(UserGroupPermission, UserGroupPermission.permission_id == Permission.id)
            .where(UserGroupPermission.group_id == group.id)
        )
    ).all()
    site_rows = (
        await db.execute(
            select(UserGroupSiteAccess, Site)
            .join(Site, Site.id == UserGroupSiteAccess.site_id)
            .where(UserGroupSiteAccess.group_id == group.id)
            .order_by(Site.code)
        )
    ).all()
    sites: list[SiteAccessOut] = []
    for access, site in site_rows:
        racks = list(
            (await db.execute(select(UserGroupRackAccess.rack_id).where(UserGroupRackAccess.site_access_id == access.id)))
            .scalars()
        )
        sites.append(
            SiteAccessOut(site_id=site.id, site_code=site.code, site_name=site.name, rack_scope=access.rack_scope, rack_ids=racks)
        )
    return GroupDetailOut(
        id=group.id, name=group.name, description=group.description, created_at=group.created_at,
        member_count=len(members), site_count=len(sites), member_ids=members,
        allow_permissions=sorted(f"{r}:{a}" for r, a, e in perm_rows if e == "allow"),
        deny_permissions=sorted(f"{r}:{a}" for r, a, e in perm_rows if e == "deny"),
        sites=sites,
    )


async def _assert_not_member(ctx: AuthContext, db: AsyncSession, group_id: uuid.UUID) -> None:
    """Self-escalation guard: members cannot rewrite the group that grants them access."""
    if await db.get(UserGroupMember, (group_id, ctx.user.id)) is not None:
        raise ForbiddenError("You cannot modify a group you are a member of.")


@router.get("/permission-catalog", response_model=list[PermissionCatalogItem])
async def permission_catalog(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("group:read"))
) -> list[PermissionCatalogItem]:
    rows = (await db.execute(select(Permission).order_by(Permission.resource, Permission.action))).scalars().all()
    return [
        PermissionCatalogItem(
            code=p.code, resource=p.resource, action=p.action, description=p.description,
            site_scoped=is_scope_independent(p.code),
        )
        for p in rows
    ]


@router.get("", response_model=Page[GroupOut])
async def list_groups(
    q: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("group:read")),
) -> Page:
    conds = []
    if q:
        needle = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(func.lower(UserGroup.name).like(f"%{needle}%", escape="\\"))
    total = (await db.execute(select(func.count()).select_from(UserGroup).where(*conds))).scalar_one()
    groups = list(
        (
            await db.execute(
                select(UserGroup).where(*conds).order_by(UserGroup.name).offset(pagination.offset).limit(pagination.limit)
            )
        ).scalars()
    )
    ids = [g.id for g in groups]
    members = dict((await db.execute(
        select(UserGroupMember.group_id, func.count()).where(UserGroupMember.group_id.in_(ids)).group_by(UserGroupMember.group_id)
    )).all()) if ids else {}
    sites = dict((await db.execute(
        select(UserGroupSiteAccess.group_id, func.count()).where(UserGroupSiteAccess.group_id.in_(ids))
        .group_by(UserGroupSiteAccess.group_id)
    )).all()) if ids else {}
    items = [
        GroupOut(
            id=g.id, name=g.name, description=g.description, created_at=g.created_at,
            member_count=members.get(g.id, 0), site_count=sites.get(g.id, 0),
        )
        for g in groups
    ]
    return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("group:read"))
) -> GroupDetailOut:
    return await _detail(db, await get_group_or_404(db, group_id))


@router.post("", response_model=GroupDetailOut, status_code=201)
async def create_group(
    body: GroupIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> GroupDetailOut:
    name = clean_text(body.name, field="name", max_length=100)
    description = None
    if body.description:
        description = clean_text(body.description, field="description", max_length=500, allow_empty=True) or None
    if (await db.execute(select(UserGroup.id).where(func.lower(UserGroup.name) == name.lower()))).first() is not None:
        raise ConflictError(f"A group named {name!r} already exists.")
    group = UserGroup(name=name, description=description)
    db.add(group)
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.create", entity_type="group", entity_id=group.id,
        request_id=request_id, correlation_id=correlation_id, after={"name": name, "description": description},
    )
    await db.commit()
    await db.refresh(group)
    return await _detail(db, group)


@router.patch("/{group_id}", response_model=GroupDetailOut)
async def update_group(
    group_id: uuid.UUID,
    body: GroupUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> GroupDetailOut:
    group = await get_group_or_404(db, group_id)
    await _assert_not_member(ctx, db, group_id)
    before = {"name": group.name, "description": group.description}
    if body.name is not None:
        name = clean_text(body.name, field="name", max_length=100)
        clash = (
            await db.execute(select(UserGroup.id).where(func.lower(UserGroup.name) == name.lower(), UserGroup.id != group.id))
        ).first()
        if clash is not None:
            raise ConflictError(f"A group named {name!r} already exists.")
        group.name = name
    if body.description is not None:
        group.description = clean_text(body.description, field="description", max_length=500, allow_empty=True) or None
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.update", entity_type="group", entity_id=group.id,
        request_id=request_id, correlation_id=correlation_id, before=before,
        after={"name": group.name, "description": group.description},
    )
    await db.commit()
    return await _detail(db, group)


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> None:
    group = await get_group_or_404(db, group_id)
    await _assert_not_member(ctx, db, group_id)
    detail = await _detail(db, group)
    await db.delete(group)
    await db.flush()
    try:
        await assert_administrator_remains(db)
    except ConflictError:
        await db.rollback()
        raise
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.delete", entity_type="group", entity_id=group_id,
        request_id=request_id, correlation_id=correlation_id,
        before={"name": detail.name, "member_count": detail.member_count, "allow": detail.allow_permissions,
                "deny": detail.deny_permissions, "site_ids": [str(s.site_id) for s in detail.sites]},
    )
    await db.commit()


class MembersIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(max_length=MAX_MEMBERS)


@router.put("/{group_id}/members", response_model=GroupDetailOut)
async def set_members(
    group_id: uuid.UUID,
    body: MembersIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> GroupDetailOut:
    from app.application.user_admin_service import assert_can_assign_group

    group = await get_group_or_404(db, group_id)
    await _assert_not_member(ctx, db, group_id)
    target = set(body.user_ids)
    if target:
        found = set((await db.execute(select(User.id).where(User.id.in_(target)))).scalars())
        if found != target:
            raise NotFoundError("One or more users were not found.")
    current = set((await db.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id))).scalars())
    if target - current:
        await assert_can_assign_group(db, ctx, group_id)
    for uid in target - current:
        db.add(UserGroupMember(group_id=group_id, user_id=uid))
    for uid in current - target:
        member = await db.get(UserGroupMember, (group_id, uid))
        if member is not None:
            await db.delete(member)
    await db.flush()
    try:
        await assert_administrator_remains(db)
    except ConflictError:
        await db.rollback()
        raise
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.members.update", entity_type="group", entity_id=group.id,
        request_id=request_id, correlation_id=correlation_id,
        before={"user_ids": sorted(str(u) for u in current)}, after={"user_ids": sorted(str(u) for u in target)},
    )
    await db.commit()
    return await _detail(db, group)


class PermissionsIn(BaseModel):
    allow: list[str] = Field(default_factory=list, max_length=500)
    deny: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _disjoint(self) -> "PermissionsIn":
        overlap = set(self.allow) & set(self.deny)
        if overlap:
            raise ValueError(f"A permission cannot be both allowed and denied: {', '.join(sorted(overlap)[:5])}")
        return self


@router.put("/{group_id}/permissions", response_model=GroupDetailOut)
async def set_permissions(
    group_id: uuid.UUID,
    body: PermissionsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> GroupDetailOut:
    group = await get_group_or_404(db, group_id)
    await _assert_not_member(ctx, db, group_id)
    allow = await resolve_permission_codes(db, body.allow)
    deny = await resolve_permission_codes(db, body.deny)
    assert_can_grant_permissions(ctx, set(allow))
    before = await _detail(db, group)

    for row in (await db.execute(select(UserGroupPermission).where(UserGroupPermission.group_id == group_id))).scalars():
        await db.delete(row)
    await db.flush()
    for perm in allow.values():
        db.add(UserGroupPermission(group_id=group_id, permission_id=perm.id, effect="allow"))
    for perm in deny.values():
        db.add(UserGroupPermission(group_id=group_id, permission_id=perm.id, effect="deny"))
    await db.flush()
    try:
        await assert_administrator_remains(db)
    except ConflictError:
        await db.rollback()
        raise
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.permissions.update", entity_type="group", entity_id=group.id,
        request_id=request_id, correlation_id=correlation_id,
        before={"allow": before.allow_permissions, "deny": before.deny_permissions},
        after={"allow": sorted(allow), "deny": sorted(deny)},
    )
    await db.commit()
    return await _detail(db, group)


class SiteAccessIn(BaseModel):
    site_id: uuid.UUID
    rack_scope: str = "selected"
    rack_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_RACKS_PER_SITE)

    @model_validator(mode="after")
    def _check(self) -> "SiteAccessIn":
        if self.rack_scope not in ("all", "selected"):
            raise ValueError("rack_scope must be 'all' or 'selected'.")
        if self.rack_scope == "all" and self.rack_ids:
            raise ValueError("rack_ids must be empty when rack_scope is 'all'.")
        return self


class SiteAccessSetIn(BaseModel):
    sites: list[SiteAccessIn] = Field(max_length=MAX_SITES)

    @model_validator(mode="after")
    def _unique_sites(self) -> "SiteAccessSetIn":
        ids = [s.site_id for s in self.sites]
        if len(ids) != len(set(ids)):
            raise ValueError("Each site may appear once.")
        return self


@router.put("/{group_id}/site-access", response_model=GroupDetailOut)
async def set_site_access(
    group_id: uuid.UUID,
    body: SiteAccessSetIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("group:manage")),
) -> GroupDetailOut:
    """Replaces the group's whole site/rack grant set. Every rack must currently be placed
    in the site it is granted under (cross-site rack grants are rejected)."""
    group = await get_group_or_404(db, group_id)
    await _assert_not_member(ctx, db, group_id)
    site_ids = {s.site_id for s in body.sites}
    rack_ids = {r for s in body.sites for r in s.rack_ids}
    await assert_can_grant_sites(db, ctx, site_ids, rack_ids)

    if rack_ids:
        sub = _current_rack_sites().subquery()
        rows = (await db.execute(select(sub.c.rack_id, sub.c.site_id).where(sub.c.rack_id.in_(rack_ids)))).all()
        placed: dict[uuid.UUID, uuid.UUID] = {rack: site for rack, site in rows}
        for entry in body.sites:
            for rid in entry.rack_ids:
                if placed.get(rid) != entry.site_id:
                    raise ValidationFailed(f"Rack {rid} is not currently placed in site {entry.site_id}.")

    before = await _detail(db, group)
    for row in (await db.execute(select(UserGroupSiteAccess).where(UserGroupSiteAccess.group_id == group_id))).scalars():
        await db.delete(row)
    await db.flush()
    for entry in body.sites:
        access = UserGroupSiteAccess(group_id=group_id, site_id=entry.site_id, rack_scope=entry.rack_scope)
        db.add(access)
        await db.flush()
        for rid in dict.fromkeys(entry.rack_ids):
            db.add(UserGroupRackAccess(site_access_id=access.id, rack_id=rid))
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Site access could not be saved; a site or rack no longer exists.") from exc
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="group.site_access.update", entity_type="group", entity_id=group.id,
        request_id=request_id, correlation_id=correlation_id,
        before={"sites": [{"site_id": str(s.site_id), "rack_scope": s.rack_scope, "rack_ids": [str(r) for r in s.rack_ids]}
                          for s in before.sites]},
        after={"sites": [{"site_id": str(s.site_id), "rack_scope": s.rack_scope, "rack_ids": [str(r) for r in s.rack_ids]}
                         for s in body.sites]},
    )
    await db.commit()
    return await _detail(db, group)
