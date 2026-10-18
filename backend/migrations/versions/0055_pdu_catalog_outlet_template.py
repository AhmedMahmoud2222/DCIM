"""PDU catalog outlet template and electrical attributes (Issue #150).

Revision ID: 0055_pdu_catalog_outlet_template
Revises: 0054_role_assignment_scope_types
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0055_pdu_catalog_outlet_template"
down_revision = "0054_role_assignment_scope_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalog_model_revision", sa.Column("pdu_input_voltage_v", sa.Numeric(6, 1), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_input_current_a", sa.Numeric(6, 2), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_phase", sa.String(8), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_rated_capacity_kw", sa.Numeric(10, 3), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_plug_type", sa.String(32), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_monitoring_protocol", sa.String(16), nullable=True))
    op.add_column("catalog_model_revision", sa.Column("pdu_monitoring_endpoint", sa.String(255), nullable=True))

    op.create_table(
        "pdu_outlet_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("catalog_model_revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stable_key", sa.String(64), nullable=False),
        sa.Column("outlet_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("outlet_type", sa.String(16), nullable=False, server_default="other"),
        sa.Column("outlet_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(8), nullable=False, server_default="single"),
        sa.Column("rated_voltage_v", sa.Numeric(6, 1), nullable=True),
        sa.Column("rated_current_a", sa.Numeric(6, 2), nullable=True),
        sa.Column("position", sa.String(32), nullable=True),
        sa.Column("side", sa.String(8), nullable=False, server_default="front"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("catalog_model_revision_id", "stable_key", name="uq_pdu_outlet_template_cmr_sk"),
        sa.UniqueConstraint("catalog_model_revision_id", "outlet_number", name="uq_pdu_outlet_template_cmr_on"),
    )


def downgrade() -> None:
    op.drop_table("pdu_outlet_template")
    for c in ["pdu_monitoring_endpoint","pdu_monitoring_protocol","pdu_plug_type","pdu_rated_capacity_kw","pdu_phase","pdu_input_current_a","pdu_input_voltage_v"]:
        op.drop_column("catalog_model_revision", c)
