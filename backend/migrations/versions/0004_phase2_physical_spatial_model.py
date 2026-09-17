"""Phase 2: physical/spatial domain model (Rack, Equipment, RackPlacement,
EquipmentPlacement, FloorPlan/SpatialLayer/SpatialObject, floor-plan import pipeline) and
its RBAC permission additions.

Purely additive: no existing Phase 1 table, column, or constraint is modified. The
GiST exclusion constraints and the spatial/room-consistency trigger are hand-written raw
SQL (not expressed via SQLAlchemy's declarative `ExcludeConstraint`), following the same
precedent Phase 1's migration 0003 set for
`trg_managed_asset_replacement_acyclic` — this class of constraint is more reliable and
more reviewable as explicit SQL than fought through declarative ORM mapping.

Revision ID: 0004_phase2
Revises: 0003_correction
Create Date: 2026-09-17
"""
import uuid as _uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_phase2"
down_revision = "0003_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- catalog
    op.create_table(
        "rack_model",
        sa.Column("manufacturer", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rack_model")),
        sa.UniqueConstraint("manufacturer", "model_name", name="uq_rack_model_manufacturer_model_name"),
    )
    op.create_table(
        "equipment_model",
        sa.Column("manufacturer", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_model")),
        sa.UniqueConstraint("manufacturer", "model_name", name="uq_equipment_model_manufacturer_model_name"),
    )
    op.create_table(
        "rack_model_revision",
        sa.Column("rack_model_id", sa.Uuid(), nullable=False),
        sa.Column("height_u", sa.Integer(), nullable=False),
        sa.Column("width_mm", sa.Integer(), nullable=False),
        sa.Column("depth_mm", sa.Integer(), nullable=False),
        sa.Column("weight_capacity_kg", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["rack_model_id"], ["rack_model.id"], name=op.f("fk_rack_model_revision_rack_model_id_rack_model"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rack_model_revision")),
    )
    op.create_index("ix_rack_model_revision_rack_model_id", "rack_model_revision", ["rack_model_id"])
    op.create_table(
        "equipment_model_revision",
        sa.Column("equipment_model_id", sa.Uuid(), nullable=False),
        sa.Column("height_u", sa.Integer(), nullable=True),
        sa.Column("width_mm", sa.Integer(), nullable=True),
        sa.Column("depth_mm", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["equipment_model_id"], ["equipment_model.id"],
            name=op.f("fk_equipment_model_revision_equipment_model_id_equipment_model"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_model_revision")),
    )
    op.create_index("ix_equipment_model_revision_equipment_model_id", "equipment_model_revision", ["equipment_model_id"])

    # ---------------------------------------------------------------- ManagedAsset subtypes
    op.create_table(
        "rack",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("custom_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["id"], ["managed_asset.id"], name=op.f("fk_rack_id_managed_asset"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["model_revision_id"], ["rack_model_revision.id"],
            name=op.f("fk_rack_model_revision_id_rack_model_revision"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rack")),
    )
    op.create_table(
        "equipment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("mac_address", sa.String(length=17), nullable=True),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("service", sa.String(length=128), nullable=True),
        sa.Column("environment", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("custom_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["managed_asset.id"], name=op.f("fk_equipment_id_managed_asset"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_revision_id"], ["equipment_model_revision.id"],
            name=op.f("fk_equipment_model_revision_id_equipment_model_revision"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment")),
    )

    # ---------------------------------------------------------------- spatial
    op.create_table(
        "floor_plan",
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_file_name", sa.String(length=255), nullable=True),
        sa.Column("source_file_hash", sa.String(length=64), nullable=True),
        sa.Column("source_format", sa.String(length=8), nullable=True),
        sa.Column("calibration_scale_mm_per_px", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("room_width_mm", sa.Integer(), nullable=True),
        sa.Column("room_height_mm", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_format IS NULL OR source_format IN ('svg', 'png', 'jpeg')",
                            name=op.f("ck_floor_plan_source_format_allowed")),
        sa.CheckConstraint("status IN ('draft', 'active', 'superseded')", name=op.f("ck_floor_plan_status_allowed")),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"],
                                 name=op.f("fk_floor_plan_created_by_user_id_app_user"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_floor_plan_room_id_room"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan")),
        sa.UniqueConstraint("room_id", "revision_number", name="uq_floor_plan_room_revision"),
    )
    op.create_index("ix_floor_plan_room_status", "floor_plan", ["room_id", "status"])
    # §8: "exactly one ACTIVE FloorPlan at a time" per room — a hard DB invariant, not
    # merely an application convention.
    op.execute(
        "CREATE UNIQUE INDEX uq_floor_plan_one_active_per_room ON floor_plan (room_id) WHERE status = 'active'"
    )

    op.create_table(
        "spatial_layer",
        sa.Column("floor_plan_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("layer_type", sa.String(length=32), nullable=False),
        sa.Column("z_order", sa.Integer(), nullable=False),
        sa.Column("visible_by_default", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "layer_type IN ('background', 'room_outline', 'racks', 'equipment', 'annotations', 'imported')",
            name=op.f("ck_spatial_layer_layer_type_allowed"),
        ),
        sa.ForeignKeyConstraint(["floor_plan_id"], ["floor_plan.id"],
                                 name=op.f("fk_spatial_layer_floor_plan_id_floor_plan"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spatial_layer")),
    )
    op.create_table(
        "spatial_object",
        sa.Column("spatial_layer_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("geometry_type", sa.String(length=16), nullable=False),
        sa.Column("x_mm", sa.Integer(), nullable=False),
        sa.Column("y_mm", sa.Integer(), nullable=False),
        sa.Column("width_mm", sa.Integer(), nullable=True),
        sa.Column("height_mm", sa.Integer(), nullable=True),
        sa.Column("rotation_deg", sa.Integer(), nullable=False),
        sa.Column("geometry_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("geometry_type IN ('rect', 'polygon', 'circle', 'text', 'path')",
                            name=op.f("ck_spatial_object_geometry_type_allowed")),
        sa.CheckConstraint("object_type IN ('rack', 'equipment', 'room_outline', 'annotation', 'imported_shape')",
                            name=op.f("ck_spatial_object_object_type_allowed")),
        sa.CheckConstraint("source IN ('authoritative', 'imported', 'discovered')",
                            name=op.f("ck_spatial_object_source_allowed")),
        sa.ForeignKeyConstraint(["spatial_layer_id"], ["spatial_layer.id"],
                                 name=op.f("fk_spatial_object_spatial_layer_id_spatial_layer"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spatial_object")),
    )
    op.create_index("ix_spatial_object_layer", "spatial_object", ["spatial_layer_id"])

    # ---------------------------------------------------------------- placement
    op.create_table(
        "rack_placement",
        sa.Column("rack_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("spatial_object_id", sa.Uuid(), nullable=True),
        sa.Column("x_mm", sa.Integer(), nullable=True),
        sa.Column("y_mm", sa.Integer(), nullable=True),
        sa.Column("rotation_deg", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["rack_id"], ["managed_asset.id"], name=op.f("fk_rack_placement_rack_id_managed_asset"),
                                 ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_rack_placement_room_id_room"),
                                 ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["spatial_object_id"], ["spatial_object.id"],
                                 name=op.f("fk_rack_placement_spatial_object_id_spatial_object"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rack_placement")),
        sa.UniqueConstraint("spatial_object_id", name=op.f("uq_rack_placement_spatial_object_id")),
    )
    op.create_index("ix_rack_placement_rack_effective_to", "rack_placement", ["rack_id", "effective_to"])

    op.create_table(
        "equipment_placement",
        sa.Column("equipment_id", sa.Uuid(), nullable=False),
        sa.Column("placement_type", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("rack_id", sa.Uuid(), nullable=True),
        sa.Column("u_range", postgresql.INT4RANGE(), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("occupies_front", sa.Boolean(), sa.Computed("COALESCE(side IN ('front', 'both'), false)", persisted=True),
                  nullable=False),
        sa.Column("occupies_rear", sa.Boolean(), sa.Computed("COALESCE(side IN ('rear', 'both'), false)", persisted=True),
                  nullable=False),
        sa.Column("spatial_object_id", sa.Uuid(), nullable=True),
        sa.Column("rotation_deg", sa.Integer(), nullable=True),
        sa.Column("mounting_method", sa.String(length=64), nullable=True),
        sa.Column("orientation", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "placement_type != 'rack_mounted' OR (rack_id IS NOT NULL AND u_range IS NOT NULL AND side IS NOT NULL)",
            name=op.f("ck_equipment_placement_rack_mounted_requires_rack_u_range_and_side"),
        ),
        sa.CheckConstraint(
            "placement_type IN ('rack_mounted', 'floor_standing', 'wall_mounted', 'ceiling_mounted', 'other')",
            name=op.f("ck_equipment_placement_placement_type_allowed"),
        ),
        sa.CheckConstraint("side IS NULL OR side IN ('front', 'rear', 'both')",
                            name=op.f("ck_equipment_placement_side_allowed")),
        sa.ForeignKeyConstraint(["equipment_id"], ["managed_asset.id"],
                                 name=op.f("fk_equipment_placement_equipment_id_managed_asset"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rack_id"], ["managed_asset.id"],
                                 name=op.f("fk_equipment_placement_rack_id_managed_asset"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_equipment_placement_room_id_room"),
                                 ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["spatial_object_id"], ["spatial_object.id"],
                                 name=op.f("fk_equipment_placement_spatial_object_id_spatial_object"),
                                 ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_placement")),
        sa.UniqueConstraint("spatial_object_id", name="uq_equipment_placement_spatial_object_id"),
    )
    op.create_index("ix_equipment_placement_equipment_effective_to", "equipment_placement",
                     ["equipment_id", "effective_to"])
    op.create_index("ix_equipment_placement_rack_effective_to", "equipment_placement", ["rack_id", "effective_to"])

    # --- §7a: two partial GiST exclusion constraints closing the front/rear overlap
    # truth table (front-front/front-both/rear-rear/rear-both/both-both blocked;
    # front-rear allowed). btree_gist (enabled in Phase 1) supplies the `=` operator
    # class GiST needs for the uuid equality term.
    op.execute(
        "ALTER TABLE equipment_placement ADD CONSTRAINT no_front_overlap "
        "EXCLUDE USING gist (rack_id WITH =, u_range WITH &&) "
        "WHERE (placement_type = 'rack_mounted' AND effective_to IS NULL AND occupies_front)"
    )
    op.execute(
        "ALTER TABLE equipment_placement ADD CONSTRAINT no_rear_overlap "
        "EXCLUDE USING gist (rack_id WITH =, u_range WITH &&) "
        "WHERE (placement_type = 'rack_mounted' AND effective_to IS NULL AND occupies_rear)"
    )
    # --- §7b: at most one current placement per asset, and no two historical intervals
    # for the same asset ever overlap in time. Applied identically to RackPlacement (§8).
    op.execute(
        "ALTER TABLE equipment_placement ADD CONSTRAINT equipment_placement_one_timeline_per_asset "
        "EXCLUDE USING gist (equipment_id WITH =, tstzrange(effective_from, effective_to) WITH &&)"
    )
    op.execute(
        "ALTER TABLE rack_placement ADD CONSTRAINT rack_placement_one_timeline_per_asset "
        "EXCLUDE USING gist (rack_id WITH =, tstzrange(effective_from, effective_to) WITH &&)"
    )

    # --- §8: "a BEFORE INSERT OR UPDATE trigger checks that
    # spatial_object.spatial_layer_id -> floor_plan.room_id = <placement>.room_id" — a
    # rack/equipment cannot be visually placed on a floor plan belonging to a different
    # room than its authoritative placement. One shared function, since both tables
    # expose the same NEW.spatial_object_id / NEW.room_id shape.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_placement_spatial_object_room_match() RETURNS trigger AS $$
        DECLARE
            resolved_room_id uuid;
        BEGIN
            IF NEW.spatial_object_id IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT fp.room_id INTO resolved_room_id
            FROM spatial_object so
            JOIN spatial_layer sl ON sl.id = so.spatial_layer_id
            JOIN floor_plan fp ON fp.id = sl.floor_plan_id
            WHERE so.id = NEW.spatial_object_id;

            IF resolved_room_id IS NULL THEN
                RAISE EXCEPTION 'spatial_object % does not resolve to a floor plan room', NEW.spatial_object_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF resolved_room_id != NEW.room_id THEN
                RAISE EXCEPTION
                    'spatial_object % belongs to a different room than this placement (room %)',
                    NEW.spatial_object_id, NEW.room_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_rack_placement_spatial_object_room_match "
        "BEFORE INSERT OR UPDATE OF spatial_object_id, room_id ON rack_placement "
        "FOR EACH ROW EXECUTE FUNCTION check_placement_spatial_object_room_match()"
    )
    op.execute(
        "CREATE TRIGGER trg_equipment_placement_spatial_object_room_match "
        "BEFORE INSERT OR UPDATE OF spatial_object_id, room_id ON equipment_placement "
        "FOR EACH ROW EXECUTE FUNCTION check_placement_spatial_object_room_match()"
    )

    # ---------------------------------------------------------------- floor-plan import
    op.create_table(
        "floor_plan_import_job",
        sa.Column("floor_plan_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'quarantined', 'parsing', 'parsed', 'failed', 'rejected')",
                            name=op.f("ck_floor_plan_import_job_status_allowed")),
        sa.ForeignKeyConstraint(["floor_plan_id"], ["floor_plan.id"],
                                 name=op.f("fk_floor_plan_import_job_floor_plan_id_floor_plan"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["app_user.id"],
                                 name=op.f("fk_floor_plan_import_job_uploaded_by_user_id_app_user"),
                                 ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan_import_job")),
    )
    op.create_table(
        "floor_plan_import_diagnostics",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("source_format", sa.String(length=8), nullable=True),
        sa.Column("parser_name", sa.String(length=64), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("objects_discovered", sa.Integer(), nullable=False),
        sa.Column("objects_classified", sa.Integer(), nullable=False),
        sa.Column("racks_detected", sa.Integer(), nullable=False),
        sa.Column("equipment_detected", sa.Integer(), nullable=False),
        sa.Column("unsupported_object_count", sa.Integer(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["floor_plan_import_job.id"],
                                 name=op.f("fk_floor_plan_import_diagnostics_job_id_floor_plan_import_job"),
                                 ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan_import_diagnostics")),
        sa.UniqueConstraint("job_id", name=op.f("uq_floor_plan_import_diagnostics_job_id")),
    )
    op.create_table(
        "floor_plan_import_candidate",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("raw_geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("suggested_object_type", sa.String(length=32), nullable=True),
        sa.Column("suggested_label", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("matched_asset_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resulting_spatial_object_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'rejected')",
                            name=op.f("ck_floor_plan_import_candidate_status_allowed")),
        sa.ForeignKeyConstraint(["job_id"], ["floor_plan_import_job.id"],
                                 name=op.f("fk_floor_plan_import_candidate_job_id_floor_plan_import_job"),
                                 ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matched_asset_id"], ["managed_asset.id"],
                                 name=op.f("fk_floor_plan_import_candidate_matched_asset_id_managed_asset"),
                                 ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resulting_spatial_object_id"], ["spatial_object.id"],
                                 name=op.f(
                                     "fk_floor_plan_import_candidate_resulting_spatial_object_id_spatial_object"
                                 ), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["app_user.id"],
                                 name=op.f("fk_floor_plan_import_candidate_reviewed_by_user_id_app_user"),
                                 ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_floor_plan_import_candidate")),
    )
    op.create_index("ix_floor_plan_import_candidate_job_status", "floor_plan_import_candidate", ["job_id", "status"])

    # ---------------------------------------------------------------- RBAC seed (additive)
    _seed_new_permissions()


def _seed_new_permissions() -> None:
    """Adds the Phase 2 permission codes (rack:*/equipment:*/floor_plan:*/spatial:*) to
    the existing RBAC tables without touching any Phase 1 permission/role/role_permission
    row. `DEFAULT_ROLE_PERMISSIONS` is the same single source of truth migration 0002
    imported from; this only inserts what's missing, exactly like 0002 does for a fresh
    database, so re-running Phase 2 on top of a database that somehow already has these
    codes (e.g. a repeated partial apply) is a safe no-op."""
    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS

    bind = op.get_bind()

    all_codes = sorted({code for codes in DEFAULT_ROLE_PERMISSIONS.values() for code in codes})
    existing_permissions = {
        (row.resource, row.action): row.id
        for row in bind.execute(sa.text("SELECT id, resource, action FROM permission")).fetchall()
    }
    existing_roles = {
        row.name: row.id for row in bind.execute(sa.text("SELECT id, name FROM role")).fetchall()
    }
    existing_role_permissions = {
        (row.role_id, row.permission_id)
        for row in bind.execute(sa.text("SELECT role_id, permission_id FROM role_permission")).fetchall()
    }

    permission_ids: dict[tuple[str, str], _uuid.UUID] = dict(existing_permissions)
    for code in all_codes:
        resource, action = code.split(":")
        if (resource, action) in permission_ids:
            continue
        new_id = _uuid.uuid4()
        permission_ids[(resource, action)] = new_id
        bind.execute(
            sa.text(
                "INSERT INTO permission (id, resource, action, description) "
                "VALUES (:id, :resource, :action, :description)"
            ),
            {"id": new_id, "resource": resource, "action": action, "description": f"{action} on {resource}"},
        )

    for role_name, codes in DEFAULT_ROLE_PERMISSIONS.items():
        role_id = existing_roles.get(role_name)
        if role_id is None:
            # A custom deployment renamed/removed a default role — nothing to seed for it.
            continue
        for code in codes:
            resource, action = code.split(":")
            permission_id = permission_ids[(resource, action)]
            if (role_id, permission_id) in existing_role_permissions:
                continue
            bind.execute(
                sa.text("INSERT INTO role_permission (role_id, permission_id) VALUES (:role_id, :permission_id)"),
                {"role_id": role_id, "permission_id": permission_id},
            )
            existing_role_permissions.add((role_id, permission_id))


def downgrade() -> None:
    # RBAC rows are left in place on downgrade — same precedent as 0002_seed's downgrade,
    # which likewise does not attempt to surgically remove seeded permission rows (a
    # partial RBAC state is worse than a few unused permission codes on a role nobody's
    # using them against, since the code granting them no longer exists after downgrade).
    op.drop_index("ix_floor_plan_import_candidate_job_status", table_name="floor_plan_import_candidate")
    op.drop_table("floor_plan_import_candidate")
    op.drop_table("floor_plan_import_diagnostics")
    op.drop_table("floor_plan_import_job")

    op.execute("DROP TRIGGER IF EXISTS trg_equipment_placement_spatial_object_room_match ON equipment_placement")
    op.execute("DROP TRIGGER IF EXISTS trg_rack_placement_spatial_object_room_match ON rack_placement")
    op.execute("DROP FUNCTION IF EXISTS check_placement_spatial_object_room_match()")

    op.drop_index("ix_equipment_placement_rack_effective_to", table_name="equipment_placement")
    op.drop_index("ix_equipment_placement_equipment_effective_to", table_name="equipment_placement")
    op.drop_table("equipment_placement")
    op.drop_index("ix_rack_placement_rack_effective_to", table_name="rack_placement")
    op.drop_table("rack_placement")

    op.drop_index("ix_spatial_object_layer", table_name="spatial_object")
    op.drop_table("spatial_object")
    op.drop_table("spatial_layer")
    op.execute("DROP INDEX IF EXISTS uq_floor_plan_one_active_per_room")
    op.drop_index("ix_floor_plan_room_status", table_name="floor_plan")
    op.drop_table("floor_plan")

    op.drop_table("equipment")
    op.drop_table("rack")

    op.drop_index("ix_equipment_model_revision_equipment_model_id", table_name="equipment_model_revision")
    op.drop_table("equipment_model_revision")
    op.drop_index("ix_rack_model_revision_rack_model_id", table_name="rack_model_revision")
    op.drop_table("rack_model_revision")
    op.drop_table("equipment_model")
    op.drop_table("rack_model")
