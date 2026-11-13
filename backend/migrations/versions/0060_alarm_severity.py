"""Explicit alarm severity, independent of workflow state.

Revision ID: 0060_alarm_severity
Revises: 0059_idempotency_principal

``alarm_rule.severity`` is the operator-authored severity of a rule (critical / warning / info).
``alarm.severity`` snapshots the rule's severity when the alarm opens, so editing a rule later does
not rewrite the severity an operator already saw on an existing alarm.

Existing rows: no severity was ever recorded, so every existing rule and alarm is stamped
``warning`` (the documented default). That value is a placeholder, not a judgement about the rule;
operators set the real severity on each rule. Adding a column with a constant default is a
metadata-only change on PostgreSQL 11+, and the CHECK constraints are added NOT VALID then validated
so the table is not rewritten under a long lock.
"""
import sqlalchemy as sa
from alembic import op

revision = "0060_alarm_severity"
down_revision = "0059_idempotency_principal"
branch_labels = None
depends_on = None

SEVERITIES = ("critical", "warning", "info")


def upgrade() -> None:
    for table in ("alarm_rule", "alarm"):
        op.add_column(table, sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"))
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_severity_allowed "
            f"CHECK (severity IN {SEVERITIES!r}) NOT VALID"
        )
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_{table}_severity_allowed")


def downgrade() -> None:
    for table in ("alarm", "alarm_rule"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_severity_allowed")
        op.drop_column(table, "severity")
