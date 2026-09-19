"""Additive MVP alarms with one open lifecycle per rule and subject.

Revision ID: 0010_mvp_alarms
Revises: 0009_mvp_telemetry
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_mvp_alarms"
down_revision = "0009_mvp_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("alarm_rule",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False), sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("rule_type", sa.String(32), nullable=False), sa.Column("threshold", sa.Numeric(18, 8)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "rule_type IN ('threshold_high', 'threshold_low', 'availability_unavailable')", name="rule_type_allowed"
        ),
        sa.CheckConstraint(
            "(rule_type = 'availability_unavailable' AND threshold IS NULL) OR "
            "(rule_type != 'availability_unavailable' AND threshold IS NOT NULL)", name="threshold_matches_rule_type"
        ),
        sa.ForeignKeyConstraint(["integration_id"], ["integration.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_alarm_rule_integration_metric", "alarm_rule", ["integration_id", "metric"])
    op.create_table("alarm",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False), sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("telemetry_reading_id", sa.Uuid()), sa.Column("managed_asset_id", sa.Uuid()),
        sa.Column("subject_key", sa.String(512), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("acknowledged_by", sa.Uuid()), sa.Column("cleared_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_value", sa.Numeric(18, 8), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("status IN ('ACTIVE', 'ACKNOWLEDGED', 'CLEARED')", name="status_allowed"),
        sa.ForeignKeyConstraint(["rule_id"], ["alarm_rule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["integration_id"], ["integration.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["telemetry_reading_id"], ["telemetry_reading.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_asset_id"], ["managed_asset.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["acknowledged_by"], ["app_user.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_alarm_integration_status_opened", "alarm", ["integration_id", "status", "opened_at"])
    op.create_index("ix_alarm_managed_asset_id", "alarm", ["managed_asset_id"])
    op.create_index("uq_alarm_open_rule_subject", "alarm", ["rule_id", "subject_key"], unique=True,
                    postgresql_where=sa.text("status IN ('ACTIVE', 'ACKNOWLEDGED')"))
    _seed_permissions()


def _seed_permissions() -> None:
    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS
    bind = op.get_bind()
    permissions = {(r.resource, r.action): r.id for r in bind.execute(sa.text("SELECT id, resource, action FROM permission"))}
    roles = {r.name: r.id for r in bind.execute(sa.text("SELECT id, name FROM role"))}
    existing = {(r.role_id, r.permission_id) for r in bind.execute(sa.text("SELECT role_id, permission_id FROM role_permission"))}
    for codes in DEFAULT_ROLE_PERMISSIONS.values():
        for code in codes:
            resource, action = code.split(":")
            if (resource, action) not in permissions:
                pid = _uuid.uuid4()
                bind.execute(
                    sa.text("INSERT INTO permission (id, resource, action, description) VALUES (:id, :r, :a, :d)"),
                    {"id": pid, "r": resource, "a": action, "d": f"{action} on {resource}"},
                )
                permissions[(resource, action)] = pid
    for role_name, codes in DEFAULT_ROLE_PERMISSIONS.items():
        if role_name in roles:
            for code in codes:
                resource, action = code.split(":")
                key = (roles[role_name], permissions[(resource, action)])
                if key not in existing:
                    bind.execute(
                        sa.text("INSERT INTO role_permission (role_id, permission_id) VALUES (:r, :p)"),
                        {"r": key[0], "p": key[1]},
                    )


def downgrade() -> None:
    op.drop_index("uq_alarm_open_rule_subject", table_name="alarm")
    op.drop_index("ix_alarm_managed_asset_id", table_name="alarm")
    op.drop_index("ix_alarm_integration_status_opened", table_name="alarm")
    op.drop_table("alarm")
    op.drop_index("ix_alarm_rule_integration_metric", table_name="alarm_rule")
    op.drop_table("alarm_rule")
