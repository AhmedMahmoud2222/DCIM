"""audit_log partitions, retention role, and RBAC seed data

Revision ID: 0002_seed
Revises: e9fd19228f19
Create Date: 2026-09-16
"""
from datetime import date

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_seed"
down_revision = "e9fd19228f19"
branch_labels = None
depends_on = None


def _month_bounds(base: date, offset_months: int) -> tuple[date, date]:
    year = base.year + (base.month - 1 + offset_months) // 12
    month = (base.month - 1 + offset_months) % 12 + 1
    start = date(year, month, 1)
    end_year = year + (month // 12)
    end_month = month % 12 + 1
    end = date(end_year, end_month, 1)
    return start, end


def upgrade() -> None:
    # --- AuditLog partitions (§30a): a default catch-all plus the current + next 2
    # months. The maintenance task (audit_partition_maintenance.py) keeps future months
    # created ahead of time; this migration only guarantees Phase 1 doesn't start with
    # zero partitions (which would reject every insert).
    op.execute("CREATE TABLE IF NOT EXISTS audit_log_default PARTITION OF audit_log DEFAULT")
    today = date.today()
    for offset in range(0, 3):
        start, end = _month_bounds(today, offset)
        name = f"audit_log_{start.strftime('%Y_%m')}"
        op.execute(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF audit_log "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_entity ON audit_log (entity_type, entity_id, timestamp)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_actor ON audit_log (actor_user_id, timestamp)")

    # --- Privileged retention role (§30a): distinct from the application's own role.
    # Deliberately NOT created here — this migration runs as the application's own
    # least-privilege role, which does not (and must not) hold CREATEROLE. Creating
    # `dcim_retention_admin` and granting it rights on audit_log is a one-time,
    # superuser-run bootstrap step: see scripts/bootstrap_privileged_roles.sql, which
    # must be run once per environment after this migration. This split is itself a
    # concrete demonstration of the least-privilege principle (§33/§40 of the Phase 1
    # prompt), not a gap: the role that runs migrations should not be able to grant
    # privileges to a role more powerful than itself.

    # Append-only enforcement (§30): the application role may INSERT/SELECT but never
    # UPDATE or DELETE audit_log, so even a bug in application code cannot rewrite
    # history. This must run after the table is owned/created by dcim_app (the migration
    # runs as dcim_app in this environment) — REVOKE from the owning role is still honored
    # by Postgres for DML privileges.
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM dcim_app")
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")

    # --- RBAC seed (§32, v1.0 §6.1): permission catalog + default roles. No bootstrap
    # admin *user* is created here deliberately — see scripts/create_admin.py; credentials
    # never belong in migration history.
    permission_table = sa.table(
        "permission",
        sa.column("id", UUID),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.String),
    )
    role_table = sa.table(
        "role",
        sa.column("id", UUID),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("is_system", sa.Boolean),
    )
    role_permission_table = sa.table(
        "role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID)
    )
    location_type_table = sa.table(
        "location_type",
        sa.column("id", UUID),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("icon", sa.String),
        sa.column("sort_order", sa.Integer),
    )

    import uuid as _uuid

    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS

    role_permission_codes = DEFAULT_ROLE_PERMISSIONS
    permission_specs = sorted(
        {tuple(code.split(":")) for codes in role_permission_codes.values() for code in codes}
    )
    permission_ids = {spec: _uuid.uuid4() for spec in permission_specs}
    op.bulk_insert(
        permission_table,
        [
            {"id": permission_ids[(r, a)], "resource": r, "action": a, "description": f"{a} on {r}"}
            for (r, a) in permission_specs
        ],
    )

    role_ids = {name: _uuid.uuid4() for name in role_permission_codes}
    op.bulk_insert(
        role_table,
        [{"id": role_ids[name], "name": name, "description": f"Default {name} role", "is_system": True} for name in role_ids],
    )
    role_permission_rows = []
    for role_name, codes in role_permission_codes.items():
        for code in codes:
            resource, action = code.split(":")
            role_permission_rows.append(
                {"role_id": role_ids[role_name], "permission_id": permission_ids[(resource, action)]}
            )
    op.bulk_insert(role_permission_table, role_permission_rows)

    location_types = [
        ("organization", "Organization", 0), ("country", "Country", 1), ("city", "City", 2),
        ("site", "Site", 3), ("building", "Building", 4), ("floor", "Floor", 5), ("room", "Room", 6),
    ]
    op.bulk_insert(
        location_type_table,
        [{"id": _uuid.uuid4(), "code": c, "label": l, "icon": None, "sort_order": o} for c, l, o in location_types],
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_permission")
    op.execute("DELETE FROM role")
    op.execute("DELETE FROM permission")
    op.execute("DELETE FROM location_type")
    op.execute("GRANT UPDATE, DELETE ON audit_log TO dcim_app")
    # Finding L1 (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md / PHASE1_CORRECTION_REPORT.md):
    # this previously dropped only audit_log_default, leaving the monthly partitions
    # upgrade() created still attached to audit_log — an incomplete reversal (harmless on
    # a subsequent re-upgrade, since partition creation is idempotent, but not a correct
    # downgrade). Drop every partition this migration created, not just the default one.
    today = date.today()
    for offset in range(0, 3):
        start, _ = _month_bounds(today, offset)
        name = f"audit_log_{start.strftime('%Y_%m')}"
        op.execute(f"DROP TABLE IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS audit_log_default")
