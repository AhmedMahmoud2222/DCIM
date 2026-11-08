# ruff: noqa: E501
"""Issue #104: calibrated spatial digital twin.

Additive. New: floor_plan_calibration (immutable calibration lineage), floor_plan_import_sir (immutable
sanitized intermediate representation). Extended: floor_plan (current calibration pointer, DXF/VSDX source
formats), floor_plan_import_job (detected/declared format, identical-upload key), floor_plan_import_diagnostics
(source units/bbox, SIR hash, failure code), floor_plan_import_candidate (version, evidence, match status,
staged corrections), spatial_object (provenance, wall/column/obstacle/aisle types). No existing row changes.

Downgrade refuses while any Issue #104 data exists (calibrations, SIRs, DXF/VSDX jobs, new object types,
provenance, staged corrections, identical-upload keys), because dropping the columns would destroy lineage.

Revision ID: 0044_spatial_digital_twin
Revises: 0043_ops_correlation_itsm
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_spatial_digital_twin"
down_revision = "0043_ops_correlation_itsm"
branch_labels = None
depends_on = None

_OLD_OBJECT_TYPES = ("rack", "equipment", "room_outline", "annotation", "imported_shape")
_NEW_OBJECT_TYPES = (*_OLD_OBJECT_TYPES, "wall", "column", "obstacle", "aisle")
_OLD_FORMATS = ("svg", "png", "jpeg")
_NEW_FORMATS = (*_OLD_FORMATS, "dxf", "vsdx")


def upgrade() -> None:
    op.create_table(
        "floor_plan_calibration",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("floor_plan_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("source_units", sa.String(length=8), nullable=False),
        sa.Column("mm_per_unit", sa.Numeric(18, 9), nullable=False),
        sa.Column("origin_x", sa.Numeric(20, 6), nullable=False),
        sa.Column("origin_y", sa.Numeric(20, 6), nullable=False),
        sa.Column("y_axis", sa.String(length=4), nullable=False),
        sa.Column("rotation_quadrants", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("error_bound_mm", sa.Numeric(14, 3), nullable=True),
        sa.Column("relative_error", sa.Numeric(10, 6), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("reference", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("method IN ('declared_units', 'two_point', 'room_dimension', 'manual_scale')", name=op.f("ck_floor_plan_calibration_method_allowed")),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name=op.f("ck_floor_plan_calibration_confidence_allowed")),
        sa.CheckConstraint("source_units IN ('mm', 'cm', 'm', 'in', 'ft', 'px', 'unitless')", name=op.f("ck_floor_plan_calibration_source_units_allowed")),
        sa.CheckConstraint("mm_per_unit > 0", name=op.f("ck_floor_plan_calibration_scale_positive")),
        sa.CheckConstraint("y_axis IN ('up', 'down')", name=op.f("ck_floor_plan_calibration_y_axis_allowed")),
        sa.CheckConstraint("rotation_quadrants BETWEEN 0 AND 3", name=op.f("ck_floor_plan_calibration_rotation_quadrants_range")),
        sa.CheckConstraint("error_bound_mm IS NULL OR error_bound_mm >= 0", name=op.f("ck_floor_plan_calibration_error_bound_nonnegative")),
        sa.ForeignKeyConstraint(["floor_plan_id"], ["floor_plan.id"], name=op.f("fk_floor_plan_calibration_floor_plan_id_floor_plan"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["floor_plan_import_job.id"], name=op.f("fk_floor_plan_calibration_job_id_floor_plan_import_job"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["floor_plan_calibration.id"], name=op.f("fk_floor_plan_calibration_supersedes_id_floor_plan_calibration"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], name=op.f("fk_floor_plan_calibration_created_by_user_id_app_user"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan_calibration")),
        sa.UniqueConstraint("floor_plan_id", "sequence", name="uq_floor_plan_calibration_sequence"),
    )
    op.create_index("ix_floor_plan_calibration_floor_plan", "floor_plan_calibration", ["floor_plan_id"])
    # Immutability. ON DELETE SET NULL on supersedes_id/job_id would otherwise UPDATE a row, so the guard
    # only blocks changes to the calibration *values*; a cascade delete from a vanished floor plan is allowed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_floor_plan_calibration_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (SELECT 1 FROM floor_plan WHERE id = OLD.floor_plan_id) THEN
                    RAISE EXCEPTION 'floor_plan_calibration rows are immutable (DELETE rejected)';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.floor_plan_id IS DISTINCT FROM OLD.floor_plan_id OR NEW.sequence IS DISTINCT FROM OLD.sequence
               OR NEW.method IS DISTINCT FROM OLD.method OR NEW.source_units IS DISTINCT FROM OLD.source_units
               OR NEW.mm_per_unit IS DISTINCT FROM OLD.mm_per_unit OR NEW.origin_x IS DISTINCT FROM OLD.origin_x
               OR NEW.origin_y IS DISTINCT FROM OLD.origin_y OR NEW.y_axis IS DISTINCT FROM OLD.y_axis
               OR NEW.rotation_quadrants IS DISTINCT FROM OLD.rotation_quadrants
               OR NEW.error_bound_mm IS DISTINCT FROM OLD.error_bound_mm
               OR NEW.relative_error IS DISTINCT FROM OLD.relative_error
               OR NEW.confidence IS DISTINCT FROM OLD.confidence OR NEW.reference IS DISTINCT FROM OLD.reference
               OR NEW.warnings IS DISTINCT FROM OLD.warnings OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'floor_plan_calibration rows are immutable (UPDATE rejected)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_floor_plan_calibration_immutable BEFORE UPDATE OR DELETE ON floor_plan_calibration "
        "FOR EACH ROW EXECUTE FUNCTION fn_floor_plan_calibration_immutable()"
    )

    op.add_column("floor_plan", sa.Column("current_calibration_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_floor_plan_current_calibration", "floor_plan", "floor_plan_calibration", ["current_calibration_id"], ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(op.f("ck_floor_plan_source_format_allowed"), "floor_plan", type_="check")
    op.create_check_constraint(op.f("ck_floor_plan_source_format_allowed"), "floor_plan", f"source_format IS NULL OR source_format IN {_NEW_FORMATS!r}")

    op.add_column("floor_plan_import_job", sa.Column("detected_format", sa.String(length=8), nullable=True))
    op.add_column("floor_plan_import_job", sa.Column("declared_format", sa.String(length=32), nullable=True))
    op.add_column("floor_plan_import_job", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_floor_plan_import_job_dedup", "floor_plan_import_job", ["floor_plan_id", "dedup_key"], unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )

    op.create_table(
        "floor_plan_import_sir",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("sir_sha256", sa.String(length=64), nullable=False),
        sa.Column("sir", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["floor_plan_import_job.id"], name=op.f("fk_floor_plan_import_sir_job_id_floor_plan_import_job"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan_import_sir")),
        sa.UniqueConstraint("job_id", name=op.f("uq_floor_plan_import_sir_job_id")),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_floor_plan_import_sir_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'floor_plan_import_sir rows are immutable (UPDATE rejected)';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_floor_plan_import_sir_immutable BEFORE UPDATE ON floor_plan_import_sir "
        "FOR EACH ROW EXECUTE FUNCTION fn_floor_plan_import_sir_immutable()"
    )

    for column in (
        sa.Column("source_units", sa.String(length=8), nullable=True),
        sa.Column("units_trusted", sa.Boolean(), nullable=True),
        sa.Column("y_axis", sa.String(length=4), nullable=True),
        sa.Column("source_bbox", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sir_sha256", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
    ):
        op.add_column("floor_plan_import_diagnostics", column)

    for column in (
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_ref", sa.String(length=128), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("match_status", sa.String(length=16), nullable=False, server_default="not_applicable"),
        sa.Column("match_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("duplicate_of_spatial_object_id", sa.Uuid(), nullable=True),
        sa.Column("reconciled_calibration_id", sa.Uuid(), nullable=True),
        sa.Column("correction", postgresql.JSONB(), nullable=True),
        sa.Column("correction_history", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    ):
        op.add_column("floor_plan_import_candidate", column)
    op.create_foreign_key(
        op.f("fk_floor_plan_import_candidate_duplicate_of_spatial_object_id_spatial_object"), "floor_plan_import_candidate",
        "spatial_object", ["duplicate_of_spatial_object_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_floor_plan_import_candidate_reconciled_calibration_id_floor_plan_calibration"), "floor_plan_import_candidate",
        "floor_plan_calibration", ["reconciled_calibration_id"], ["id"], ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_floor_plan_import_candidate_match_status_allowed"), "floor_plan_import_candidate",
        "match_status IN ('not_applicable', 'unmatched', 'matched', 'ambiguous', 'conflict', 'duplicate')",
    )
    op.create_check_constraint(op.f("ck_floor_plan_import_candidate_version_positive"), "floor_plan_import_candidate", "version >= 1")

    op.add_column("spatial_object", sa.Column("provenance", postgresql.JSONB(), nullable=True))
    op.drop_constraint(op.f("ck_spatial_object_object_type_allowed"), "spatial_object", type_="check")
    op.create_check_constraint(op.f("ck_spatial_object_object_type_allowed"), "spatial_object", f"object_type IN {_NEW_OBJECT_TYPES!r}")


def downgrade() -> None:
    bind = op.get_bind()
    blockers = {
        "calibrations": "SELECT count(*) FROM floor_plan_calibration",
        "import SIRs": "SELECT count(*) FROM floor_plan_import_sir",
        "DXF/VSDX jobs": "SELECT count(*) FROM floor_plan_import_job WHERE detected_format IN ('dxf', 'vsdx') OR dedup_key IS NOT NULL",
        "DXF/VSDX floor plans": "SELECT count(*) FROM floor_plan WHERE source_format IN ('dxf', 'vsdx') OR current_calibration_id IS NOT NULL",
        "spatial lineage": "SELECT count(*) FROM spatial_object WHERE provenance IS NOT NULL OR object_type IN ('wall', 'column', 'obstacle', 'aisle')",
        "staged corrections": "SELECT count(*) FROM floor_plan_import_candidate WHERE correction IS NOT NULL OR match_status <> 'not_applicable' OR version > 1",
    }
    present = [label for label, sql in blockers.items() if bind.execute(sa.text(sql)).scalar_one()]
    if present:
        raise RuntimeError(
            "Refusing to downgrade 0044_spatial_digital_twin: it would destroy Issue #104 data ("
            + ", ".join(present) + "). Export or remove it first."
        )

    op.drop_constraint(op.f("ck_spatial_object_object_type_allowed"), "spatial_object", type_="check")
    op.create_check_constraint(op.f("ck_spatial_object_object_type_allowed"), "spatial_object", f"object_type IN {_OLD_OBJECT_TYPES!r}")
    op.drop_column("spatial_object", "provenance")

    op.drop_constraint(op.f("ck_floor_plan_import_candidate_version_positive"), "floor_plan_import_candidate", type_="check")
    op.drop_constraint(op.f("ck_floor_plan_import_candidate_match_status_allowed"), "floor_plan_import_candidate", type_="check")
    op.drop_constraint(op.f("fk_floor_plan_import_candidate_reconciled_calibration_id_floor_plan_calibration"), "floor_plan_import_candidate", type_="foreignkey")
    op.drop_constraint(op.f("fk_floor_plan_import_candidate_duplicate_of_spatial_object_id_spatial_object"), "floor_plan_import_candidate", type_="foreignkey")
    for column in (
        "correction_history", "correction", "reconciled_calibration_id", "duplicate_of_spatial_object_id", "match_score",
        "match_status", "evidence", "ordinal", "source_ref", "version",
    ):
        op.drop_column("floor_plan_import_candidate", column)
    for column in ("failure_code", "sir_sha256", "candidate_count", "source_bbox", "y_axis", "units_trusted", "source_units"):
        op.drop_column("floor_plan_import_diagnostics", column)

    op.execute("DROP TRIGGER IF EXISTS trg_floor_plan_import_sir_immutable ON floor_plan_import_sir")
    op.execute("DROP FUNCTION IF EXISTS fn_floor_plan_import_sir_immutable()")
    op.drop_table("floor_plan_import_sir")

    op.drop_index("uq_floor_plan_import_job_dedup", table_name="floor_plan_import_job")
    for column in ("dedup_key", "declared_format", "detected_format"):
        op.drop_column("floor_plan_import_job", column)

    op.drop_constraint(op.f("ck_floor_plan_source_format_allowed"), "floor_plan", type_="check")
    op.create_check_constraint(op.f("ck_floor_plan_source_format_allowed"), "floor_plan", f"source_format IS NULL OR source_format IN {_OLD_FORMATS!r}")
    op.drop_constraint("fk_floor_plan_current_calibration", "floor_plan", type_="foreignkey")
    op.drop_column("floor_plan", "current_calibration_id")

    op.execute("DROP TRIGGER IF EXISTS trg_floor_plan_calibration_immutable ON floor_plan_calibration")
    op.execute("DROP FUNCTION IF EXISTS fn_floor_plan_calibration_immutable()")
    op.drop_index("ix_floor_plan_calibration_floor_plan", table_name="floor_plan_calibration")
    op.drop_table("floor_plan_calibration")
