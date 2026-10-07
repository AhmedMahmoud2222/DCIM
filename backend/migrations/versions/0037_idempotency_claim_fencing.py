"""Fence idempotency claims with a monotonically increasing generation (NEW-1).

Revision ID: 0037_idempotency_claim_fencing
Revises: 0036_catalog_extraction_apply
"""
import sqlalchemy as sa
from alembic import op

revision = "0037_idempotency_claim_fencing"
down_revision = "0036_catalog_extraction_apply"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows (processing or completed) start at generation 1; any in-flight claim
    # keeps its single owner and stays completable by it.
    op.add_column(
        "idempotency_key", sa.Column("claim_generation", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )
    op.create_check_constraint(
        "ck_idempotency_key_claim_generation_positive", "idempotency_key", "claim_generation >= 1"
    )


def downgrade() -> None:
    op.drop_constraint("ck_idempotency_key_claim_generation_positive", "idempotency_key", type_="check")
    op.drop_column("idempotency_key", "claim_generation")
