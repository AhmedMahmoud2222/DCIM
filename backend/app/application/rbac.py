"""RBAC enforcement (§39 of v1.0, §32/§32a). H7 = Option B: Phase 1 enforces permissions
globally only — RoleAssignment.scope_type/scope_id are populated but never filtered on.
Every protected route depends on require_permission(), never on frontend hiding (§13 of
the Phase 1 prompt).

Phase 10A PR-2 (docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-designer-
design.md §9.1, aligned plan §3.2) adds `AuthContext.role_names`/`has_role()` and
`require_catalog_administrator()`, additive and non-breaking to every existing
`require_permission()` call site: catalog mutation requires both the relevant `catalog:*`
permission and Administrator role membership, not permission alone — see
`require_catalog_administrator()`'s own docstring below for why this is a narrowly scoped
exception, not a new general mechanism."""

from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.application.access_control import AccessScope, load_effective_access
from app.core.errors import ForbiddenError
from app.domain.auth.models import User

# Default role -> permission-code seed, per ARCHITECTURE_REVIEW.md v1.0 §6.1 role list.
# Custom roles remain fully supported (Role is a normal table); this is only the seed set
# a fresh Phase 1 database bootstraps with.
DEFAULT_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "Administrator": [
        "organization:read",
        "organization:manage",
        "location:read",
        "location:update",
        "location:manage",
        "managed_asset:read",
        "managed_asset:manage",
        "managed_asset:update_lifecycle",
        "user:manage",
        "user:read",
        "group:read",
        "group:manage",
        "role:manage",
        "audit:view",
        "rack:read",
        "rack:manage",
        "rack:place",
        "rack:import",
        "equipment:read",
        "equipment:manage",
        "equipment:place",
        "equipment:import",
        "floor_plan:read",
        "floor_plan:import",
        "floor_plan:manage",
        "spatial:read",
        "power:read",
        "power:manage",
        "capacity:read",
        "capacity:manage",
        "dashboard:read",
        "integration:read",
        "integration:manage",
        "collector:read",
        "collector:manage",
        "collector:assign",
        "discovery:read",
        "discovery:reconcile",
        "telemetry:read",
        "telemetry:manage",
        "alarm:read",
        "alarm:manage",
        "catalog:read",
        "catalog:read_draft",
        "catalog:manage",
        "catalog:publish",
        "catalog:retire",
        "catalog:import",
        "catalog:migrate",
    ],
    "DCIM Manager": [
        "organization:read",
        "location:read",
        "location:update",
        "location:manage",
        "managed_asset:read",
        "managed_asset:manage",
        "managed_asset:update_lifecycle",
        "audit:view",
        "rack:read",
        "rack:manage",
        "rack:place",
        "rack:import",
        "equipment:read",
        "equipment:manage",
        "equipment:place",
        "equipment:import",
        "floor_plan:read",
        "floor_plan:import",
        "floor_plan:manage",
        "spatial:read",
        "power:read",
        "power:manage",
        "capacity:read",
        "capacity:manage",
        "dashboard:read",
        "integration:read",
        "integration:manage",
        "collector:read",
        "collector:manage",
        "collector:assign",
        "discovery:read",
        "discovery:reconcile",
        "telemetry:read",
        "telemetry:manage",
        "alarm:read",
        "alarm:manage",
        "catalog:read",
    ],
    "Engineer": [
        "organization:read",
        "location:read",
        "location:update",
        "managed_asset:read",
        "managed_asset:manage",
        "managed_asset:update_lifecycle",
        "rack:read",
        "rack:manage",
        "rack:place",
        "rack:import",
        "equipment:read",
        "equipment:manage",
        "equipment:place",
        "equipment:import",
        "floor_plan:read",
        "floor_plan:import",
        "spatial:read",
        "power:read",
        "power:manage",
        "capacity:read",
        "dashboard:read",
        "integration:read",
        "collector:read",
        "discovery:read",
        "discovery:reconcile",
        "telemetry:read",
        "alarm:read",
        "catalog:read",
    ],
    "Operator": [
        "organization:read",
        "location:read",
        "managed_asset:read",
        "managed_asset:update_lifecycle",
        "rack:read",
        "rack:place",
        "equipment:read",
        "equipment:place",
        "floor_plan:read",
        "spatial:read",
        "power:read",
        "capacity:read",
        "dashboard:read",
        "integration:read",
        "collector:read",
        "discovery:read",
        "telemetry:read",
        "alarm:read",
        "catalog:read",
    ],
    "Viewer": [
        "organization:read",
        "location:read",
        "managed_asset:read",
        "rack:read",
        "equipment:read",
        "floor_plan:read",
        "spatial:read",
        "power:read",
        "capacity:read",
        "dashboard:read",
        "integration:read",
        "collector:read",
        "discovery:read",
        "telemetry:read",
        "alarm:read",
        "catalog:read",
    ],
}
"""Every role that can write a resource also explicitly holds read on it — permission
codes are checked for an exact match (§39 of v1.0), not a hierarchy, so 'manage' never
implicitly grants 'read'; this list is the single source of truth both the seed
migration and this module's own require_permission() dependency are checked against."""


