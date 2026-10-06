"""Datasheet document lifecycle: stage an upload, version it, attach it to a draft revision,
and purge staged uploads nobody attached.

Datasheet versioning (plan v2 section 4.7): a second, different PDF for the same catalog
model becomes `version_number + 1` in the model's document group and points at its
predecessor. Nothing here ever edits an earlier document, its file, or any published
revision's links; database triggers (migration 0032) enforce that independently of this
code. Uploading identical bytes for the same model returns the existing document."""

import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.catalog_documents.malware_scan import MalwareScanner, ScanOutcome, scan_with_policy
from app.application.catalog_documents.pdf_validation import validate_pdf_isolated
from app.core.config import Settings
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.catalog.application_models import CatalogExtractionApplication
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision
from app.domain.catalog.document_models import CatalogDocument, CatalogRevisionDocument
from app.infrastructure.storage import StorageBackend

logger = get_logger(__name__)

_LOCK_CLASS = 0x44434D31  # 'DCM1': namespace for catalog-document object locks


def object_lock_key(sha256: str) -> tuple[int, int]:
    """Advisory-lock key for a stored object. 28 bits of the digest is plenty: a collision only
    makes two unrelated objects wait for each other briefly."""
    return _LOCK_CLASS, int(sha256[:7], 16)


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")


def sanitize_filename(name: str | None) -> str:
    """Display-only: the stored key is always the sha256, never this string."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _FILENAME_UNSAFE.sub("_", base).strip(" .")[:200]
    if not cleaned:
        cleaned = "datasheet.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned


async def _existing_by_sha(db: AsyncSession, catalog_model_id: uuid.UUID, sha256: str) -> CatalogDocument | None:
    return (
        await db.execute(
            select(CatalogDocument)
            .where(CatalogDocument.catalog_model_id == catalog_model_id, CatalogDocument.sha256 == sha256)
            # A purge that selected this row holds it FOR UPDATE until it commits; waiting here
            # means we either see the row gone or keep it alive (the purge then skips it).
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()


async def latest_document_for_model(db: AsyncSession, catalog_model_id: uuid.UUID) -> CatalogDocument | None:
    return (
        await db.execute(
            select(CatalogDocument)
            .where(CatalogDocument.catalog_model_id == catalog_model_id, CatalogDocument.kind == "datasheet")
            .order_by(CatalogDocument.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def stage_document(
    db: AsyncSession,
    *,
    content: bytes,
    original_filename: str | None,
    uploaded_by_user_id: uuid.UUID,
    catalog_model_id: uuid.UUID | None,
    settings: Settings,
    storage: StorageBackend,
    scanner: MalwareScanner,
) -> tuple[CatalogDocument, bool]:
    """Validates, scans, stores and records a PDF. Returns `(document, created)`; `created`
    is False when identical bytes were already recorded for `catalog_model_id`.

    Raises PdfRejected, MalwareDetected, ScannerUnavailable, NotFoundError. Never commits;
    nothing reaches storage until validation and scanning have passed. If the caller's
    later commit fails, the stored object is content-addressed and unreferenced, which the
    purge job's storage sweep tolerates."""
    page_info = await asyncio.to_thread(
        validate_pdf_isolated, content, max_bytes=settings.catalog_document_max_bytes,
        max_pages=settings.catalog_document_max_pages,
    )
    sha256 = hashlib.sha256(content).hexdigest()

    if catalog_model_id is not None:
        if await db.get(CatalogModel, catalog_model_id) is None:
            raise NotFoundError(f"CatalogModel {catalog_model_id} not found.")
        duplicate = await _existing_by_sha(db, catalog_model_id, sha256)
        if duplicate is not None:
            return duplicate, False

    outcome: ScanOutcome = await asyncio.to_thread(
        scan_with_policy, content, mode=settings.catalog_pdf_scan_mode, scanner=scanner
    )

    group_id, version_number, supersedes_id = uuid.uuid4(), 1, None
    if catalog_model_id is not None:
        # Serializes version allocation per model, same discipline as allocate_revision_number().
        await db.execute(select(CatalogModel.id).where(CatalogModel.id == catalog_model_id).with_for_update())
        duplicate = await _existing_by_sha(db, catalog_model_id, sha256)
        if duplicate is not None:
            return duplicate, False
        previous = await latest_document_for_model(db, catalog_model_id)
        if previous is not None:
            group_id, version_number, supersedes_id = previous.document_group_id, previous.version_number + 1, previous.id

    # Serializes this upload against a purge that may be deleting the same content-addressed
    # object; held until the caller commits.
    await db.execute(select(func.pg_advisory_xact_lock(*object_lock_key(sha256))))

    now = datetime.now(UTC)
    document = CatalogDocument(
        document_group_id=group_id, version_number=version_number, supersedes_document_id=supersedes_id,
        catalog_model_id=catalog_model_id, kind="datasheet", storage_key=f"{sha256}.pdf", sha256=sha256,
        original_filename=sanitize_filename(original_filename), mime_type="application/pdf",
        file_size_bytes=len(content), page_count=page_info.page_count, scan_status=outcome.status,
        scan_engine=outcome.engine, scan_detail=outcome.detail, scanned_at=now if outcome.engine else None,
        uploaded_by_user_id=uploaded_by_user_id, uploaded_at=now,
    )
    db.add(document)
    await db.flush()
    await asyncio.to_thread(storage.save, document.storage_key, content)
    return document, True


