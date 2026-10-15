"""SEC-ARCH-01 (#57): RoleAssignment scope types become ('global', 'site', 'organization').

Revision ID: 0047_role_assignment_scope_types
Revises: 0046_mapping_revisions

`building` is removed. It was never enforceable (site grants do not name buildings), so since
SEC-ARCH-01 a building-scoped assignment grants nothing. Existing building rows are NOT converted:
mapping them to `site`, `organization` or `global` would silently widen access relative to their
current fail-closed state. Upgrade instead refuses while any exist, so an operator reviews and
re-creates or deletes them by hand first. `organization` is new: no existing row is modified.

Downgrade refuses while organization-scoped assignments exist, then restores the previous constraint.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_role_assignment_scope_types"
down_revision = "0046_mapping_revisions"
branch_labels = None
depends_on = None

_OLD_TYPES = "('global', 'site', 'building')"
_NEW_TYPES = "('global', 'site', 'organization')"


def _count_scope_type(scope_type: str) -> int:
    return op.get_bind().execute(
        sa.text("SELECT count(*) FROM role_assignment WHERE scope_type = :t"), {"t": scope_type}
    ).scalar_one()


def _swap_constraint(allowed: str) -> None:
    op.drop_constraint(op.f("ck_role_assignment_scope_type_allowed"), "role_assignment", type_="check")
    op.create_check_constraint(op.f("ck_role_assignment_scope_type_allowed"), "role_assignment", f"scope_type IN {allowed}")


def upgrade() -> None:
    # Blocks concurrent writes between the check and the constraint swap.
    op.execute("LOCK TABLE role_assignment IN SHARE ROW EXCLUSIVE MODE")
    building_rows = _count_scope_type("building")
    if building_rows:
        raise RuntimeError(
            f"Cannot upgrade to 0047: {building_rows} role_assignment row(s) have scope_type='building', which is "
            "no longer supported. They currently grant nothing (fail closed, SEC-ARCH-01) and are deliberately not "
            "converted, because any other scope could widen access. Review them (SELECT * FROM role_assignment "
            "WHERE scope_type = 'building'), then delete them or re-create them with an explicit site, organization "
            "or global scope, and run the upgrade again."
        )
    _swap_constraint(_NEW_TYPES)


def downgrade() -> None:
    op.execute("LOCK TABLE role_assignment IN SHARE ROW EXCLUSIVE MODE")
    organization_rows = _count_scope_type("organization")
    if organization_rows:
        raise RuntimeError(
            f"Cannot downgrade 0047: {organization_rows} role_assignment row(s) have scope_type='organization', "
            "which the previous schema does not allow. Remove or re-scope them explicitly first."
        )
    _swap_constraint(_OLD_TYPES)
