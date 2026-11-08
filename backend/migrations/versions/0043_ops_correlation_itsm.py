# ruff: noqa: E501
"""Issue #103: collector state transitions, correlation incidents, notifications and ITSM tickets.

Additive. Nine new tables; no existing table or row is changed. Alarm, collector, heartbeat and telemetry
rows remain the only source facts: incidents reference alarms and transitions by foreign key and are
removed with their incident, never the reverse. Downgrade refuses while any of these tables holds rows,
because dropping them would destroy delivery history, ticket references and collector transition history.

Revision ID: 0043_ops_correlation_itsm
Revises: 0042_power_protection_reports
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_ops_correlation_itsm"
down_revision = "0042_power_protection_reports"
branch_labels = None
depends_on = None

_TABLES = (  # dependants first
    "itsm_ticket",
    "itsm_connection",
    "notification_delivery",
    "notification_policy",
    "notification_channel",
    "correlation_member",
    "correlation_incident",
    "collector_transition",
    "collector_state",
)


def upgrade() -> None:
    op.create_table(
        "collector_state",
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("since", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_collector_state_generation_positive")),
        sa.CheckConstraint("state IN ('online', 'offline')", name=op.f("ck_collector_state_state_allowed")),
        sa.ForeignKeyConstraint(["collector_id"], ["collector.id"], name=op.f("fk_collector_state_collector_id_collector"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collector_id", name=op.f("pk_collector_state")),
    )

    op.create_table(
        "collector_transition",
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("collector_name", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=False),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("never_heartbeat", sa.Boolean(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint("from_state IN ('unknown', 'online', 'offline')", name=op.f("ck_collector_transition_from_state_allowed")),
        sa.CheckConstraint("from_state != to_state", name=op.f("ck_collector_transition_state_changed")),
        sa.CheckConstraint("to_state IN ('online', 'offline')", name=op.f("ck_collector_transition_to_state_allowed")),
        sa.ForeignKeyConstraint(["collector_id"], ["collector.id"], name=op.f("fk_collector_transition_collector_id_collector"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_transition")),
        sa.UniqueConstraint("collector_id", "generation", name=op.f("uq_collector_transition_collector_generation")),
    )
    op.create_index("ix_collector_transition_collector_detected", "collector_transition", ["collector_id", "detected_at"])
    op.create_index("ix_collector_transition_detected", "collector_transition", ["detected_at"])

    op.create_table(
        "correlation_incident",
        sa.Column("dedup_key", sa.String(length=160), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("cause_type", sa.String(length=32), nullable=False),
        sa.Column("cause_ref", sa.String(length=64), nullable=False),
        sa.Column("cause_label", sa.String(length=256), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("rationale", sa.String(length=None), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_member_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("causation_id", sa.String(length=128), nullable=True),
        sa.Column("method_version", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cause_type IN ('collector_transition', 'alarm', 'protection_device')", name=op.f("ck_correlation_incident_cause_type_allowed")),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name=op.f("ck_correlation_incident_confidence_allowed")),
        sa.CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL)", name=op.f("ck_correlation_incident_resolved_at_iff_resolved")),
        sa.CheckConstraint("rule IN ('collector_offline', 'shared_power_cause', 'same_device', 'same_integration')", name=op.f("ck_correlation_incident_rule_allowed")),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name=op.f("ck_correlation_incident_status_allowed")),
        sa.ForeignKeyConstraint(["resolved_by"], ["app_user.id"], name=op.f("fk_correlation_incident_resolved_by_app_user"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_correlation_incident")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_correlation_incident_dedup_key")),
    )
    op.create_index("ix_correlation_incident_site_opened", "correlation_incident", ["site_id", "opened_at"])
    op.create_index("ix_correlation_incident_status_opened", "correlation_incident", ["status", "opened_at"])

    op.create_table(
        "correlation_member",
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("member_type", sa.String(length=24), nullable=False),
        sa.Column("alarm_id", sa.Uuid(), nullable=True),
        sa.Column("transition_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("source_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint("(member_type = 'alarm' AND alarm_id IS NOT NULL AND transition_id IS NULL) OR (member_type = 'collector_transition' AND transition_id IS NOT NULL AND alarm_id IS NULL)", name=op.f("ck_correlation_member_exactly_one_source")),
        sa.CheckConstraint("member_type IN ('alarm', 'collector_transition')", name=op.f("ck_correlation_member_member_type_allowed")),
        sa.CheckConstraint("role IN ('cause', 'symptom')", name=op.f("ck_correlation_member_role_allowed")),
        sa.ForeignKeyConstraint(["alarm_id"], ["alarm.id"], name=op.f("fk_correlation_member_alarm_id_alarm"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["correlation_incident.id"], name=op.f("fk_correlation_member_incident_id_correlation_incident"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transition_id"], ["collector_transition.id"], name=op.f("fk_correlation_member_transition_id_collector_transition"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_correlation_member")),
    )
    op.create_index("ix_correlation_member_incident", "correlation_member", ["incident_id"])
    op.create_index("uq_correlation_member_alarm", "correlation_member", ["alarm_id"], unique=True, postgresql_where=sa.text("alarm_id IS NOT NULL"))
    op.create_index("uq_correlation_member_transition", "correlation_member", ["transition_id"], unique=True, postgresql_where=sa.text("transition_id IS NOT NULL"))

    op.create_table(
        "notification_channel",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url_display", sa.String(length=300), nullable=False),
        sa.Column("url_ciphertext", sa.String(length=4000), nullable=False),
        sa.Column("secret_ciphertext", sa.String(length=1000), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('webhook')", name=op.f("ck_notification_channel_kind_allowed")),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"], name=op.f("fk_notification_channel_created_by_app_user"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channel")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_channel_name")),
    )

    op.create_table(
        "notification_policy",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("min_confidence", sa.String(length=8), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("min_confidence IS NULL OR min_confidence IN ('high', 'medium', 'low')", name=op.f("ck_notification_policy_min_confidence_allowed")),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channel.id"], name=op.f("fk_notification_policy_channel_id_notification_channel"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_notification_policy_site_id_site"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_policy")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_policy_name")),
    )
    op.create_index("ix_notification_policy_channel", "notification_policy", ["channel_id"])

    op.create_table(
        "notification_delivery",
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=24), nullable=True),
        sa.Column("final_failure", sa.Boolean(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND claim_generation >= 0", name=op.f("ck_notification_delivery_counters_valid")),
        sa.CheckConstraint("event_type IN ('incident.opened', 'incident.updated', 'collector.offline', 'collector.online')", name=op.f("ck_notification_delivery_event_type_allowed")),
        sa.CheckConstraint("failure_code IS NULL OR failure_code IN ('TIMEOUT', 'CONNECTION_ERROR', 'RATE_LIMITED', 'HTTP_4XX', 'HTTP_5XX', 'TARGET_BLOCKED', 'CHANNEL_DISABLED', 'ATTEMPTS_EXHAUSTED', 'INTERNAL_ERROR')", name=op.f("ck_notification_delivery_failure_code_allowed")),
        sa.CheckConstraint("(status = 'failed') = (final_failure IS TRUE)", name=op.f("ck_notification_delivery_final_failure_matches_status")),
        sa.CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent', 'failed')", name=op.f("ck_notification_delivery_status_allowed")),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channel.id"], name=op.f("fk_notification_delivery_channel_id_notification_channel"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["correlation_incident.id"], name=op.f("fk_notification_delivery_incident_id_correlation_incident"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["policy_id"], ["notification_policy.id"], name=op.f("fk_notification_delivery_policy_id_notification_policy"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_notification_delivery_dedup_key")),
    )
    op.create_index("ix_notification_delivery_due", "notification_delivery", ["status", "next_attempt_at"])
    op.create_index("ix_notification_delivery_incident", "notification_delivery", ["incident_id"])
    op.create_index("ix_notification_delivery_source", "notification_delivery", ["source_type", "source_id"])

    op.create_table(
        "itsm_connection",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=300), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("secret_ciphertext", sa.String(length=1000), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_create", sa.Boolean(), nullable=False),
        sa.Column("min_confidence", sa.String(length=8), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("min_confidence IS NULL OR min_confidence IN ('high', 'medium', 'low')", name=op.f("ck_itsm_connection_min_confidence_allowed")),
        sa.CheckConstraint("provider IN ('servicenow')", name=op.f("ck_itsm_connection_provider_allowed")),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"], name=op.f("fk_itsm_connection_created_by_app_user"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_itsm_connection_site_id_site"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_itsm_connection")),
        sa.UniqueConstraint("name", name=op.f("uq_itsm_connection_name")),
    )

    op.create_table(
        "itsm_ticket",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_key", sa.String(length=128), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("external_number", sa.String(length=64), nullable=True),
        sa.Column("external_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pending_op", sa.String(length=8), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=24), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0 AND claim_generation >= 0", name=op.f("ck_itsm_ticket_counters_valid")),
        sa.CheckConstraint("external_state IN ('new', 'in_progress', 'on_hold', 'resolved', 'closed', 'canceled', 'unknown')", name=op.f("ck_itsm_ticket_external_state_allowed")),
        sa.CheckConstraint("failure_code IS NULL OR failure_code IN ('TIMEOUT', 'CONNECTION_ERROR', 'RATE_LIMITED', 'AUTH_FAILED', 'HTTP_4XX', 'HTTP_5XX', 'MALFORMED_RESPONSE', 'RESPONSE_TOO_LARGE', 'TARGET_BLOCKED', 'CONNECTION_DISABLED', 'ATTEMPTS_EXHAUSTED', 'INTERNAL_ERROR')", name=op.f("ck_itsm_ticket_failure_code_allowed")),
        sa.CheckConstraint("pending_op IN ('create', 'update')", name=op.f("ck_itsm_ticket_pending_op_allowed")),
        sa.CheckConstraint("status IN ('pending', 'syncing', 'retry', 'synced', 'failed')", name=op.f("ck_itsm_ticket_status_allowed")),
        sa.ForeignKeyConstraint(["connection_id"], ["itsm_connection.id"], name=op.f("fk_itsm_ticket_connection_id_itsm_connection"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["correlation_incident.id"], name=op.f("fk_itsm_ticket_incident_id_correlation_incident"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_itsm_ticket")),
        sa.UniqueConstraint("connection_id", "incident_id", name=op.f("uq_itsm_ticket_connection_incident")),
        sa.UniqueConstraint("correlation_key", name=op.f("uq_itsm_ticket_correlation_key")),
    )
    op.create_index("ix_itsm_ticket_due", "itsm_ticket", ["status", "next_attempt_at"])
    op.create_index("ix_itsm_ticket_external", "itsm_ticket", ["connection_id", "external_id"])
    op.create_index("ix_itsm_ticket_incident", "itsm_ticket", ["incident_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:  # noqa: S608 - fixed names
            raise RuntimeError(f"Refusing to drop {table}: it holds recorded data. Export or remove it first.")
    for table in _TABLES:
        op.drop_table(table)
