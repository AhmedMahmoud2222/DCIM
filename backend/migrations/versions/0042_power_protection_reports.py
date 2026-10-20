"""Issue #102: protection devices, utilization snapshots and queued power reports.

Additive. `power_node.node_type` gains `protection_device`; three tables are created:
`protection_device` (a one-to-one extension of its `power_node`), `power_utilization_snapshot` (immutable
hourly roll-ups every trend, forecast and report reads) and `power_report_job` (the report queue row).
No existing row is read or modified. Downgrade refuses to discard recorded protection devices, snapshots
or report jobs, then restores the previous node-type constraint.

Revision ID: 0042_power_protection_reports
Revises: 0041_idempotency_claim_fencing
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_power_protection_reports"
down_revision = "0041_idempotency_claim_fencing"
branch_labels = None
depends_on = None

_OLD_TYPES = (
    "('utility_intake', 'generator', 'ups', 'power_panel', 'power_circuit', 'pdu', 'pdu_outlet', 'equipment_power_input')"
)
_NEW_TYPES = (
    "('utility_intake', 'generator', 'ups', 'power_panel', 'power_circuit', 'pdu', 'pdu_outlet', "
    "'equipment_power_input', 'protection_device')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_power_node_node_type_allowed"), "power_node", type_="check")
    op.create_check_constraint(op.f("ck_power_node_node_type_allowed"), "power_node", f"node_type IN {_NEW_TYPES}")

    op.create_table(
        "protection_device",
        sa.Column("power_node_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("device_type", sa.String(length=16), nullable=False),
        sa.Column("rating_a", sa.Numeric(8, 2), nullable=False),
        sa.Column("voltage_v", sa.Numeric(8, 2), nullable=False),
        sa.Column("poles", sa.Integer(), nullable=False),
        sa.Column("phase_config", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("state_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "device_type IN ('breaker', 'fuse', 'switch', 'disconnect')", name=op.f("ck_protection_device_device_type_allowed")
        ),
        sa.CheckConstraint(
            "state IN ('closed', 'open', 'tripped', 'unknown')", name=op.f("ck_protection_device_state_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('in_service', 'maintenance', 'out_of_service')", name=op.f("ck_protection_device_status_allowed")
        ),
        sa.CheckConstraint("phase_config IN ('single', 'three')", name=op.f("ck_protection_device_phase_config_allowed")),
        sa.CheckConstraint("rating_a > 0 AND rating_a <= 6300", name=op.f("ck_protection_device_rating_a_range")),
        sa.CheckConstraint("voltage_v >= 24 AND voltage_v <= 1000", name=op.f("ck_protection_device_voltage_v_range")),
        sa.CheckConstraint("poles IN (1, 2, 3)", name=op.f("ck_protection_device_poles_allowed")),
        sa.CheckConstraint(
            "(phase_config = 'single' AND poles IN (1, 2)) OR (phase_config = 'three' AND poles = 3)",
            name=op.f("ck_protection_device_poles_match_phase_config"),
        ),
        sa.ForeignKeyConstraint(
            ["power_node_id"], ["power_node.id"], name=op.f("fk_protection_device_power_node_id_power_node"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["site.id"], name=op.f("fk_protection_device_site_id_site"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("power_node_id", name=op.f("pk_protection_device")),
    )
    op.create_index("ix_protection_device_site_id", "protection_device", ["site_id"])

    op.create_table(
        "power_utilization_snapshot",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("granularity", sa.String(length=8), nullable=False),
        sa.Column("bucket_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False),
        sa.Column("registry_version", sa.String(length=16), nullable=True),
        sa.Column("load_kw", sa.Numeric(14, 4), nullable=True),
        sa.Column("load_basis", sa.String(length=16), nullable=False),
        sa.Column("allocated_kw", sa.Numeric(14, 4), nullable=True),
        sa.Column("effective_capacity_kw", sa.Numeric(14, 4), nullable=True),
        sa.Column("headroom_kw", sa.Numeric(14, 4), nullable=True),
        sa.Column("utilization_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("expected_samples", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(6, 5), nullable=False),
        sa.Column("quality", sa.String(length=16), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("method_version", sa.String(length=16), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("granularity IN ('hour')", name=op.f("ck_power_utilization_snapshot_granularity_allowed")),
        sa.CheckConstraint("scope_type IN ('power_node', 'site')", name=op.f("ck_power_utilization_snapshot_scope_type_allowed")),
        sa.CheckConstraint(
            "quality IN ('measured', 'estimated', 'mixed', 'stale', 'missing')",
            name=op.f("ck_power_utilization_snapshot_quality_allowed"),
        ),
        sa.CheckConstraint(
            "load_basis IN ('measured', 'estimated', 'none')", name=op.f("ck_power_utilization_snapshot_load_basis_allowed")
        ),
        sa.CheckConstraint("bucket_end > bucket_start", name=op.f("ck_power_utilization_snapshot_bucket_ordered")),
        sa.CheckConstraint("window_end > window_start", name=op.f("ck_power_utilization_snapshot_window_ordered")),
        sa.CheckConstraint(
            "metric = 'power_kw' AND unit = 'kW'", name=op.f("ck_power_utilization_snapshot_canonical_metric_and_unit")
        ),
        sa.CheckConstraint("load_kw IS NULL OR load_kw >= 0", name=op.f("ck_power_utilization_snapshot_load_kw_non_negative")),
        sa.CheckConstraint(
            "effective_capacity_kw IS NULL OR effective_capacity_kw >= 0",
            name=op.f("ck_power_utilization_snapshot_capacity_kw_non_negative"),
        ),
        sa.CheckConstraint(
            "sample_count >= 0 AND expected_samples >= 0", name=op.f("ck_power_utilization_snapshot_sample_counts_non_negative")
        ),
        sa.CheckConstraint(
            "coverage_ratio >= 0 AND coverage_ratio <= 1", name=op.f("ck_power_utilization_snapshot_coverage_ratio_range")
        ),
        sa.CheckConstraint(
            "(load_basis = 'none') = (load_kw IS NULL)", name=op.f("ck_power_utilization_snapshot_load_basis_matches_load")
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["site.id"], name=op.f("fk_power_utilization_snapshot_site_id_site"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_utilization_snapshot")),
        sa.UniqueConstraint("granularity", "bucket_start", "scope_type", "scope_id", name="uq_power_snapshot_bucket_scope"),
    )
    op.create_index("ix_power_snapshot_scope_bucket", "power_utilization_snapshot", ["scope_type", "scope_id", "bucket_start"])
    op.create_index("ix_power_snapshot_site_bucket", "power_utilization_snapshot", ["site_id", "bucket_start"])

    op.create_table(
        "power_report_job",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("report_type", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("report_type IN ('operational')", name=op.f("ck_power_report_job_report_type_allowed")),
        sa.CheckConstraint("format IN ('json', 'csv')", name=op.f("ck_power_report_job_format_allowed")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name=op.f("ck_power_report_job_status_allowed")
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN ('GENERATION_FAILED', 'RESULT_TOO_LARGE', 'ATTEMPTS_EXHAUSTED')",
            name=op.f("ck_power_report_job_failure_code_allowed"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_power_report_job_attempts_non_negative")),
        sa.CheckConstraint("(status = 'completed') = (result IS NOT NULL)", name=op.f("ck_power_report_job_result_iff_completed")),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_code IS NOT NULL)", name=op.f("ck_power_report_job_failure_code_iff_failed")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["app_user.id"], name=op.f("fk_power_report_job_requested_by_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_power_report_job_site_id_site"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_report_job")),
    )
    op.create_index("ix_power_report_job_requester_created", "power_report_job", ["requested_by_user_id", "created_at"])
    op.create_index("ix_power_report_job_status_lease", "power_report_job", ["status", "lease_expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("protection_device", "power_utilization_snapshot", "power_report_job"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:  # noqa: S608 - fixed table names
            raise RuntimeError(f"Refusing to drop {table}: it holds recorded data. Export or remove it first.")
    if bind.execute(sa.text("SELECT 1 FROM power_node WHERE node_type = 'protection_device' LIMIT 1")).first() is not None:
        raise RuntimeError("Refusing to downgrade: protection_device power nodes exist.")
    op.drop_table("power_report_job")
    op.drop_table("power_utilization_snapshot")
    op.drop_table("protection_device")
    op.drop_constraint(op.f("ck_power_node_node_type_allowed"), "power_node", type_="check")
    op.create_check_constraint(op.f("ck_power_node_node_type_allowed"), "power_node", f"node_type IN {_OLD_TYPES}")
