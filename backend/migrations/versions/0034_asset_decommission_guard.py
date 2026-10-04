"""Guard decommission timestamps without inventing historical dates.

Revision ID: 0034_asset_decommission_guard
Revises: 0033_catalog_extraction
"""
from alembic import op

revision = "0034_asset_decommission_guard"
down_revision = "0033_catalog_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Lock before auditing so another writer cannot invalidate the preflight.
    # Transactional DDL releases this lock on commit or rollback.
    op.execute("LOCK TABLE managed_asset IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM managed_asset
                WHERE decommissioned_at IS NOT NULL
                  AND lifecycle_status NOT IN ('decommissioned', 'removed')
            ) THEN
                RAISE EXCEPTION 'managed_asset has non-terminal assets with decommissioned_at; '
                    'audit and correct these rows explicitly before retrying migration 0034_asset_decommission_guard';
            END IF;
        END $$;
    """)
    op.execute("""
        ALTER TABLE managed_asset
        ADD CONSTRAINT ck_managed_asset_decommissioned_at_terminal
        CHECK (decommissioned_at IS NULL OR lifecycle_status IN ('decommissioned', 'removed'))
        NOT VALID
    """)
    op.execute("""
        ALTER TABLE managed_asset
        VALIDATE CONSTRAINT ck_managed_asset_decommissioned_at_terminal
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE managed_asset DROP CONSTRAINT ck_managed_asset_decommissioned_at_terminal")
