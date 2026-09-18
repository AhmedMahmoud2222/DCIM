"""RBAC enforcement (§39 of v1.0, §32/§32a). H7 = Option B: Phase 1 enforces permissions
globally only — RoleAssignment.scope_type/scope_id are populated but never filtered on.
Every protected route depends on require_permission(), never on frontend hiding (§13 of
the Phase 1 prompt)."""

from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ForbiddenError
from app.domain.auth.models import Permission, Role, RoleAssignment, RolePermission, User

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
        "role:manage",
        "audit:view",
        "rack:read",
        "rack:manage",
        "rack:place",
        "equipment:read",
        "equipment:manage",
        "equipment:place",
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
        "equipment:read",
        "equipment:manage",
        "equipment:place",
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
        "equipment:read",
        "equipment:manage",
        "equipment:place",
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

    def has_permission(self, code: str) -> bool:
        return code in self.permission_codes


async def get_auth_context(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> AuthContext:
    stmt = (
        select(Permission.resource, Permission.action)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .where(RoleAssignment.user_id == user.id)
    )
    rows = (await db.execute(stmt)).all()
    codes = frozenset(f"{resource}:{action}" for resource, action in rows)
    return AuthContext(user=user, permission_codes=codes)


def require_permission(code: str):
    async def _dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_permission(code):
            raise ForbiddenError(f"Missing required permission: {code}")
        return ctx

    return _dependency
