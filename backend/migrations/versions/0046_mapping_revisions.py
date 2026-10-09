"""Issue #128 / G1: immutable metric-mapping revisions and event-time contract pinning.

Revision ID: 0046_mapping_revisions
Revises: 0045_cooling_thermal

Additive only. New: integration_metric_mapping_revision (append-only; a trigger rejects every UPDATE and every
DELETE except the cascade that removes the revisions of a mapping being deleted), telemetry_contract_hold (records
Central refused to interpret ambiguously, retained so bounded retry never loses data). Extended, all nullable:
integration_metric_mapping.current_revision_id, telemetry_reading.mapping_revision_id / contract_evidence.

Data written: exactly one revision per existing mapping, labelled provenance = 'backfilled_from_current'. It is
the mapping row as it stands now with `effective_from` = this migration's time; it is NOT evidence of the contract
in force when any earlier reading was measured, and no earlier telemetry_reading is touched or reinterpreted
(NULL revision + NULL evidence means "stored before revision pinning").

Locks: the two foreign keys and the CHECK constraints on telemetry_reading are added NOT VALID and then
VALIDATEd, so the large table is held only under the brief ACCESS EXCLUSIVE needed to add nullable columns
(metadata only) and SHARE ROW EXCLUSIVE for ADD CONSTRAINT ... NOT VALID, and the full scans run under
SHARE UPDATE EXCLUSIVE, which does not block reads or writes.

Downgrade refuses once anything beyond the seed exists: an authored revision, a second revision, a reading that
carries a revision or evidence, or a hold row.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_mapping_revisions"
down_revision = "0045_cooling_thermal"
branch_labels = None
depends_on = None

PROVENANCE = ("authored", "backfilled_from_current")
EVIDENCE = ("pinned", "inferred_single_revision", "operator_resolved")
HOLD_STATUSES = ("held", "expired", "resolved")


def upgrade() -> None:
    op.create_table(
        "integration_metric_mapping_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("mapping_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_identifier", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("canonical_metric", sa.String(64), nullable=False),
        sa.Column("source_unit", sa.String(32), nullable=False),
        sa.Column("source_scale", sa.Numeric(18, 8), nullable=False),
        sa.Column("registry_version", sa.String(16), nullable=True),
        sa.Column("conversion_hash", sa.String(64), nullable=False),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("effective_from", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_metric_mapping_revision")),
        sa.ForeignKeyConstraint(
            ["mapping_id"], ["integration_metric_mapping.id"], ondelete="CASCADE",
            name="fk_mapping_revision_mapping",
        ),
        sa.UniqueConstraint("mapping_id", "revision", name="uq_mapping_revision_mapping_revision"),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_integration_metric_mapping_revision_revision_positive")),
        sa.CheckConstraint(
            f"provenance IN {PROVENANCE!r}", name=op.f("ck_integration_metric_mapping_revision_provenance_allowed")
        ),
        sa.CheckConstraint(
            "char_length(conversion_hash) = 64", name=op.f("ck_integration_metric_mapping_revision_conversion_hash_length")
        ),
    )
    op.create_index(
        op.f("ix_integration_metric_mapping_revision_mapping_id"), "integration_metric_mapping_revision", ["mapping_id"]
    )
    op.create_index(
        op.f("ix_integration_metric_mapping_revision_integration_id"), "integration_metric_mapping_revision", ["integration_id"]
    )
    op.execute("""
        CREATE FUNCTION forbid_mapping_revision_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'integration_metric_mapping_revision is append-only: UPDATE is not allowed'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            -- DELETE: allowed only as the cascade of deleting the mapping itself (the parent row is already gone).
            IF EXISTS (SELECT 1 FROM integration_metric_mapping WHERE id = OLD.mapping_id) THEN
                RAISE EXCEPTION 'integration_metric_mapping_revision is append-only: DELETE is not allowed'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_mapping_revision_append_only
        BEFORE UPDATE OR DELETE ON integration_metric_mapping_revision
        FOR EACH ROW EXECUTE FUNCTION forbid_mapping_revision_change()
    """)

    op.add_column("integration_metric_mapping", sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_metric_mapping_current_revision", "integration_metric_mapping", "integration_metric_mapping_revision",
        ["current_revision_id"], ["id"],
    )

    op.add_column("telemetry_reading", sa.Column("mapping_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("telemetry_reading", sa.Column("contract_evidence", sa.String(32), nullable=True))
    op.execute("""
        ALTER TABLE telemetry_reading ADD CONSTRAINT fk_telemetry_reading_mapping_revision
        FOREIGN KEY (mapping_revision_id) REFERENCES integration_metric_mapping_revision (id) ON DELETE RESTRICT NOT VALID
    """)
    op.execute(f"""
        ALTER TABLE telemetry_reading ADD CONSTRAINT ck_telemetry_reading_contract_evidence_allowed
        CHECK (contract_evidence IS NULL OR contract_evidence IN {EVIDENCE!r}) NOT VALID
    """)
    op.execute("""
        ALTER TABLE telemetry_reading ADD CONSTRAINT ck_telemetry_reading_revision_matches_evidence
        CHECK ((mapping_revision_id IS NULL) = (contract_evidence IS NULL)) NOT VALID
    """)
    op.execute("ALTER TABLE telemetry_reading VALIDATE CONSTRAINT fk_telemetry_reading_mapping_revision")
    op.execute("ALTER TABLE telemetry_reading VALIDATE CONSTRAINT ck_telemetry_reading_contract_evidence_allowed")
    op.execute("ALTER TABLE telemetry_reading VALIDATE CONSTRAINT ck_telemetry_reading_revision_matches_evidence")

    op.create_table(
        "telemetry_contract_hold",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_identifier", sa.String(255), nullable=False),
        sa.Column("external_identifier", sa.String(255), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("value_text", sa.String(64), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("first_held_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_held_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_reading_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_contract_hold")),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], ondelete="CASCADE", name=op.f("fk_telemetry_contract_hold_collector_id_collector")
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["integration.id"], ondelete="CASCADE",
            name=op.f("fk_telemetry_contract_hold_integration_id_integration"),
        ),
        sa.UniqueConstraint("collector_id", "dedup_key", name="uq_telemetry_contract_hold_collector_dedup"),
        sa.CheckConstraint(f"status IN {HOLD_STATUSES!r}", name=op.f("ck_telemetry_contract_hold_status_allowed")),
    )
    op.create_index(op.f("ix_telemetry_contract_hold_integration_id"), "telemetry_contract_hold", ["integration_id"])
    op.create_index("ix_telemetry_contract_hold_status_last", "telemetry_contract_hold", ["status", "last_held_at"])

    # Seed: one revision per existing mapping, honestly labelled. The hash is computed by the application's
    # canonical encoding (not SQL) so that it matches what ingest recomputes.
    from decimal import Decimal
    from uuid import uuid4

    from app.domain.telemetry.contract import conversion_contract_hash_or_unresolved

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, integration_id, source_identifier, canonical_metric, unit, scale, registry_version "
        "FROM integration_metric_mapping ORDER BY created_at, id"
    )).all()
    now = bind.scalar(sa.text("SELECT now()"))
    for row in rows:
        revision_id = uuid4()
        bind.execute(sa.text("""
            INSERT INTO integration_metric_mapping_revision
                (id, mapping_id, integration_id, source_identifier, revision, canonical_metric, source_unit,
                 source_scale, registry_version, conversion_hash, provenance, effective_from, created_at)
            VALUES (:id, :mapping_id, :integration_id, :source_identifier, 1, :metric, :unit, :scale, :version,
                    :hash, 'backfilled_from_current', :now, :now)
        """), {
            "id": revision_id, "mapping_id": row.id, "integration_id": row.integration_id,
            "source_identifier": row.source_identifier, "metric": row.canonical_metric, "unit": row.unit,
            "scale": row.scale, "version": row.registry_version, "now": now,
            "hash": conversion_contract_hash_or_unresolved(
                row.canonical_metric, row.unit, Decimal(str(row.scale)), row.registry_version
            ),
        })
        bind.execute(
            sa.text("UPDATE integration_metric_mapping SET current_revision_id = :rev WHERE id = :id"),
            {"rev": revision_id, "id": row.id},
        )


def downgrade() -> None:
    op.execute("""
        LOCK TABLE integration_metric_mapping, integration_metric_mapping_revision, telemetry_reading,
            telemetry_contract_hold IN ACCESS EXCLUSIVE MODE
    """)
    populated = op.get_bind().scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM integration_metric_mapping_revision WHERE provenance <> 'backfilled_from_current' OR revision <> 1
            UNION ALL
            SELECT 1 FROM telemetry_reading WHERE mapping_revision_id IS NOT NULL OR contract_evidence IS NOT NULL
            UNION ALL
            SELECT 1 FROM telemetry_contract_hold
        )
    """))
    if populated:
        raise RuntimeError(
            "Cannot downgrade 0046: authored mapping revisions, revision-pinned readings or held telemetry records "
            "exist. Dropping them would destroy the only record of the contract those readings were interpreted "
            "under. Preserve this evidence and establish an explicit data contract before rollback."
        )
    op.drop_table("telemetry_contract_hold")
    op.drop_constraint(op.f("ck_telemetry_reading_revision_matches_evidence"), "telemetry_reading", type_="check")
    op.drop_constraint(op.f("ck_telemetry_reading_contract_evidence_allowed"), "telemetry_reading", type_="check")
    op.drop_constraint("fk_telemetry_reading_mapping_revision", "telemetry_reading", type_="foreignkey")
    op.drop_column("telemetry_reading", "contract_evidence")
    op.drop_column("telemetry_reading", "mapping_revision_id")
    op.drop_constraint("fk_metric_mapping_current_revision", "integration_metric_mapping", type_="foreignkey")
    op.drop_column("integration_metric_mapping", "current_revision_id")
    op.execute("DROP TRIGGER trg_mapping_revision_append_only ON integration_metric_mapping_revision")
    op.drop_table("integration_metric_mapping_revision")
    op.execute("DROP FUNCTION forbid_mapping_revision_change()")
