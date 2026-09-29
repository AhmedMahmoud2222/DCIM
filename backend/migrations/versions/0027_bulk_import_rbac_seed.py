"""Bulk-import permission seed: `rack:import` and `equipment:import`, granted to every
role that currently holds the matching `*:manage` permission (Administrator, DCIM
Manager, Engineer — verified against app/application/rbac.py's DEFAULT_ROLE_PERMISSIONS,
which this migration's grants must stay in lockstep with). `catalog:import` already exists
(migration 0021_catalog_rbac_seed, Administrator-only via require_catalog_administrator)
and is untouched here. Modeled on 0021's own `sa.table`/`op.bulk_insert` pattern.

Revision ID: 0027_bulk_import_rbac_seed
Revises: 0026_bulk_import
Create Date: 2026-09-28
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0027_bulk_import_rbac_seed"
down_revision = "0026_bulk_import"
branch_labels = None
depends_on = None

# resource -> (action, [role names granted]).
_GRANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "rack": ("import", ("Administrator", "DCIM Manager", "Engineer")),
    "equipment": ("import", ("Administrator", "DCIM Manager", "Engineer")),
}


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

    permission_ids = {resource: _uuid.uuid4() for resource in _GRANTS}
    op.bulk_insert(
        permission_table,
        [
            {
                "id": permission_ids[resource], "resource": resource, "action": action,
                "description": f"{action} on {resource}",
            }
            for resource, (action, _roles) in _GRANTS.items()
        ],
    )

    connection = op.get_bind()
    all_role_names = sorted({name for _action, names in _GRANTS.values() for name in names})
    role_ids = {
        name: role_id
        for name, role_id in connection.execute(
            sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(all_role_names))
        )
    }
    missing = set(all_role_names) - set(role_ids)
    if missing:
        raise RuntimeError(
            f"bulk-import RBAC seed expects roles {sorted(all_role_names)} to already exist "
            f"(from migration 0002); missing: {sorted(missing)}"
        )

    role_permission_rows = [
        {"role_id": role_ids[role_name], "permission_id": permission_ids[resource]}
        for resource, (_action, role_names) in _GRANTS.items()
        for role_name in role_names
    ]
    op.bulk_insert(role_permission_table, role_permission_rows)


def downgrade() -> None:
    resources = tuple(_GRANTS.keys())
    op.execute(
        f"""
        DELETE FROM role_permission
        WHERE permission_id IN (
            SELECT id FROM permission WHERE resource IN {resources!r} AND action = 'import'
        )
        """
    )
    op.execute(f"DELETE FROM permission WHERE resource IN {resources!r} AND action = 'import'")
