"""AuditLog per ARCHITECTURE_REVIEW.md §30/§30a. Partitioned by month on `timestamp`
(H6, resolved in v1.2) — the partition DDL lives in the Alembic migration as raw SQL
since SQLAlchemy's ORM layer maps to the partitioned parent table transparently; Postgres
routes inserts to the correct child partition on its own. The application's DB role has
no UPDATE/DELETE grant on this table (enforced by a migration-time GRANT statement, not
by this model) — only a separate `dcim_retention_admin` role, used solely by a scheduled
maintenance job, may ever touch old partitions (§30a)."""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RESULT_VALUES = ("success", "failure")
SOURCE_VALUES = ("ui", "api", "system", "import")


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(f"result IN {RESULT_VALUES!r}", name="result_allowed"),
        CheckConstraint(f"source IN {SOURCE_VALUES!r}", name="source_allowed"),
        {
            "postgresql_partition_by": "RANGE (timestamp)",
        },
    )

    # Partitioned tables require the partition key in every unique/primary key, so
    # `audit_id` alone cannot be the sole PK — the composite (audit_id, timestamp) is.
    audit_id: Mapped[uuid.UUID] = mapped_column(server_default=func.gen_random_uuid(), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True, nullable=False)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    user_agent: Mapped[str | None] = mapped_column(String(500))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    reason: Mapped[str | None] = mapped_column(String(1000))
