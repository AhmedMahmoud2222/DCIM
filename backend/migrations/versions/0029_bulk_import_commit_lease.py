"""SEC (Codex PR #50 review, ROUND 2, finding #1): adds `bulk_import_job.commit_lease_id`
and `bulk_import_job.commit_lease_expires_at` — a lease/ownership token claimed atomically
by `run_commit` (app/application/bulk_import/service.py), renewed at every batch
checkpoint, and verified before every durable write. See that module's docstring for the
full reasoning: `status == 'committing'` alone is not one-shot-consumed (it stays
'committing' for the whole run), so a plain status check let two overlapping Celery
deliveries of the same commit both observe 'committing' and both process the same rows.

Revision ID: 0029_bulk_import_commit_lease
Revises: 0028_bulk_import_row_version
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_bulk_import_commit_lease"
down_revision = "0028_bulk_import_row_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bulk_import_job", sa.Column("commit_lease_id", sa.Uuid(), nullable=True))
    op.add_column(
        "bulk_import_job", sa.Column("commit_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("bulk_import_job", "commit_lease_expires_at")
    op.drop_column("bulk_import_job", "commit_lease_id")
