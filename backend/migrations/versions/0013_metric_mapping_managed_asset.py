"""Associate thin metric mappings with authoritative managed assets.

Revision ID: 0013_metric_mapping_managed_asset
Revises: 0012_retention_series_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_metric_mapping_managed_asset"
down_revision = "0012_retention_series_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_metric_mapping", sa.Column("managed_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_metric_mapping_managed_asset", "integration_metric_mapping", "managed_asset",
        ["managed_asset_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_integration_metric_mapping_managed_asset_id", "integration_metric_mapping", ["managed_asset_id"])


def downgrade() -> None:
    op.drop_index("ix_integration_metric_mapping_managed_asset_id", table_name="integration_metric_mapping")
    op.drop_constraint("fk_metric_mapping_managed_asset", "integration_metric_mapping", type_="foreignkey")
    op.drop_column("integration_metric_mapping", "managed_asset_id")
