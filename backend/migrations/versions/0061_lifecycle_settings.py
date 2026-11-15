"""Add singleton lifecycle_settings table for administrator lifecycle configuration.

Revision ID: 0061_lifecycle_settings
Revises: 0060_alarm_severity
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061_lifecycle_settings"
down_revision = "0060_alarm_severity"
branch_labels = None
depends_on = None

# Self-contained immutable seed data for migration 0061 (decoupled from runtime application models).
_SEED_LIFECYCLE_STATES = [
    {
        "status": "planned",
        "label": "Planned",
        "description": "Equipment is planned or procured and awaiting physical installation.",
        "badge_color": "neutral",
        "display_order": 1,
        "allowed_transitions": ["installed", "removed"],
    },
    {
        "status": "installed",
        "label": "Installed",
        "description": "Equipment is physically mounted or placed and ready for commissioning.",
        "badge_color": "info",
        "display_order": 2,
        "allowed_transitions": ["active", "maintenance", "removed"],
    },
    {
        "status": "active",
        "label": "Active",
        "description": "Equipment is commissioned and in production service.",
        "badge_color": "ok",
        "display_order": 3,
        "allowed_transitions": ["maintenance", "decommissioned"],
    },
    {
        "status": "maintenance",
        "label": "Maintenance",
        "description": "Equipment is undergoing scheduled or corrective maintenance.",
        "badge_color": "caution",
        "display_order": 4,
        "allowed_transitions": ["active", "decommissioned"],
    },
    {
        "status": "decommissioned",
        "label": "Decommissioned",
        "description": "Equipment is permanently retired from service and awaiting physical removal.",
        "badge_color": "serious",
        "display_order": 5,
        "allowed_transitions": ["removed"],
    },
    {
        "status": "removed",
        "label": "Removed",
        "description": "Equipment has been physically removed from the facility.",
        "badge_color": "danger",
        "display_order": 6,
        "allowed_transitions": [],
    },
]


def upgrade() -> None:
    op.create_table(
        "lifecycle_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("states_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
            "INSERT INTO lifecycle_settings (id, states_config, version) "
            "VALUES (1, CAST(:states_config AS jsonb), 1) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(states_config=json.dumps(_SEED_LIFECYCLE_STATES))
    )


def downgrade() -> None:
    op.drop_table("lifecycle_settings")
