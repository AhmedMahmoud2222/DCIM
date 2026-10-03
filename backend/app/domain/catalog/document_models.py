"""DCIM01 PDF datasheet import, PR-A: document storage records
(docs/plans/DCIM01_PDF_DATASHEET_IMPORT_PLAN_v2.md sections 4.2 and 4.7).

`CatalogDocument` is one immutable uploaded file. A datasheet the manufacturer later revises
is a *new* row in the same `document_group_id` with `version_number + 1`; the old row, its
file and its links are never touched. `CatalogRevisionDocument` links a document to a
revision. Migration 0032 attaches `fn_reject_write_on_non_draft_revision()` to it, so a
published or retired revision's links are immutable at the database level and a new
datasheet version can never change an existing published revision or the assets
instantiated from it."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

DOCUMENT_KINDS = ("datasheet",)
SCAN_STATUSES = ("clean", "skipped", "infected", "error")
DOWNLOADABLE_SCAN_STATUSES = ("clean", "skipped")


class CatalogDocument(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "catalog_document"
    __table_args__ = (
        UniqueConstraint("document_group_id", "version_number", name="uq_catalog_document_group_version"),
        CheckConstraint(f"kind IN {DOCUMENT_KINDS!r}", name="kind_allowed"),
        CheckConstraint(f"scan_status IN {SCAN_STATUSES!r}", name="scan_status_allowed"),
        CheckConstraint("mime_type = 'application/pdf'", name="mime_type_pdf"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("page_count >= 1", name="page_count_positive"),
        CheckConstraint("file_size_bytes > 0", name="file_size_positive"),
        CheckConstraint("char_length(sha256) = 64", name="sha256_length"),
        CheckConstraint("supersedes_document_id IS NULL OR version_number > 1", name="supersedes_requires_later_version"),
    )

    document_group_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog_document.id", ondelete="RESTRICT"), unique=True
    )
    catalog_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog_model.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="datasheet", server_default="datasheet")
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False, default="application/pdf")
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_status: Mapped[str] = mapped_column(String(16), nullable=False)
    scan_engine: Mapped[str | None] = mapped_column(String(64))
    scan_detail: Mapped[str | None] = mapped_column(String(128))
    scanned_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CatalogRevisionDocument(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "catalog_revision_document"
    __table_args__ = (
        UniqueConstraint("catalog_model_revision_id", "catalog_document_id", name="uq_catalog_revision_document_pair"),
    )

    catalog_model_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_model_revision.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_document.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attached_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False)
    attached_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
