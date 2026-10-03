"""Effective-access engine for user groups (docs/USER_GROUP_MANAGEMENT.md).

Permission rules
  1. allowed  = role permissions (legacy RoleAssignment) UNION group `allow` grants
  2. denied   = UNION of group `deny` grants over every active group the user is in
  3. effective = allowed MINUS denied. An explicit deny beats any allow, from any group or
     any role. There is no ordering between groups and no inheritance between groups.

Data-scope rules
  * A user holding a `global` RoleAssignment is UNRESTRICTED: existing users keep exactly
    the access they had before groups existed.
  * Every other user is RESTRICTED: they see only the sites granted by their groups (union
    across groups). Within a site, `rack_scope=all` exposes every rack currently placed
    there; `rack_scope=selected` exposes only the listed racks. If any group grants `all`
    on a site, `all` wins for that site. No groups and no roles means no access at all.
  * Restricted users only keep permissions whose endpoints filter by site/rack
    (SCOPE_AWARE_PERMISSIONS) plus user/group administration. Anything else is reported as
    `inactive_permissions` and is not honoured, so a group grant can never expose an
    endpoint that is not yet site-aware (fail closed).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, false, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import NotFoundError
from app.domain.auth.models import (
    Permission,
    Role,
    RoleAssignment,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
    UserGroupPermission,
    UserGroupRackAccess,
    UserGroupSiteAccess,
)
from app.domain.location.models import Building, City, Country, Floor, Room, Site
from app.domain.placement.models import EquipmentPlacement, RackPlacement

SCOPE_AWARE_PERMISSIONS = frozenset(
    {"organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"}
)
UNSCOPED_ADMIN_RESOURCES = frozenset({"user", "group"})


def is_scope_independent(code: str) -> bool:
    """Permissions that a restricted user may exercise: site-aware data permissions, and
    user/group administration (which exposes no site data)."""
    return code in SCOPE_AWARE_PERMISSIONS or code.split(":", 1)[0] in UNSCOPED_ADMIN_RESOURCES


@dataclass(frozen=True)
class AccessScope:
    unrestricted: bool
    site_ids: frozenset[uuid.UUID] = frozenset()
    full_site_ids: frozenset[uuid.UUID] = frozenset()
    rack_ids: frozenset[uuid.UUID] = frozenset()
    selected_racks_by_site: Mapping[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    """site id -> racks granted by `selected` grants on that site (empty for `all` sites)."""

    def allows_site(self, site_id: uuid.UUID | None) -> bool:
        return self.unrestricted or (site_id is not None and site_id in self.site_ids)

    def contains(self, inner: "AccessScope") -> bool:
        """True when everything `inner` can reach, this scope can reach too. Compares the
        grants themselves (site, all-vs-selected, per-site racks), so a `selected` scope
        never contains an `all` scope on the same site."""
        if self.unrestricted:
            return True
        if inner.unrestricted:
            return False
        if not inner.site_ids <= self.site_ids or not inner.full_site_ids <= self.full_site_ids:
            return False
        for site_id in inner.site_ids - inner.full_site_ids:
            if site_id in self.full_site_ids:
                continue
            if not inner.selected_racks_by_site.get(site_id, frozenset()) <= self.selected_racks_by_site.get(
                site_id, frozenset()
            ):
                return False
        return True


UNRESTRICTED = AccessScope(unrestricted=True)


@dataclass
class EffectiveAccess:
    user_id: uuid.UUID
    permission_codes: frozenset[str]
    role_names: frozenset[str]
    scope: AccessScope
    denied_permissions: frozenset[str] = frozenset()
    inactive_permissions: frozenset[str] = frozenset()
    group_ids: frozenset[uuid.UUID] = frozenset()
    sources: dict[str, list[str]] = field(default_factory=dict)  # code -> ["role:Viewer", "group:Ops"]


def build_scope(
    site_rows: list[tuple[uuid.UUID, uuid.UUID, str]], rack_by_access: dict[uuid.UUID, set[uuid.UUID]]
) -> AccessScope:
    """site_rows: (site_access_id, site_id, rack_scope) grants; rack_by_access: selected racks."""
    site_ids: set[uuid.UUID] = set()
    full_sites: set[uuid.UUID] = set()
    selected: dict[uuid.UUID, set[uuid.UUID]] = {}
    for access_id, site_id, rack_scope in site_rows:
        site_ids.add(site_id)
        if rack_scope == "all":
            full_sites.add(site_id)
        else:
            selected.setdefault(site_id, set()).update(rack_by_access.get(access_id, set()))
    return AccessScope(
        unrestricted=False,
        site_ids=frozenset(site_ids),
        full_site_ids=frozenset(full_sites),
        rack_ids=frozenset(r for racks in selected.values() for r in racks),
        selected_racks_by_site={k: frozenset(v) for k, v in selected.items() if k not in full_sites},
    )


async def load_group_scopes(db: AsyncSession, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, AccessScope]:
    """The site/rack grants of each group, as a scope (empty scope when it grants nothing)."""
    result = {gid: AccessScope(unrestricted=False) for gid in group_ids}
    if not group_ids:
        return result
    rows = (
        await db.execute(
            select(
                UserGroupSiteAccess.group_id, UserGroupSiteAccess.id, UserGroupSiteAccess.site_id,
                UserGroupSiteAccess.rack_scope,
            ).where(UserGroupSiteAccess.group_id.in_(group_ids))
        )
    ).all()
    access_ids = {r[1] for r in rows}
    racks: dict[uuid.UUID, set[uuid.UUID]] = {}
    if access_ids:
        for access_id, rack_id in (
            await db.execute(
                select(UserGroupRackAccess.site_access_id, UserGroupRackAccess.rack_id).where(
                    UserGroupRackAccess.site_access_id.in_(access_ids)
                )
            )
        ).all():
            racks.setdefault(access_id, set()).add(rack_id)
    by_group: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID, str]]] = {}
    for gid, access_id, site_id, rack_scope in rows:
        by_group.setdefault(gid, []).append((access_id, site_id, rack_scope))
    for gid, grants in by_group.items():
        result[gid] = build_scope(grants, racks)
    return result


async def load_effective_access(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, EffectiveAccess]:
    """Batch-evaluates effective access for the given users (4 queries, independent of N)."""
    if not user_ids:
        return {}
    wanted = list(set(user_ids))

    role_rows = (
        await db.execute(
            select(RoleAssignment.user_id, RoleAssignment.scope_type, Role.name, Permission.resource, Permission.action)
            .select_from(RoleAssignment)
            .join(Role, Role.id == RoleAssignment.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .where(RoleAssignment.user_id.in_(wanted))
        )
    ).all()
    group_perm_rows = (
        await db.execute(
            select(
                UserGroupMember.user_id, UserGroup.id, UserGroup.name, Permission.resource, Permission.action,
                UserGroupPermission.effect,
            )
            .select_from(UserGroupMember)
            .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
            .outerjoin(UserGroupPermission, UserGroupPermission.group_id == UserGroup.id)
            .outerjoin(Permission, Permission.id == UserGroupPermission.permission_id)
            .where(UserGroupMember.user_id.in_(wanted))
        )
    ).all()
    site_rows = (
        await db.execute(
            select(UserGroupMember.user_id, UserGroupSiteAccess.id, UserGroupSiteAccess.site_id, UserGroupSiteAccess.rack_scope)
            .select_from(UserGroupMember)
            .join(UserGroupSiteAccess, UserGroupSiteAccess.group_id == UserGroupMember.group_id)
            .where(UserGroupMember.user_id.in_(wanted))
        )
    ).all()
    site_access_ids = {row[1] for row in site_rows}
    rack_by_access: dict[uuid.UUID, set[uuid.UUID]] = {}
    if site_access_ids:
        for access_id, rack_id in (
            await db.execute(
                select(UserGroupRackAccess.site_access_id, UserGroupRackAccess.rack_id).where(
                    UserGroupRackAccess.site_access_id.in_(site_access_ids)
                )
            )
        ).all():
            rack_by_access.setdefault(access_id, set()).add(rack_id)

    roles_by_user: dict[uuid.UUID, list] = {}
    for role_row in role_rows:
        roles_by_user.setdefault(role_row[0], []).append(role_row)
    perms_by_user: dict[uuid.UUID, list] = {}
    for perm_row in group_perm_rows:
        perms_by_user.setdefault(perm_row[0], []).append(perm_row)
    sites_by_user: dict[uuid.UUID, list] = {}
    for site_row in site_rows:
        sites_by_user.setdefault(site_row[0], []).append(site_row)

    result: dict[uuid.UUID, EffectiveAccess] = {}
    for uid in wanted:
        allowed: dict[str, list[str]] = {}
        denied: set[str] = set()
        role_names: set[str] = set()
        has_global_role = False
        group_ids: set[uuid.UUID] = set()
        for _uid, scope_type, role_name, resource, action in roles_by_user.get(uid, []):
            role_names.add(role_name)
            has_global_role = has_global_role or scope_type == "global"
            if resource is not None:
                allowed.setdefault(f"{resource}:{action}", []).append(f"role:{role_name}")
        for _uid, group_id, group_name, resource, action, effect in perms_by_user.get(uid, []):
            group_ids.add(group_id)
            if resource is None:
                continue
            code = f"{resource}:{action}"
            if effect == "deny":
                denied.add(code)
            else:
                allowed.setdefault(code, []).append(f"group:{group_name}")

        user_scope = build_scope(
            [(access_id, site_id, rack_scope) for _uid, access_id, site_id, rack_scope in sites_by_user.get(uid, [])],
            rack_by_access,
        )

        unrestricted = has_global_role
        effective = {code for code in allowed if code not in denied}
        inactive: set[str] = set()
        if not unrestricted:
            inactive = {code for code in effective if not is_scope_independent(code)}
            effective -= inactive
        scope = UNRESTRICTED if unrestricted else user_scope
        result[uid] = EffectiveAccess(
            user_id=uid, permission_codes=frozenset(effective), role_names=frozenset(role_names), scope=scope,
            denied_permissions=frozenset(denied & set(allowed)), inactive_permissions=frozenset(inactive),
            group_ids=frozenset(group_ids), sources=allowed,
        )
    return result


# ------------------------------------------------------------------ SQL scoping helpers
def _current_rack_sites() -> Select:
    return (
        select(RackPlacement.rack_id.label("rack_id"), Building.site_id.label("site_id"))
        .select_from(RackPlacement)
        .join(Room, Room.id == RackPlacement.room_id)
        .join(Floor, Floor.id == Room.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .where(RackPlacement.effective_to.is_(None))
    )


def visible_rack_ids_query(scope: AccessScope) -> Select:
    """Rack ids the scope may see: current placement is in a granted site, and the site is
    `all`, or the rack was explicitly selected. A stale grant (rack moved to another site)
    therefore never leaks: the current placement site is what is checked."""
    sub = _current_rack_sites().subquery()
    conds: list[ColumnElement[bool]] = []
    if scope.full_site_ids:
        conds.append(sub.c.site_id.in_(scope.full_site_ids))
    # A selected rack is visible only in the site it was granted under: a rack granted on
    # site A that moved to site B (where the user holds a different grant) stays hidden.
    for site_id, racks in scope.selected_racks_by_site.items():
        if racks and site_id not in scope.full_site_ids:
            conds.append(and_(sub.c.site_id == site_id, sub.c.rack_id.in_(racks)))
    return select(sub.c.rack_id).where(or_(*conds) if conds else false())


def rack_ids_in_site_query(site_id: uuid.UUID) -> Select:
    sub = _current_rack_sites().subquery()
    return select(sub.c.rack_id).where(sub.c.site_id == site_id)


async def visible_rack_ids_in_site(db: AsyncSession, scope: AccessScope, site_id: uuid.UUID) -> list[uuid.UUID]:
    sub = _current_rack_sites().subquery()
    visible = visible_rack_ids_query(scope).subquery()
    return list(
        (await db.execute(select(sub.c.rack_id).where(sub.c.site_id == site_id, sub.c.rack_id.in_(select(visible.c.rack_id)))))
        .scalars()
    )


def rack_visible_clause(scope: AccessScope, rack_id_col) -> ColumnElement[bool]:
    return true() if scope.unrestricted else rack_id_col.in_(visible_rack_ids_query(scope))


def equipment_visible_clause(scope: AccessScope, equipment_id_col) -> ColumnElement[bool]:
    """Equipment is visible when its current placement is in a visible rack, or is placed
    directly in a room of a site granted with rack_scope='all'."""
    if scope.unrestricted:
        return true()
    placement = (
        select(EquipmentPlacement.equipment_id)
        .join(Room, Room.id == EquipmentPlacement.room_id)
        .join(Floor, Floor.id == Room.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .where(EquipmentPlacement.effective_to.is_(None))
    )
    in_visible_rack = placement.where(EquipmentPlacement.rack_id.in_(visible_rack_ids_query(scope)))
    if scope.full_site_ids:
        # Only rack-less equipment is matched by its room: a rack-mounted item follows its
        # rack, because equipment_placement.room_id is not updated when the rack moves.
        in_full_site = placement.where(
            Building.site_id.in_(scope.full_site_ids), EquipmentPlacement.rack_id.is_(None)
        )
        return or_(equipment_id_col.in_(in_visible_rack), equipment_id_col.in_(in_full_site))
    return equipment_id_col.in_(in_visible_rack)


def site_visible_clause(scope: AccessScope, site_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return site_id_col.in_(scope.site_ids) if scope.site_ids else false()


def building_visible_clause(scope: AccessScope, building_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return building_id_col.in_(select(Building.id).where(site_visible_clause(scope, Building.site_id)))


def floor_visible_clause(scope: AccessScope, floor_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return floor_id_col.in_(
        select(Floor.id).join(Building, Building.id == Floor.building_id).where(site_visible_clause(scope, Building.site_id))
    )


def room_visible_clause(scope: AccessScope, room_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return room_id_col.in_(
        select(Room.id)
        .join(Floor, Floor.id == Room.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .where(site_visible_clause(scope, Building.site_id))
    )


def city_visible_clause(scope: AccessScope, city_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return city_id_col.in_(select(Site.city_id).where(site_visible_clause(scope, Site.id)))


def country_visible_clause(scope: AccessScope, country_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return country_id_col.in_(
        select(City.country_id).join(Site, Site.city_id == City.id).where(site_visible_clause(scope, Site.id))
    )


def organization_visible_clause(scope: AccessScope, org_id_col) -> ColumnElement[bool]:
    if scope.unrestricted:
        return true()
    return org_id_col.in_(
        select(Country.organization_id)
        .join(City, City.country_id == Country.id)
        .join(Site, Site.city_id == City.id)
        .where(site_visible_clause(scope, Site.id))
    )


# ------------------------------------------------------------------ Per-entity guards
# All guards raise 404 (not 403) so a restricted user cannot probe which ids exist.
async def ensure_site_access(scope: AccessScope, site_id: uuid.UUID, what: str = "Site") -> None:
    if not scope.allows_site(site_id):
        raise NotFoundError(f"{what} {site_id} not found.")


async def site_id_for_room(db: AsyncSession, room_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Building.site_id)
            .select_from(Room)
            .join(Floor, Floor.id == Room.floor_id)
            .join(Building, Building.id == Floor.building_id)
            .where(Room.id == room_id)
        )
    ).scalar_one_or_none()


async def ensure_room_access(db: AsyncSession, scope: AccessScope, room_id: uuid.UUID) -> None:
    if scope.unrestricted:
        return
    if not scope.allows_site(await site_id_for_room(db, room_id)):
        raise NotFoundError(f"Room {room_id} not found.")


async def ensure_rack_access(db: AsyncSession, scope: AccessScope, rack_id: uuid.UUID) -> None:
    if scope.unrestricted:
        return
    lit = literal(rack_id)
    if (await db.execute(select(lit).where(lit.in_(visible_rack_ids_query(scope))))).first() is None:
        raise NotFoundError(f"Rack {rack_id} not found.")


async def ensure_equipment_access(db: AsyncSession, scope: AccessScope, equipment_id: uuid.UUID) -> None:
    if scope.unrestricted:
        return
    lit = literal(equipment_id)
    hit = (await db.execute(select(lit).where(equipment_visible_clause(scope, lit)))).first()
    if hit is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")


async def is_user_admin(access: EffectiveAccess | None) -> bool:
    return access is not None and {"user:manage", "group:manage"} <= access.permission_codes


async def active_administrator_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Active users who can still manage both users and groups (the 'administrators')."""
    active = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())
    accesses = await load_effective_access(db, active)
    return [uid for uid, acc in accesses.items() if {"user:manage", "group:manage"} <= acc.permission_codes]
