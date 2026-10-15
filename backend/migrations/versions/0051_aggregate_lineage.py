"""G6 evidence lifetime. Additive, no historic provenance backfill.

Follows merged G9b/0052 and G5/0053 without importing sibling G8/G9a work.
No raw deletion here.
"""

import sqlalchemy as sa
from alembic import op

revision = '0051_aggregate_lineage'
down_revision = '0052_monitoring_template_binding'
branch_labels = None
depends_on = None

COLUMNS = {
    'evidence_class': sa.String(24), 'lineage_sample_count': sa.Integer(),
    'first_compacted_at': sa.TIMESTAMP(timezone=True), 'raw_retention_days_at_compaction': sa.Integer(),
}
CHECKS = {
    'lineage_evidence_allowed': "evidence_class IS NULL OR evidence_class IN "
                                "('pinned','inferred','operator_resolved','mixed','unverified')",
    'lineage_count_scope': '(evidence_class IS NULL) = (lineage_sample_count IS NULL) AND '
                           '(lineage_sample_count IS NULL OR lineage_sample_count BETWEEN 1 AND sample_count)',
}


def upgrade() -> None:
    # New table CHECKs have no historical scan; nullable aggregate columns have no default or rewrite.
    if not sa.inspect(op.get_bind()).has_table('daily_telemetry_aggregate_lineage'):
        op.create_table('daily_telemetry_aggregate_lineage',
            sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False, primary_key=True),
            sa.Column('aggregate_id', sa.Uuid(),
                      sa.ForeignKey('daily_telemetry_aggregate.id', ondelete='CASCADE'), nullable=False),
            sa.Column('contract_key', sa.String(64), nullable=False),
            sa.Column('mapping_revision_id', sa.Uuid(),
                      sa.ForeignKey('integration_metric_mapping_revision.id', ondelete='RESTRICT')),
            sa.Column('contract_evidence', sa.String(32)), sa.Column('raw_unit', sa.String(32)),
            sa.Column('source_scale', sa.Numeric(18, 8)), sa.Column('sample_count', sa.Integer(), nullable=False),
            sa.Column('first_occurred_at', sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column('last_occurred_at', sa.TIMESTAMP(timezone=True), nullable=False),
            sa.UniqueConstraint('aggregate_id', 'contract_key', name='uq_daily_lineage_contract'),
            sa.CheckConstraint('sample_count >= 1', name='sample_count_positive'),
            sa.CheckConstraint('first_occurred_at <= last_occurred_at', name='event_time_order'),
            sa.CheckConstraint('(mapping_revision_id IS NULL) = (contract_evidence IS NULL)', name='revision_matches_evidence'),
            sa.CheckConstraint("contract_evidence IS NULL OR contract_evidence IN "
                               "('pinned','inferred_single_revision','operator_resolved')",
                               name='evidence_allowed'),
        )
        op.create_index('ix_daily_telemetry_aggregate_lineage_aggregate_id',
                        'daily_telemetry_aggregate_lineage', ['aggregate_id'])
    for name, type_ in COLUMNS.items():
        sql_type = type_.compile(dialect=op.get_bind().dialect)
        op.execute(f'ALTER TABLE daily_telemetry_aggregate ADD COLUMN IF NOT EXISTS {name} {sql_type}')
    for name, expression in CHECKS.items():
        # NOT VALID avoids an existing-table scan while ADD COLUMN holds ACCESS EXCLUSIVE.
        op.execute(f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'daily_telemetry_aggregate'::regclass
                           AND conname = 'ck_daily_telemetry_aggregate_{name}') THEN
                ALTER TABLE daily_telemetry_aggregate ADD CONSTRAINT ck_daily_telemetry_aggregate_{name}
                CHECK ({expression}) NOT VALID;
            END IF; END $$""")
    # Policy only: no hold purge exists or is activated. NULL can explicitly mean no hold expiry policy.
    op.execute('ALTER TABLE monitoring_policy ADD COLUMN IF NOT EXISTS resolved_hold_retention_days INTEGER DEFAULT 365')
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_policy'::regclass
                       AND conname = 'ck_monitoring_policy_resolved_hold_retention_positive') THEN
            ALTER TABLE monitoring_policy ADD CONSTRAINT ck_monitoring_policy_resolved_hold_retention_positive
            CHECK (resolved_hold_retention_days IS NULL OR resolved_hold_retention_days >= 1);
        END IF; END $$""")
    # Release ADD COLUMN's ACCESS EXCLUSIVE before scanning populated aggregates.
    # All pre-commit DDL is rerunnable if execution stops before revision stamping.
    with op.get_context().autocommit_block():
        for name in CHECKS:
            op.execute(f'ALTER TABLE daily_telemetry_aggregate VALIDATE CONSTRAINT ck_daily_telemetry_aggregate_{name}')


def downgrade() -> None:
    # Lock every inspected table before checking: a concurrent policy edit must
    # not commit between the evidence check and DROP COLUMN.
    op.execute('LOCK TABLE daily_telemetry_aggregate, daily_telemetry_aggregate_lineage, '
               'monitoring_policy IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().scalar(sa.text('SELECT EXISTS(SELECT 1 FROM daily_telemetry_aggregate_lineage) OR '
                                   'EXISTS(SELECT 1 FROM daily_telemetry_aggregate WHERE evidence_class IS NOT NULL '
                                   'OR lineage_sample_count IS NOT NULL OR first_compacted_at IS NOT NULL '
                                   'OR raw_retention_days_at_compaction IS NOT NULL) OR '
                                   'EXISTS(SELECT 1 FROM monitoring_policy '
                                   'WHERE resolved_hold_retention_days IS DISTINCT FROM 365)')):
        raise RuntimeError('Cannot downgrade 0051: lineage evidence or configured hold retention exists; '
                           'retain the schema and stop writes.')
    op.drop_table('daily_telemetry_aggregate_lineage')
    for name in CHECKS:
        op.execute(f'ALTER TABLE daily_telemetry_aggregate DROP CONSTRAINT ck_daily_telemetry_aggregate_{name}')
    for name in COLUMNS:
        op.drop_column('daily_telemetry_aggregate', name)
    op.execute('ALTER TABLE monitoring_policy DROP CONSTRAINT ck_monitoring_policy_resolved_hold_retention_positive')
    op.drop_column('monitoring_policy', 'resolved_hold_retention_days')
