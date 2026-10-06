"""Append-only evidence for each explicit extraction-to-draft application."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPkMixin


class CatalogExtractionApplication(Base, UUIDPkMixin):
    __tablename__ = "catalog_extraction_application"
    __table_args__ = (
        UniqueConstraint("revision_id", "revision_version", name="uq_catalog_extraction_application_revision_version"),
        CheckConstraint("revision_version > 1", name="revision_version_positive"),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_model_revision.id", ondelete="RESTRICT"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_document.id", ondelete="RESTRICT"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_extraction_job.id", ondelete="RESTRICT"))
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    revision_version: Mapped[int] = mapped_column(Integer)
    document_sha256: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(48))
    extraction_unit_registry_version: Mapped[str] = mapped_column(String(16))
    before_values: Mapped[dict] = mapped_column(JSONB)
    after_values: Mapped[dict] = mapped_column(JSONB)
    candidates: Mapped[list] = mapped_column(JSONB)
    applied_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
