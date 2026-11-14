"""Add singleton lifecycle_settings table for administrator lifecycle configuration.

Revision ID: 0061_lifecycle_settings
Revises: 0060_alarm_severity
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.domain.identity.models import DEFAULT_LIFECYCLE_STATES

revision = "0061_lifecycle_settings"
down_revision = "0060_alarm_severity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("states_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_lifecycle_settings_single_row"),
        sa.PrimaryKeyConstraint("id", name="pk_lifecycle_settings"),
    )
    op.execute(
        sa.text(
            "INSERT INTO lifecycle_settings (id, states_config) "
            "VALUES (1, CAST(:states_config AS jsonb)) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(states_config=json.dumps(DEFAULT_LIFECYCLE_STATES))
    )


def downgrade() -> None:
    op.drop_table("lifecycle_settings")
