"""Issue #103: collector state transitions, correlation incidents, notifications and ITSM tickets.

Alarms and telemetry stay the only source facts. Everything here either records a derived observation
(a collector crossing the offline threshold, a correlation incident that *references* alarms) or the
delivery state of an outbound message. No row here is ever the authority for an alarm, an asset, the
topology or a collector's heartbeat history, and nothing here is written by the ingestion path.

Secrets (channel URLs, signing keys, ITSM passwords) are stored only as Fernet ciphertext
(`app/core/secrets.py`) and are never selected into API responses."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


def _in(values: tuple[str, ...]) -> str:
    """SQL IN list; `repr` of a one-element tuple would leave a trailing comma PostgreSQL rejects."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


COLLECTOR_STATES = ("online", "offline")
TRANSITION_FROM_STATES = ("unknown", "online", "offline")

INCIDENT_RULES = ("collector_offline", "shared_power_cause", "same_device", "same_integration")
INCIDENT_CAUSE_TYPES = ("collector_transition", "alarm", "protection_device")
INCIDENT_CONFIDENCES = ("high", "medium", "low")
INCIDENT_STATUSES = ("open", "acknowledged", "resolved")
MEMBER_TYPES = ("alarm", "collector_transition")
MEMBER_ROLES = ("cause", "symptom")

CHANNEL_KINDS = ("webhook",)
NOTIFICATION_EVENT_TYPES = ("incident.opened", "incident.updated", "collector.offline", "collector.online")
DELIVERY_STATUSES = ("pending", "sending", "retry", "sent", "failed")
DELIVERY_FAILURE_CODES = (
    "TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED", "HTTP_4XX", "HTTP_5XX", "TARGET_BLOCKED", "CHANNEL_DISABLED",
    "ATTEMPTS_EXHAUSTED", "INTERNAL_ERROR",
)

ITSM_PROVIDERS = ("servicenow",)
TICKET_STATUSES = ("pending", "syncing", "retry", "synced", "failed")
TICKET_OPS = ("create", "update")
TICKET_FAILURE_CODES = (
    "TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED", "AUTH_FAILED", "HTTP_4XX", "HTTP_5XX", "MALFORMED_RESPONSE",
    "RESPONSE_TOO_LARGE", "TARGET_BLOCKED", "CONNECTION_DISABLED", "ATTEMPTS_EXHAUSTED", "INTERNAL_ERROR",
)
TICKET_EXTERNAL_STATES = ("new", "in_progress", "on_hold", "resolved", "closed", "canceled", "unknown")