async def attach_document(
    db: AsyncSession, *, revision: CatalogModelRevision, document: CatalogDocument, user_id: uuid.UUID
) -> CatalogRevisionDocument:
    """Links `document` to a *draft* revision the caller has already locked with
    `lock_draft_revision_for_edit`. A staged document (no model yet) is assigned to the
    revision's model here, once; that is refused when the model already has a datasheet
    lineage, because a document's version position is fixed at upload time."""
    if document.catalog_model_id is None:
        if await latest_document_for_model(db, revision.catalog_model_id) is not None:
            raise ConflictError(
                detail="This model already has datasheet versions. "
                "Upload the PDF against the model so it is recorded as the next version."
            )
        if await _existing_by_sha(db, revision.catalog_model_id, document.sha256) is not None:
            raise ConflictError(detail="An identical PDF is already recorded for this model.")
        document.catalog_model_id = revision.catalog_model_id
        await db.flush()
    elif document.catalog_model_id != revision.catalog_model_id:
        raise ApiError(
            status_code=422, title="Unprocessable Entity", detail="The document belongs to a different catalog model."
        )
    already = (
        await db.execute(
            select(CatalogRevisionDocument.id).where(
                CatalogRevisionDocument.catalog_model_revision_id == revision.id,
                CatalogRevisionDocument.catalog_document_id == document.id,
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ConflictError(detail="The document is already attached to this revision.")
    link = CatalogRevisionDocument(
        catalog_model_revision_id=revision.id, catalog_document_id=document.id, attached_by_user_id=user_id,
        attached_at=datetime.now(UTC),
    )
    db.add(link)
    await db.flush()
    return link


def purge_expired_staged_documents(
    db: Session, *, storage: StorageBackend, retention_days: int, now: datetime | None = None, batch_size: int = 100
) -> int:
    """Row purge followed by the orphan-object sweep. Returns the number of rows deleted."""
    deleted = _purge_expired_rows(db, storage=storage, retention_days=retention_days, now=now, batch_size=batch_size)
    sweep_orphan_objects(db, storage=storage, now=now)
    return deleted


_OBJECT_KEY = re.compile(r"^[0-9a-f]{64}\.pdf$")
ORPHAN_GRACE_SECONDS = 3600


def sweep_orphan_objects(db: Session, *, storage: StorageBackend, now: datetime | None = None) -> int:
    """Deletes stored objects no row references, such as the file a request wrote before its
    commit failed. Each key is re-checked under the same advisory lock uploads hold, so an
    upload that is writing or has just committed the same bytes is never undercut. Objects
    younger than ORPHAN_GRACE_SECONDS and names that are not `<sha256>.pdf` are left alone."""
    reference = (now or datetime.now(UTC)).timestamp()
    old_keys = [
        key for key, modified in storage.iter_keys() if _OBJECT_KEY.match(key) and reference - modified > ORPHAN_GRACE_SECONDS
    ]
    removed = 0
    for key in old_keys:
        lock_connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")  # type: ignore[union-attr]
        try:
            lock_connection.execute(select(func.pg_advisory_lock(*object_lock_key(key.removesuffix(".pdf")))))
            referenced = db.execute(
                select(func.count()).select_from(CatalogDocument).where(CatalogDocument.storage_key == key)
            ).scalar_one()
            db.rollback()
            if not referenced:
                storage.delete(key)
                removed += 1
                logger.info("catalog_document_orphan_object_deleted", storage_key=key)
        finally:
            lock_connection.execute(select(func.pg_advisory_unlock(*object_lock_key(key.removesuffix(".pdf")))))
            lock_connection.close()
    return removed


def _purge_expired_rows(
    db: Session, *, storage: StorageBackend, retention_days: int, now: datetime | None = None, batch_size: int = 100
) -> int:
    """Deletes datasheet rows nobody attached within the retention window (default 14 days),
    then removes their stored objects when no other row shares the content-addressed key.
    A document that is linked to any revision, or is the predecessor of a newer version, is
    never eligible. Returns the number of rows deleted.

    Races handled here:
    - Attach vs purge: each candidate is re-checked for links and successors with a fresh
      statement after its row is locked, and deleted inside a savepoint. The foreign keys
      (`catalog_revision_document.catalog_document_id`, `supersedes_document_id`, both
      RESTRICT) are the backstop: a document linked in the meantime cannot be deleted.
    - Upload vs object deletion: `stage_document` takes a transaction-level advisory lock on
      the object's key before it inserts its row and saves the file. This purge takes the
      same lock (session-level, on a dedicated connection so it survives the commit) for
      every key it may delete, commits, re-counts the rows that reference the key, and only
      then deletes the object. An upload that references the same bytes either committed
      first (the count is non-zero and the object stays) or waits until the object is gone
      and rewrites it."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    linked = exists().where(CatalogRevisionDocument.catalog_document_id == CatalogDocument.id)
    applied = exists().where(CatalogExtractionApplication.document_id == CatalogDocument.id)
    candidates = list(
        db.execute(
            select(CatalogDocument)
            .where(CatalogDocument.uploaded_at < cutoff, ~linked, ~applied)
            .order_by(CatalogDocument.uploaded_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    if not candidates:
        db.rollback()
        return 0

    keys = sorted({document.sha256 for document in candidates})
    lock_connection = db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT")  # type: ignore[union-attr]
    held: list[str] = []
    try:
        for sha in keys:
            lock_connection.execute(select(func.pg_advisory_lock(*object_lock_key(sha))))
            held.append(sha)

        deleted_storage_keys: list[str] = []
        for document in candidates:
            still_linked = db.execute(
                select(func.count()).select_from(CatalogRevisionDocument).where(
                    CatalogRevisionDocument.catalog_document_id == document.id
                )
            ).scalar_one()
            has_successor = db.execute(
                select(func.count()).select_from(CatalogDocument).where(CatalogDocument.supersedes_document_id == document.id)
            ).scalar_one()
            if still_linked or has_successor:
                continue
            try:
                with db.begin_nested():
                    db.delete(document)
                    db.flush()
            except IntegrityError:
                continue
            deleted_storage_keys.append(document.storage_key)
            logger.info(
                "catalog_document_purged", document_id=str(document.id), sha256=document.sha256,
                version_number=document.version_number, catalog_model_id=str(document.catalog_model_id or ""),
            )
        db.commit()

        for storage_key in sorted(set(deleted_storage_keys)):
            still_used = db.execute(
                select(func.count()).select_from(CatalogDocument).where(CatalogDocument.storage_key == storage_key)
            ).scalar_one()
            if not still_used:
                storage.delete(storage_key)
        db.rollback()  # ends the read-only transaction opened by the re-count above
        return len(deleted_storage_keys)
    finally:
        for sha in held:
            lock_connection.execute(select(func.pg_advisory_unlock(*object_lock_key(sha))))
        lock_connection.close()
