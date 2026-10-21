"""PDU vertical 0U placement uniqueness (Issue #150 R5 follow-up).

A vertical (0U) PDU occupies the full rack height on one side, so at most one may be
placed per rack + side at a time. Vertical PDU placements are represented as
`placement_type='other'` with the `orientation` column set to the discriminator
`'pdu_vertical_0u'`; this partial unique index enforces one current vertical placement
per rack + side, scoped so it never affects equipment or cooling placements.

Revision ID: 0056_pdu_vertical_placement
Revises: 0055_pdu_catalog_outlet_template
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0056_pdu_vertical_placement"
down_revision = "0055_pdu_catalog_outlet_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_equipment_placement_pdu_vertical_0u",
        "equipment_placement",
        ["rack_id", "side"],
        unique=True,
        postgresql_where=sa.text(
            "placement_type = 'other' AND orientation = 'pdu_vertical_0u' AND effective_to IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_equipment_placement_pdu_vertical_0u", table_name="equipment_placement")
