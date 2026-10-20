"""Effective access with source-bound permission scopes (SEC-ARCH-02 / #163).

Each group's allow-grant is bound to that group's own site/rack scope. Only
sources granting the same permission may union their scopes. A global role
makes only its own permissions global; explicit group denies still win user-wide.
Unsupported scoped permissions remain inactive for unfiltered endpoints.

`EffectiveAccess.scope` retains aggregate reach for directory masking and the
unchanged SEC-ARCH-01 scoped-role eligibility rules. Resource authorization uses
`scope_for(code)`, never that aggregate. Selected racks remain pinned to their
current placement site. Filtered thermal views intersect their required read
entitlements; unsupported unfiltered endpoints remain closed.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, false, func, literal, or_, select, true
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
    {
        "organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read",
        # Issue #101: cables and traces are filtered by the visibility of their endpoint equipment.
        "cable:read", "cable:manage",
        # Issue #105: cooling configuration is filtered by site (units, groups, zones need full-site scope) and
        # sensor data by the same site/rack visibility clause used for equipment, before any aggregation.
        "cooling:read", "cooling:manage",
    }
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
EMPTY_SCOPE = AccessScope(unrestricted=False)


def union_scopes(left: AccessScope, right: AccessScope) -> AccessScope:
    """Union resources for the SAME permission; never combine unrelated permissions."""
    if left.unrestricted or right.unrestricted:
        return UNRESTRICTED
    full = left.full_site_ids | right.full_site_ids
    selected = {
        site: left.selected_racks_by_site.get(site, frozenset()) | right.selected_racks_by_site.get(site, frozenset())
        for site in (left.site_ids | right.site_ids) - full
    }
    return AccessScope(
        unrestricted=False, site_ids=left.site_ids | right.site_ids, full_site_ids=full,
        rack_ids=frozenset(r for racks in selected.values() for r in racks), selected_racks_by_site=selected,
    )


def intersect_scopes(left: AccessScope, right: AccessScope) -> AccessScope:
    """Resources authorized by BOTH permissions of a compound operation."""
    if left.unrestricted:
        return right
    if right.unrestricted:
        return left
    sites = left.site_ids & right.site_ids
    full = left.full_site_ids & right.full_site_ids
    selected = {}
    for site in sites - full:
        a = left.selected_racks_by_site.get(site, frozenset())
        b = right.selected_racks_by_site.get(site, frozenset())
        selected[site] = b if site in left.full_site_ids else a if site in right.full_site_ids else a & b
    return AccessScope(
        unrestricted=False, site_ids=sites, full_site_ids=full,
        rack_ids=frozenset(r for racks in selected.values() for r in racks), selected_racks_by_site=selected,
    )


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
    permission_scopes: dict[str, AccessScope] = field(default_factory=dict)
    """Non-denied source scopes, including inactive grants for filtered thermal views/preflight.
    `scope` is aggregate reach for legacy-role eligibility/directory visibility, not authorization."""

    def scope_for(self, code: str, *, include_inactive: bool = False) -> AccessScope:
        if code not in self.permission_codes and not (include_inactive and code in self.inactive_permissions):
            return EMPTY_SCOPE
        return self.permission_scopes.get(code, EMPTY_SCOPE)


def scoped_role_is_contained(
    scope_type: str,
    scope_id: uuid.UUID | None,
    *,
    unrestricted: bool,
    scope: "AccessScope",
    organization_site_ids: frozenset[uuid.UUID] = frozenset(),
) -> bool:
    """May the permissions of a NON-global role assignment be honoured?

    Permissions are one flat set per user, so a scoped assignment cannot be applied to its own
    scope only. It is therefore honoured only when everything the user can reach (`scope`) lies
    inside the assignment's scope, so it can never act outside it. Fail closed otherwise:
      * a user with a global role is unrestricted, which no scoped assignment contains;
      * `site` assignments need every reachable site to equal the assignment's site;
      * `organization` assignments need every reachable site to belong to that organization
        (`organization_site_ids`, the sites under it via Country -> City -> Site);
      * any other value contributes nothing.
    """
    if unrestricted or scope_id is None:
        return False
    if scope_type == "site":
        return scope.site_ids <= {scope_id}
    if scope_type == "organization":
        return scope.site_ids <= organization_site_ids
    return False


def scope_with_full_sites(scope: AccessScope, site_ids: frozenset[uuid.UUID]) -> AccessScope:
    """`scope` plus `site_ids` granted as rack_scope='all' (an `all` grant wins over `selected`)."""
    if not site_ids:
        return scope
    full = scope.full_site_ids | site_ids
    selected = {k: v for k, v in scope.selected_racks_by_site.items() if k not in full}
    return AccessScope(
        unrestricted=False,
        site_ids=scope.site_ids | site_ids,
        full_site_ids=full,
        rack_ids=frozenset(r for racks in selected.values() for r in racks),
        selected_racks_by_site=selected,
    )


async def organization_site_ids(
    db: AsyncSession, organization_ids: set[uuid.UUID]
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """organization id -> ids of every site under it (Organization -> Country -> City -> Site).
    An unknown organization, or one with no sites, maps to the empty set."""
    found: dict[uuid.UUID, set[uuid.UUID]] = {oid: set() for oid in organization_ids}
    if organization_ids:
        for org_id, site_id in (
            await db.execute(
                select(Country.organization_id, Site.id)
                .join(City, City.country_id == Country.id)
                .join(Site, Site.city_id == City.id)
                .where(Country.organization_id.in_(organization_ids))
            )
        ).all():
            found[org_id].add(site_id)
    return {oid: frozenset(sites) for oid, sites in found.items()}


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
    """One PostgreSQL statement/snapshot for permissions, membership and resource grants.

    Separate READ COMMITTED queries can mix an old permission with a newly moved
    site grant, inventing an entitlement present in neither committed state. A
    UNION ALL of tagged JSONB rows keeps every source in the same MVCC snapshot,
    without taking a shared authority lock that admin requests would need to upgrade.
    """
    if not user_ids:
        return {}
    wanted = list(set(user_ids))

    role_query = (
            select(literal("role").label("kind"), func.jsonb_build_array(
                RoleAssignment.user_id, RoleAssignment.scope_type, RoleAssignment.scope_id, Role.name,
                Permission.resource, Permission.action,
            ).label("data"))
            .select_from(RoleAssignment)
            .join(Role, Role.id == RoleAssignment.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .where(RoleAssignment.user_id.in_(wanted))
    )
    group_query = (
            select(literal("group"), func.jsonb_build_array(
                UserGroupMember.user_id, UserGroup.id, UserGroup.name, Permission.resource, Permission.action,
                UserGroupPermission.effect,
            ))
            .select_from(UserGroupMember)
            .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
            .outerjoin(UserGroupPermission, UserGroupPermission.group_id == UserGroup.id)
            .outerjoin(Permission, Permission.id == UserGroupPermission.permission_id)
            .where(UserGroupMember.user_id.in_(wanted))
    )
    site_query = (
            select(literal("site"), func.jsonb_build_array(
                UserGroupMember.user_id, UserGroupSiteAccess.id, UserGroupSiteAccess.site_id,
                UserGroupSiteAccess.rack_scope, UserGroupSiteAccess.group_id,
            ))
            .select_from(UserGroupMember)
            .join(UserGroupSiteAccess, UserGroupSiteAccess.group_id == UserGroupMember.group_id)
            .where(UserGroupMember.user_id.in_(wanted))
    )
    rack_query = (
        select(literal("rack"), func.jsonb_build_array(UserGroupRackAccess.site_access_id, UserGroupRackAccess.rack_id))
        .select_from(UserGroupRackAccess)
        .join(UserGroupSiteAccess, UserGroupSiteAccess.id == UserGroupRackAccess.site_access_id)
        .join(UserGroupMember, UserGroupMember.group_id == UserGroupSiteAccess.group_id)
        .where(UserGroupMember.user_id.in_(wanted))
    )
    org_query = (
        select(literal("organization"), func.jsonb_build_array(RoleAssignment.scope_id, Site.id))
        .select_from(RoleAssignment)
        .join(Country, Country.organization_id == RoleAssignment.scope_id)
        .join(City, City.country_id == Country.id)
        .join(Site, Site.city_id == City.id)
        .where(RoleAssignment.user_id.in_(wanted), RoleAssignment.scope_type == "organization")
    )
    rows = (await db.execute(role_query.union_all(group_query, site_query, rack_query, org_query))).all()
    role_rows, group_perm_rows, site_rows = [], [], []
    rack_by_access: dict[uuid.UUID, set[uuid.UUID]] = {}
    org_sets: dict[uuid.UUID, set[uuid.UUID]] = {}
    for kind, data in rows:
        if kind == "role":
            uid, scope_type, scope_id, name, resource, action = data
            role_rows.append((uuid.UUID(uid), scope_type, uuid.UUID(scope_id) if scope_id else None, name, resource, action))
            if scope_type == "organization" and scope_id:
                org_sets.setdefault(uuid.UUID(scope_id), set())
        elif kind == "group":
            uid, gid, name, resource, action, effect = data
            group_perm_rows.append((uuid.UUID(uid), uuid.UUID(gid), name, resource, action, effect))
        elif kind == "site":
            uid, access_id, site_id, rack_scope, gid = data
            site_rows.append((uuid.UUID(uid), uuid.UUID(access_id), uuid.UUID(site_id), rack_scope, uuid.UUID(gid)))
        elif kind == "rack":
            access_id, rack_id = data
            rack_by_access.setdefault(uuid.UUID(access_id), set()).add(uuid.UUID(rack_id))
        else:
            org_id, site_id = data
            org_sets.setdefault(uuid.UUID(org_id), set()).add(uuid.UUID(site_id))
    org_sites = {oid: frozenset(sites) for oid, sites in org_sets.items()}

    roles_by_user: dict[uuid.UUID, list] = {}
    for role_row in role_rows:
        roles_by_user.setdefault(role_row[0], []).append(role_row)
    perms_by_user: dict[uuid.UUID, list] = {}
    for perm_row in group_perm_rows:
        perms_by_user.setdefault(perm_row[0], []).append(perm_row)
    sites_by_user: dict[uuid.UUID, list] = {}
    for site_row in site_rows:
        sites_by_user.setdefault(site_row[0], []).append(site_row)
    sites_by_group: dict[uuid.UUID, list] = {}
    for _uid, access_id, site_id, rack_scope, gid in site_rows:
        sites_by_group.setdefault(gid, []).append((access_id, site_id, rack_scope))
    group_scopes = {gid: build_scope(rows, rack_by_access) for gid, rows in sites_by_group.items()}

    result: dict[uuid.UUID, EffectiveAccess] = {}
    for uid in wanted:
        allowed: dict[str, list[str]] = {}
        denied: set[str] = set()
        role_names: set[str] = set()
        has_global_role = False
        group_ids: set[uuid.UUID] = set()
        permission_scopes: dict[str, AccessScope] = {}
        user_scope = build_scope(
            [(r[1], r[2], r[3]) for r in sites_by_user.get(uid, [])],
            rack_by_access,
        )
        user_roles = roles_by_user.get(uid, [])
        has_global_role = any(row[1] == "global" for row in user_roles)
        # SEC-ARCH-01 organization scope: an `organization` assignment adds every site under its
        # organization (rack_scope='all') to the user's reach, independent of group grants, but only
        # when the user's group sites already lie inside that organization, so no site of another
        # organization is ever reachable with the role's permissions. Assignments naming two or more
        # distinct organizations cannot be kept apart in one flat permission set: all fail closed.
        # It never makes the user unrestricted; only a `global` assignment does.
        org_ids = {row[2] for row in user_roles if row[1] == "organization" and row[2] is not None}
        if not has_global_role and len(org_ids) == 1:
            (honoured_org,) = org_ids
            if user_scope.site_ids <= org_sites[honoured_org]:
                user_scope = scope_with_full_sites(user_scope, org_sites[honoured_org])
        for _uid, scope_type, scope_id, role_name, resource, action in user_roles:
            if scope_type != "global" and not (
                (scope_type != "organization" or len(org_ids) == 1)
                and scoped_role_is_contained(
                    scope_type, scope_id, unrestricted=has_global_role, scope=user_scope,
                    organization_site_ids=org_sites.get(scope_id, frozenset()),
                )
            ):
                # SEC-ARCH-01: a scoped assignment never widens beyond its own scope. Its role name is
                # dropped too, so it cannot satisfy role-membership checks such as "Administrator".
                continue
            if scope_type == "organization":
                # Organization scope bounds data visibility only; role-name checks (catalog administration,
                # bulk import) are organization-independent and stay reserved for in-scope global/site roles.
                if resource is not None:
                    code = f"{resource}:{action}"
                    allowed.setdefault(code, []).append(f"role:{role_name}")
                    permission_scopes[code] = union_scopes(permission_scopes.get(code, EMPTY_SCOPE), user_scope)
                continue
            role_names.add(role_name)
            if resource is not None:
                code = f"{resource}:{action}"
                allowed.setdefault(code, []).append(f"role:{role_name}")
                source_scope = UNRESTRICTED if scope_type == "global" else user_scope
                permission_scopes[code] = union_scopes(permission_scopes.get(code, EMPTY_SCOPE), source_scope)
        for _uid, group_id, group_name, resource, action, effect in perms_by_user.get(uid, []):
            group_ids.add(group_id)
            if resource is None:
                continue
            code = f"{resource}:{action}"
            if effect == "deny":
                denied.add(code)
            else:
                allowed.setdefault(code, []).append(f"group:{group_name}")
                permission_scopes[code] = union_scopes(
                    permission_scopes.get(code, EMPTY_SCOPE), group_scopes.get(group_id, EMPTY_SCOPE)
                )

        unrestricted = has_global_role
        effective = {code for code in allowed if code not in denied}
        inactive: set[str] = set()
        inactive = {
            code for code in effective
            if not permission_scopes[code].unrestricted and not is_scope_independent(code)
        }
        effective -= inactive
        scope = UNRESTRICTED if unrestricted else user_scope
        result[uid] = EffectiveAccess(
            user_id=uid, permission_codes=frozenset(effective), role_names=frozenset(role_names), scope=scope,
            denied_permissions=frozenset(denied & set(allowed)), inactive_permissions=frozenset(inactive),
            group_ids=frozenset(group_ids), sources=allowed,
            permission_scopes={code: value for code, value in permission_scopes.items() if code not in denied},
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
    lit = literal(uuid.UUID(str(rack_id)))  # see ensure_equipment_access: asyncpg's UUID subclass has no inferable bind type
    if (await db.execute(select(lit).where(lit.in_(visible_rack_ids_query(scope))))).first() is None:
        raise NotFoundError(f"Rack {rack_id} not found.")


async def ensure_equipment_access(db: AsyncSession, scope: AccessScope, equipment_id: uuid.UUID) -> None:
    if scope.unrestricted:
        return
    # asyncpg returns its own UUID subclass for ids loaded from the database; `literal()` cannot infer a
    # bind type for that subclass and PostgreSQL then rejects the statement as ambiguous. Coerce to a plain UUID.
    lit = literal(uuid.UUID(str(equipment_id)))
    hit = (await db.execute(select(lit).where(equipment_visible_clause(scope, lit)))).first()
    if hit is None:
        raise NotFoundError(f"Equipment {equipment_id} not found.")


async def is_user_admin(access: EffectiveAccess | None) -> bool:
    return access is not None and {"user:manage", "group:manage"} <= access.permission_codes


async def active_administrator_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Active unrestricted users with both effective management permissions."""
    active = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())
    accesses = await load_effective_access(db, active)
    return [
        uid for uid, acc in accesses.items()
        if all(acc.scope_for(code).unrestricted for code in ("user:manage", "group:manage"))
    ]
