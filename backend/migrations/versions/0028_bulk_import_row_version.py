"""SEC (Codex PR #50 review, finding #5): adds `bulk_import_row.expected_version`, a
nullable snapshot of the target entity's own optimistic-concurrency `version`
(Rack.version / Equipment.version / CatalogModelRevision.version) captured at validate
time for update-mode rows, so commit can detect "this record changed since it was
previewed" instead of always comparing a freshly re-fetched version against itself. See
app/domain/bulk_import/models.py's own column docstring for the full reasoning.

Revision ID: 0028_bulk_import_row_version
Revises: 0027_bulk_import_rbac_seed
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_bulk_import_row_version"
down_revision = "0027_bulk_import_rbac_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bulk_import_row", sa.Column("expected_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("bulk_import_row", "expected_version")