class CollectorState(Base):
    """Current derived connectivity state of a collector. `generation` increases by one on every
    transition and is the compare-and-swap token that makes concurrent sweeps safe."""

    __tablename__ = "collector_state"
    __table_args__ = (
        CheckConstraint(f"state IN {_in(COLLECTOR_STATES)}", name="state_allowed"),
        CheckConstraint("generation >= 1", name="generation_positive"),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    since: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CollectorTransition(Base, UUIDPkMixin):
    """Append-only history. Collector name and site are copied so the row stays meaningful after the
    collector is renamed or moved."""

    __tablename__ = "collector_transition"
    __table_args__ = (
        UniqueConstraint("collector_id", "generation", name="uq_collector_transition_collector_generation"),
        CheckConstraint(f"from_state IN {_in(TRANSITION_FROM_STATES)}", name="from_state_allowed"),
        CheckConstraint(f"to_state IN {_in(COLLECTOR_STATES)}", name="to_state_allowed"),
        CheckConstraint("from_state != to_state", name="state_changed"),
        Index("ix_collector_transition_collector_detected", "collector_id", "detected_at"),
        Index("ix_collector_transition_detected", "detected_at"),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector.id", ondelete="CASCADE"), nullable=False)
    collector_name: Mapped[str] = mapped_column(String(128), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column()
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(16), nullable=False)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    never_heartbeat: Mapped[bool] = mapped_column(nullable=False, default=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))


class CorrelationIncident(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "correlation_incident"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_correlation_incident_dedup_key"),
        CheckConstraint(f"rule IN {_in(INCIDENT_RULES)}", name="rule_allowed"),
        CheckConstraint(f"cause_type IN {_in(INCIDENT_CAUSE_TYPES)}", name="cause_type_allowed"),
        CheckConstraint(f"confidence IN {_in(INCIDENT_CONFIDENCES)}", name="confidence_allowed"),
        CheckConstraint(f"status IN {_in(INCIDENT_STATUSES)}", name="status_allowed"),
        CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL)", name="resolved_at_iff_resolved"),
        Index("ix_correlation_incident_status_opened", "status", "opened_at"),
        Index("ix_correlation_incident_site_opened", "site_id", "opened_at"),
    )

    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    cause_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cause_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    cause_label: Mapped[str] = mapped_column(String(256), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    site_id: Mapped[uuid.UUID | None] = mapped_column()
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_member_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(128))
    method_version: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CorrelationIncidentMember(Base, UUIDPkMixin):
    """A reference from an incident to one source fact. Deleting a member never touches the source;
    an alarm or transition can belong to at most one incident."""

    __tablename__ = "correlation_member"
    __table_args__ = (
        CheckConstraint(f"member_type IN {_in(MEMBER_TYPES)}", name="member_type_allowed"),
        CheckConstraint(f"role IN {_in(MEMBER_ROLES)}", name="role_allowed"),
        CheckConstraint(
            "(member_type = 'alarm' AND alarm_id IS NOT NULL AND transition_id IS NULL) OR "
            "(member_type = 'collector_transition' AND transition_id IS NOT NULL AND alarm_id IS NULL)",
            name="exactly_one_source",
        ),
        Index("uq_correlation_member_alarm", "alarm_id", unique=True, postgresql_where="alarm_id IS NOT NULL"),
        Index(
            "uq_correlation_member_transition", "transition_id", unique=True, postgresql_where="transition_id IS NOT NULL"
        ),
        Index("ix_correlation_member_incident", "incident_id"),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("correlation_incident.id", ondelete="CASCADE"), nullable=False)
    member_type: Mapped[str] = mapped_column(String(24), nullable=False)
    alarm_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alarm.id", ondelete="CASCADE"))
    transition_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collector_transition.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    source_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class NotificationChannel(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "notification_channel"
    __table_args__ = (
        UniqueConstraint("name", name="uq_notification_channel_name"),
        CheckConstraint(f"kind IN {_in(CHANNEL_KINDS)}", name="kind_allowed"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="webhook")
    url_display: Mapped[str] = mapped_column(String(300), nullable=False)
    url_ciphertext: Mapped[str] = mapped_column(String(4000), nullable=False)
    secret_ciphertext: Mapped[str | None] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class NotificationPolicy(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "notification_policy"
    __table_args__ = (
        UniqueConstraint("name", name="uq_notification_policy_name"),
        CheckConstraint(
            "min_confidence IS NULL OR min_confidence IN ('high', 'medium', 'low')", name="min_confidence_allowed"
        ),
        Index("ix_notification_policy_channel", "channel_id"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channel.id", ondelete="RESTRICT"), nullable=False)
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))
    min_confidence: Mapped[str | None] = mapped_column(String(8))
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class NotificationDelivery(Base, UUIDPkMixin, TimestampMixin):
    """One message to one channel for one source event. `dedup_key` is unique, so the same event can
    never be queued twice for the same policy. `claim_generation` fences workers: every status write
    after a send carries the generation it claimed, and a stale owner's write matches no row."""

    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_notification_delivery_dedup_key"),
        CheckConstraint(f"status IN {_in(DELIVERY_STATUSES)}", name="status_allowed"),
        CheckConstraint(f"event_type IN {_in(NOTIFICATION_EVENT_TYPES)}", name="event_type_allowed"),
        CheckConstraint(
            f"failure_code IS NULL OR failure_code IN {_in(DELIVERY_FAILURE_CODES)}", name="failure_code_allowed"
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND claim_generation >= 0", name="counters_valid"),
        CheckConstraint("(status = 'failed') = (final_failure IS TRUE)", name="final_failure_matches_status"),
        Index("ix_notification_delivery_due", "status", "next_attempt_at"),
        Index("ix_notification_delivery_incident", "incident_id"),
        Index("ix_notification_delivery_source", "source_type", "source_id"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_policy.id", ondelete="RESTRICT"), nullable=False)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channel.id", ondelete="RESTRICT"), nullable=False)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("correlation_incident.id", ondelete="SET NULL"))
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(24))
    final_failure: Mapped[bool] = mapped_column(nullable=False, default=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ItsmConnection(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "itsm_connection"
    __table_args__ = (
        UniqueConstraint("name", name="uq_itsm_connection_name"),
        CheckConstraint(f"provider IN {_in(ITSM_PROVIDERS)}", name="provider_allowed"),
        CheckConstraint(
            "min_confidence IS NULL OR min_confidence IN ('high', 'medium', 'low')", name="min_confidence_allowed"
        ),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="servicenow")
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(String(1000), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    auto_create: Mapped[bool] = mapped_column(nullable=False, default=False)
    min_confidence: Mapped[str | None] = mapped_column(String(8))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ItsmTicket(Base, UUIDPkMixin, TimestampMixin):
    """The link between a correlation incident and a ticket in an external system. `correlation_key` is
    deterministic, so a retry after an ambiguous failure finds the ticket it may already have created
    instead of making a second one. Fields from the remote system are stored only after sanitising and
    never feed back into incidents, alarms, assets or topology."""

    __tablename__ = "itsm_ticket"
    __table_args__ = (
        UniqueConstraint("connection_id", "incident_id", name="uq_itsm_ticket_connection_incident"),
        UniqueConstraint("correlation_key", name="uq_itsm_ticket_correlation_key"),
        CheckConstraint(f"status IN {_in(TICKET_STATUSES)}", name="status_allowed"),
        CheckConstraint(f"pending_op IN {_in(TICKET_OPS)}", name="pending_op_allowed"),
        CheckConstraint(f"external_state IN {_in(TICKET_EXTERNAL_STATES)}", name="external_state_allowed"),
        CheckConstraint(
            f"failure_code IS NULL OR failure_code IN {_in(TICKET_FAILURE_CODES)}", name="failure_code_allowed"
        ),
        CheckConstraint("attempts >= 0 AND claim_generation >= 0", name="counters_valid"),
        Index("ix_itsm_ticket_due", "status", "next_attempt_at"),
        Index("ix_itsm_ticket_incident", "incident_id"),
        Index("ix_itsm_ticket_external", "connection_id", "external_id"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("itsm_connection.id", ondelete="RESTRICT"), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("correlation_incident.id", ondelete="CASCADE"), nullable=False)
    correlation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64))
    external_number: Mapped[str | None] = mapped_column(String(64))
    external_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    pending_op: Mapped[str] = mapped_column(String(8), nullable=False, default="create")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(24))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_payload_hash: Mapped[str | None] = mapped_column(String(64))
