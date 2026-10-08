"""Fence idempotency claims with a monotonically increasing generation (NEW-1).

Revision ID: 0041_idempotency_claim_fencing
Revises: 0040_port_pass_through
"""
import sqlalchemy as sa
from alembic import op

revision = "0041_idempotency_claim_fencing"
down_revision = "0040_port_pass_through"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows (processing or completed) start at generation 1; any in-flight claim
    # keeps its single owner and stays completable by it.
    op.add_column(
        "idempotency_key", sa.Column("claim_generation", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )
    # Raw DDL on purpose: op.create_check_constraint() would run the name through the metadata naming
    # convention (ck_<table>_<name>) a second time. This is exactly the name the model's convention yields.
    op.execute(
        "ALTER TABLE idempotency_key ADD CONSTRAINT ck_idempotency_key_claim_generation_positive "
        "CHECK (claim_generation >= 1)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE idempotency_key DROP CONSTRAINT ck_idempotency_key_claim_generation_positive")
    op.drop_column("idempotency_key", "claim_generation")