@dataclass(frozen=True)
class AuthContext:
    user: User
    permission_codes: frozenset[str]
    role_names: frozenset[str]
    """Every Role.name the user is assigned to, independent of that role's current
    permission grants — deliberately not derived by joining through RolePermission,
    since a role stripped of every permission would then vanish from this set even
    though the user is still formally assigned to it (spec §9.1)."""
    scope: AccessScope = AccessScope(unrestricted=False)
    """Site/rack data scope (see app/application/access_control.py). Unrestricted for
    every user holding a global role, so pre-existing users are unaffected. The default is
    the empty restricted scope so a context built without a scope fails closed."""
    inactive_permissions: frozenset[str] = frozenset()

    def has_permission(self, code: str) -> bool:
        return code in self.permission_codes

    def has_role(self, name: str) -> bool:
        return name in self.role_names


async def get_auth_context(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> AuthContext:
    """Effective permissions = (role grants + group allows) - group denies, evaluated on
    every request from the database so membership/permission changes apply immediately."""
    access = (await load_effective_access(db, [user.id]))[user.id]
    return AuthContext(
        user=user,
        permission_codes=access.permission_codes,
        role_names=access.role_names,
        scope=access.scope,
        inactive_permissions=access.inactive_permissions,
    )


def require_permission(code: str):
    async def _dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_permission(code):
            raise ForbiddenError(f"Missing required permission: {code}")
        return ctx

    return _dependency


def require_catalog_administrator(code: str):
    """Every catalog-mutation route requires the given catalog:* permission code AND
    Administrator role membership, combined into one dependency (rather than two separate
    Depends per route) so a route cannot accidentally add the permission check while
    forgetting the role check, or vice versa — the two are inseparable for this feature by
    product principle 8, unlike every other require_permission()-gated route in this
    codebase, which remains permission-only. Scoped to the four tightened legacy
    `app/api/v1/catalog.py` endpoints (PR-2) and PR-3's new `app/api/v1/catalog_designer.py`
    router only — never a general RBAC mechanism, never used elsewhere.

    Matching Role.name == "Administrator" by string is deliberate, not an oversight: there
    is no other stable marker for "the Administrator role" in this schema (`Role.is_system`
    is True for all five seeded roles, not unique to Administrator; there is no
    `is_superuser`-style flag). `Role.name` carries a UNIQUE constraint, so at most one role
    can ever hold that exact name — collision is not a risk. No role-rename capability
    exists today (no `PATCH /roles/{id}` endpoint), so relying on the literal name is a
    documented, accepted constraint of the current design (spec §9.1), not a live gap."""

    async def _dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_permission(code):
            raise ForbiddenError(f"Missing required permission: {code}")
        if not ctx.has_role("Administrator"):
            raise ForbiddenError(f"'{code}' additionally requires Administrator role membership.")
        return ctx

    return _dependency
