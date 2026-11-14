"""Explicit alarm severity, independent of workflow state.

Revision ID: 0060_alarm_severity
Revises: 0059_idempotency_principal

``alarm_rule.severity`` is the operator-authored severity of a rule (critical / warning / info).
``alarm.severity`` snapshots the rule's severity when the alarm opens, so editing a rule later does
not rewrite the severity an operator already saw on an existing alarm.

Existing rows: no severity was ever recorded, so every existing rule and alarm is stamped
``warning`` (the documented default). That value is a placeholder, not a judgement about the rule;
operators set the real severity on each rule. Adding a column with a constant default is a
metadata-only change on PostgreSQL 11+ (no table rewrite). The CHECK constraints are added NOT VALID and then
validated, but both statements run in Alembic's single transaction, so the strong lock taken by the ADD is held until
the migration commits; the ordering does not shorten it. No claim is made about locking or readiness on a large
production table: run it only under the owner's change control, with a backup, as for any pending migration.
"""
import sqlalchemy as sa
from alembic import op

revision = "0060_alarm_severity"
down_revision = "0059_idempotency_principal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alarm_rule", sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"))
    op.add_column("alarm", sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"))
    # NOT VALID then VALIDATE: the ADD's lock is still held until this transaction commits (see the module docstring).
    op.execute(
        "ALTER TABLE alarm_rule ADD CONSTRAINT ck_alarm_rule_severity_allowed "
        "CHECK (severity IN ('critical', 'warning', 'info')) NOT VALID"
    )
    op.execute("ALTER TABLE alarm_rule VALIDATE CONSTRAINT ck_alarm_rule_severity_allowed")
    op.execute(
        "ALTER TABLE alarm ADD CONSTRAINT ck_alarm_severity_allowed "
        "CHECK (severity IN ('critical', 'warning', 'info')) NOT VALID"
    )
    op.execute("ALTER TABLE alarm VALIDATE CONSTRAINT ck_alarm_severity_allowed")


def downgrade() -> None:
    op.execute("ALTER TABLE alarm DROP CONSTRAINT ck_alarm_severity_allowed")
    op.drop_column("alarm", "severity")
    op.execute("ALTER TABLE alarm_rule DROP CONSTRAINT ck_alarm_rule_severity_allowed")
    op.drop_column("alarm_rule", "severity")
