"""User & group management: user_group, membership, group permissions (allow/deny),
site access and rack access, plus the `user:read`, `group:read` and `group:manage`
permissions (Administrator only).

Purely additive. No existing role, role_assignment, user or permission row is modified, so
every current user keeps exactly the access it has today. A user with no group membership
and no role assignment has no access (deny by default).

Revision ID: 0031_user_groups
Revises: 0030_bulk_import_attempts
Create Date: 2026-09-29
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0031_user_groups"
down_revision = "0030_bulk_import_attempts"
branch_labels = None
depends_on = None

_NEW_PERMISSIONS = (("user", "read"), ("group", "read"), ("group", "manage"))


def upgrade() -> None:
    op.create_table(
        "user_group",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_user_group_name"),
    )
    op.execute("CREATE UNIQUE INDEX uq_user_group_name_lower ON user_group (lower(name))")

    op.create_table(
        "user_group_member",
        sa.Column("group_id", UUID, sa.ForeignKey("user_group.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_user_group_member_user_id", "user_group_member", ["user_id"])

    op.create_table(
        "user_group_permission",
        sa.Column("group_id", UUID, sa.ForeignKey("user_group.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", UUID, sa.ForeignKey("permission.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("effect", sa.String(8), nullable=False, server_default="allow"),
        sa.CheckConstraint("effect IN ('allow', 'deny')", name="ck_user_group_permission_effect_allowed"),
    )

    op.create_table(
        "user_group_site_access",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("group_id", UUID, sa.ForeignKey("user_group.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", UUID, sa.ForeignKey("site.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rack_scope", sa.String(8), nullable=False, server_default="selected"),
        sa.CheckConstraint("rack_scope IN ('all', 'selected')", name="ck_user_group_site_access_rack_scope_allowed"),
        sa.UniqueConstraint("group_id", "site_id", name="uq_user_group_site_access"),
    )
    op.create_index("ix_user_group_site_access_group_id", "user_group_site_access", ["group_id"])
    op.create_index("ix_user_group_site_access_site_id", "user_group_site_access", ["site_id"])

    op.create_table(
        "user_group_rack_access",
        sa.Column(
            "site_access_id", UUID, sa.ForeignKey("user_group_site_access.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("rack_id", UUID, sa.ForeignKey("managed_asset.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_user_group_rack_access_rack_id", "user_group_rack_access", ["rack_id"])

    permission_table = sa.table(
        "permission",
        sa.column("id", UUID),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.String),
    )
    role_permission_table = sa.table("role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID))
    role_table = sa.table("role", sa.column("id", UUID), sa.column("name", sa.String))

    ids = {(r, a): _uuid.uuid4() for r, a in _NEW_PERMISSIONS}
    op.bulk_insert(
        permission_table,
        [{"id": pid, "resource": r, "action": a, "description": f"{a} on {r}"} for (r, a), pid in ids.items()],
    )
    admin_id = op.get_bind().execute(sa.select(role_table.c.id).where(role_table.c.name == "Administrator")).scalar_one()
    op.bulk_insert(role_permission_table, [{"role_id": admin_id, "permission_id": pid} for pid in ids.values()])


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permission WHERE permission_id IN (
            SELECT id FROM permission
            WHERE (resource, action) IN (('user', 'read'), ('group', 'read'), ('group', 'manage'))
        )
        """
    )
    op.execute("DELETE FROM permission WHERE (resource, action) IN (('user', 'read'), ('group', 'read'), ('group', 'manage'))")
    op.drop_table("user_group_rack_access")
    op.drop_table("user_group_site_access")
    op.drop_table("user_group_permission")
    op.drop_table("user_group_member")
    op.execute("DROP INDEX IF EXISTS uq_user_group_name_lower")
    op.drop_table("user_group")
