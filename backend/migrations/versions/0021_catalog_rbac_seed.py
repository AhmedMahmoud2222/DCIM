"""Phase 10A PR-2: catalog:* permission seed (docs/superpowers/specs/2026-09-23-phase-10a-
asset-catalog-designer-design.md §9.1, aligned plan §3.2).

Seeds seven new `permission` rows (`catalog:read`, `catalog:read_draft`, `catalog:manage`,
`catalog:publish`, `catalog:retire`, `catalog:import`, `catalog:migrate`) and their
`role_permission` grants: `catalog:read` to all five seeded roles (Administrator, DCIM
Manager, Engineer, Operator, Viewer — all five already hold `rack:read`/`equipment:read`);
every other `catalog:*` code to Administrator only. Modeled on migration 0002's own
`sa.table`/`op.bulk_insert` pattern, but looking up existing role ids by name rather than
re-creating them — the five roles already exist from migration 0002, and this migration
only adds permissions and their grants.

Revision ID: 0021_catalog_rbac_seed
Revises: 0020_catalog_legacy_bridge
Create Date: 2026-09-23
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0021_catalog_rbac_seed"
down_revision = "0020_catalog_legacy_bridge"
branch_labels = None
depends_on = None

# resource -> actions granted to every seeded role; actions granted to Administrator only.
_ALL_ROLES_ACTIONS = ("read",)
_ADMIN_ONLY_ACTIONS = ("read_draft", "manage", "publish", "retire", "import", "migrate")
_RESOURCE = "catalog"
_ALL_ROLE_NAMES = ("Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer")


def upgrade() -> None:
    permission_table = sa.table(
        "permission",
        sa.column("id", UUID),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.String),
    )
    role_permission_table = sa.table(
        "role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID)
    )
    role_table = sa.table("role", sa.column("id", UUID), sa.column("name", sa.String))

    actions = _ALL_ROLES_ACTIONS + _ADMIN_ONLY_ACTIONS
    permission_ids = {action: _uuid.uuid4() for action in actions}
    op.bulk_insert(
        permission_table,
        [
            {"id": permission_ids[action], "resource": _RESOURCE, "action": action, "description": f"{action} on {_RESOURCE}"}
            for action in actions
        ],
    )

    connection = op.get_bind()
    role_ids = {
        name: role_id
        for name, role_id in connection.execute(
            sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(_ALL_ROLE_NAMES))
        )
    }
    missing = set(_ALL_ROLE_NAMES) - set(role_ids)
    if missing:
        raise RuntimeError(
            f"catalog RBAC seed expects roles {sorted(_ALL_ROLE_NAMES)} to already exist "
            f"(from migration 0002); missing: {sorted(missing)}"
        )

    role_permission_rows = [
        {"role_id": role_ids[role_name], "permission_id": permission_ids[action]}
        for role_name in _ALL_ROLE_NAMES
        for action in _ALL_ROLES_ACTIONS
    ]
    role_permission_rows += [
        {"role_id": role_ids["Administrator"], "permission_id": permission_ids[action]} for action in _ADMIN_ONLY_ACTIONS
    ]
    op.bulk_insert(role_permission_table, role_permission_rows)


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM role_permission
        WHERE permission_id IN (SELECT id FROM permission WHERE resource = '{_RESOURCE}')
        """
    )
    op.execute(f"DELETE FROM permission WHERE resource = '{_RESOURCE}'")
