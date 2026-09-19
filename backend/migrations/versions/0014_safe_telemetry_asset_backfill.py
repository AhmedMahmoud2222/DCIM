"""Conservative historical telemetry ManagedAsset backfill.

Only stable mapping identity is used.  Ambiguous daily aggregates and target-key
collisions deliberately remain unmanaged; no display-name or current device guess is
ever used.
"""

from alembic import op

revision = "0014_telemetry_asset_backfill"
down_revision = "0013_metric_asset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Raw readings retain their immutable mapping_id.  Require their existing key to
    # prove the pre-association unmanaged identity before deriving the asset key.
    op.execute("""
        UPDATE telemetry_reading r SET
          managed_asset_id = m.managed_asset_id,
          series_key = r.integration_id::text || ':' || m.managed_asset_id::text || ':' ||
                       r.external_identifier || ':' || r.metric || ':' || r.unit
        FROM integration_metric_mapping m
        WHERE r.managed_asset_id IS NULL AND r.mapping_id = m.id
          AND m.managed_asset_id IS NOT NULL
          AND r.series_key = r.integration_id::text || ':unmanaged:' ||
                             r.external_identifier || ':' || r.metric || ':' || r.unit
    """)
    # Daily rows lack mapping_id.  Backfill only when exactly one assigned mapping
    # has the exact durable integration/canonical metric/unit identity, and never
    # merge a target aggregate that already exists for that day.
    op.execute("""
        WITH candidates AS (
          -- PostgreSQL does not define min(uuid); count(*) = 1 below makes this
          -- text cast a value selection, not an ordering decision.
          SELECT a.id, min(m.managed_asset_id::text)::uuid AS asset_id
          FROM daily_telemetry_aggregate a
          JOIN integration_metric_mapping m ON m.integration_id = a.integration_id
             AND m.canonical_metric = a.metric AND m.unit = a.unit
             AND m.managed_asset_id IS NOT NULL
          WHERE a.managed_asset_id IS NULL
            AND a.series_key = a.integration_id::text || ':unmanaged:' ||
                               a.external_identifier || ':' || a.metric || ':' || a.unit
          GROUP BY a.id HAVING count(*) = 1
        )
        UPDATE daily_telemetry_aggregate a SET
          managed_asset_id = c.asset_id,
          series_key = a.integration_id::text || ':' || c.asset_id::text || ':' ||
                       a.external_identifier || ':' || a.metric || ':' || a.unit
        FROM candidates c
        WHERE a.id = c.id AND NOT EXISTS (
          SELECT 1 FROM daily_telemetry_aggregate target
          WHERE target.day = a.day AND target.series_key =
            a.integration_id::text || ':' || c.asset_id::text || ':' ||
            a.external_identifier || ':' || a.metric || ':' || a.unit
        )
    """)


def downgrade() -> None:
    # Asset association is additive historical enrichment; do not destructively
    # erase it on downgrade.
    pass
