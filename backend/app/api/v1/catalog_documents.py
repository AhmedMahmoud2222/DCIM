"""DCIM01 PDF datasheet import, PR-A: upload, version, attach and download catalog datasheet
PDFs (docs/plans/DCIM01_PDF_DATASHEET_IMPORT_PLAN_v2.md section 4.3).

Every mutation requires `catalog:manage` plus Administrator membership, like all catalog
writes. Reads follow the revision-status rule of `GET /catalog/revisions/{id}`. Downloading
the file needs the separate `catalog:document_download` permission and is audited."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog_designer import _request_ids, _revision_read_dependency, _write_child_audit
from app.application.audit_service import write_audit_log
from app.application.catalog_designer_service import lock_draft_revision_for_edit
from app.application.catalog_documents.malware_scan import ClamdScanner, MalwareDetected, MalwareScanner, ScannerUnavailable
from app.application.catalog_documents.pdf_validation import PdfRejected
from app.application.catalog_documents.service import attach_document, sanitize_filename, stage_document
from app.application.concurrency import require_if_match
from app.application.rbac import AuthContext, require_catalog_administrator, require_permission
from app.core.config import Settings, get_settings
from app.core.errors import ApiError, NotFoundError
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision
from app.domain.catalog.document_models import DOWNLOADABLE_SCAN_STATUSES, CatalogDocument, CatalogRevisionDocument
from app.infrastructure.storage import get_document_storage_backend

router = APIRouter(prefix="/catalog", tags=["catalog-documents"])

_REJECTION_STATUS = {"too_large": 413}


def get_malware_scanner(settings: Settings = Depends(get_settings)) -> MalwareScanner:
    """Dependency so tests can substitute a scanner; production talks to the clamd sidecar."""
    return ClamdScanner(settings.clamd_host, settings.clamd_port, settings.clamd_timeout_seconds)


class DocumentRevisionRef(BaseModel):
    revision_id: uuid.UUID
    revision_number: int
    lifecycle_status: str


class DocumentOut(BaseModel):
    id: uuid.UUID
    document_group_id: uuid.UUID
    version_number: int
    supersedes_document_id: uuid.UUID | None
    newer_version_id: uuid.UUID | None
    is_latest_version: bool
    catalog_model_id: uuid.UUID | None
    kind: str
    original_filename: str
    file_size_bytes: int
    page_count: int
    sha256: str
    scan_status: str
    scan_engine: str | None
    uploaded_by_user_id: uuid.UUID
    uploaded_at: datetime
    revisions: list[DocumentRevisionRef] = []


class RevisionDocumentOut(DocumentOut):
    revision_version: int
    created: bool = True


def _document_out(document: CatalogDocument, *, newer: uuid.UUID | None, revisions: list[DocumentRevisionRef]) -> DocumentOut:
    return DocumentOut(
        id=document.id, document_group_id=document.document_group_id, version_number=document.version_number,
        supersedes_document_id=document.supersedes_document_id, newer_version_id=newer, is_latest_version=newer is None,
        catalog_model_id=document.catalog_model_id, kind=document.kind, original_filename=document.original_filename,
        file_size_bytes=document.file_size_bytes, page_count=document.page_count, sha256=document.sha256,
        scan_status=document.scan_status, scan_engine=document.scan_engine,
        uploaded_by_user_id=document.uploaded_by_user_id, uploaded_at=document.uploaded_at, revisions=revisions,
    )


async def _describe(db: AsyncSession, documents: list[CatalogDocument]) -> list[DocumentOut]:
    if not documents:
        return []
    ids = [d.id for d in documents]
    successors = dict(
        (await db.execute(
            select(CatalogDocument.supersedes_document_id, CatalogDocument.id).where(
                CatalogDocument.supersedes_document_id.in_(ids)
            )
        )).all()
    )
    links = (
        await db.execute(
            select(CatalogRevisionDocument.catalog_document_id, CatalogModelRevision.id, CatalogModelRevision.revision_number,
                   CatalogModelRevision.lifecycle_status)
            .join(CatalogModelRevision, CatalogModelRevision.id == CatalogRevisionDocument.catalog_model_revision_id)
            .where(CatalogRevisionDocument.catalog_document_id.in_(ids))
            .order_by(CatalogModelRevision.revision_number)
        )
    ).all()
    refs: dict[uuid.UUID, list[DocumentRevisionRef]] = {}
    for document_id, revision_id, number, status in links:
        refs.setdefault(document_id, []).append(
            DocumentRevisionRef(revision_id=revision_id, revision_number=number, lifecycle_status=status)
        )
    return [_document_out(d, newer=successors.get(d.id), revisions=refs.get(d.id, [])) for d in documents]


async def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    """Reads at most one byte past the cap so an oversized body is rejected without buffering
    the remainder."""
    content = await file.read(settings.catalog_document_max_bytes + 1)
    if len(content) > settings.catalog_document_max_bytes:
        raise ApiError(
            status_code=413, title="Payload Too Large",
            detail=f"Uploaded file exceeds the maximum allowed size of {settings.catalog_document_max_bytes} bytes.",
        )
    return content


async def _stage_or_reject(
    db: AsyncSession, request: Request, ctx: AuthContext, *, file: UploadFile, catalog_model_id: uuid.UUID | None,
    settings: Settings, scanner: MalwareScanner,
) -> tuple[CatalogDocument, bool]:
    content = await _read_upload(file, settings)
    actor_id = ctx.user.id  # read before any rollback expires the ORM object
    try:
        return await stage_document(
            db, content=content, original_filename=file.filename, uploaded_by_user_id=ctx.user.id,
            catalog_model_id=catalog_model_id, settings=settings, storage=get_document_storage_backend(), scanner=scanner,
        )
    except PdfRejected as exc:
        raise ApiError(
            status_code=_REJECTION_STATUS.get(exc.code, 422),
            title="Payload Too Large" if exc.code == "too_large" else "Unprocessable Entity", detail=exc.reason,
        ) from exc
    except MalwareDetected as exc:
        # Discard anything this request staged (for example the revision version bump taken
        # by the attach flow) so only the security audit record is committed.
        await db.rollback()
        request_id, correlation_id = _request_ids(request)
        await write_audit_log(
            db, actor_user_id=actor_id, action="catalog.document.upload_rejected_malware", entity_type="catalog_document",
            entity_id=None, request_id=request_id, correlation_id=correlation_id,
            after={"filename": sanitize_filename(file.filename), "signature": exc.signature[:128]},
        )
        await db.commit()
        raise ApiError(
            status_code=422, title="Unprocessable Entity", detail="The file was rejected by malware scanning."
        ) from exc
    except ScannerUnavailable as exc:
        raise ApiError(
            status_code=503, title="Service Unavailable",
            detail="Malware scanning is unavailable, so the upload was rejected. Try again later.",
        ) from exc


async def _audit_upload(db: AsyncSession, request: Request, ctx: AuthContext, document: CatalogDocument) -> None:
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.document.upload", entity_type="catalog_document",
        entity_id=document.id, request_id=request_id, correlation_id=correlation_id,
        after={
            "filename": document.original_filename, "sha256": document.sha256, "size_bytes": document.file_size_bytes,
            "page_count": document.page_count, "version_number": document.version_number,
            "catalog_model_id": str(document.catalog_model_id) if document.catalog_model_id else None,
            "scan_status": document.scan_status,
        },
    )


async def _load_document(db: AsyncSession, document_id: uuid.UUID) -> CatalogDocument:
    document = await db.get(CatalogDocument, document_id)
    if document is None:
        raise NotFoundError(f"CatalogDocument {document_id} not found.")
    return document


# ------------------------------------------------------------------ Stage / list / read


@router.post("/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    request: Request,
    response: Response,
    catalog_model_id: uuid.UUID | None = Query(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    scanner: MalwareScanner = Depends(get_malware_scanner),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> DocumentOut:
    """Stages a datasheet. With `catalog_model_id`, a different PDF becomes the model's next
    version and identical bytes return the existing document (HTTP 200). Without it, the
    upload stays staged (model creation flow) until attached, and is purged after the
    retention window if never attached."""
    document, created = await _stage_or_reject(
        db, request, ctx, file=file, catalog_model_id=catalog_model_id, settings=settings, scanner=scanner
    )
    if created:
        await _audit_upload(db, request, ctx, document)
    await db.commit()
    response.status_code = 201 if created else 200
    return (await _describe(db, [document]))[0]


@router.get("/models/{model_id}/documents", response_model=list[DocumentOut])
async def list_model_documents(
    model_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:read_draft")),
) -> list[DocumentOut]:
    """Full version history of a model's datasheet, newest first."""
    if await db.get(CatalogModel, model_id) is None:
        raise NotFoundError(f"CatalogModel {model_id} not found.")
    documents = list(
        (await db.execute(
            select(CatalogDocument)
            .where(CatalogDocument.catalog_model_id == model_id)
            .order_by(CatalogDocument.version_number.desc())
        )).scalars()
    )
    return await _describe(db, documents)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:read_draft")),
) -> DocumentOut:
    return (await _describe(db, [await _load_document(db, document_id)]))[0]


@router.get("/documents/{document_id}/file")
async def download_document(
    document_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_permission("catalog:document_download")),
) -> Response:
    """Serves the original bytes as an attachment. A document that is not linked to any
    published or retired revision is still draft-stage material, so it additionally needs
    `catalog:read_draft`. Files that did not pass scanning are never served."""
    document = await _load_document(db, document_id)
    if document.scan_status not in DOWNLOADABLE_SCAN_STATUSES:
        raise ApiError(status_code=409, title="Conflict", detail="This document did not pass malware scanning.")
    if document.scan_status == "skipped" and get_settings().catalog_pdf_scan_mode == "required":
        raise ApiError(status_code=409, title="Conflict", detail="This document was stored without a malware scan.")
    non_draft = (
        await db.execute(
            select(CatalogRevisionDocument.id)
            .join(CatalogModelRevision, CatalogModelRevision.id == CatalogRevisionDocument.catalog_model_revision_id)
            .where(CatalogRevisionDocument.catalog_document_id == document_id, CatalogModelRevision.lifecycle_status != "draft")
            .limit(1)
        )
    ).scalar_one_or_none()
    if non_draft is None and not ctx.has_permission("catalog:read_draft"):
        raise ApiError(status_code=403, title="Forbidden", detail="Missing required permission: catalog:read_draft")
    try:
        content = get_document_storage_backend().read(document.storage_key)
    except FileNotFoundError as exc:
        raise NotFoundError("The stored file for this document is missing.") from exc

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="catalog.document.download", entity_type="catalog_document",
        entity_id=document.id, request_id=request_id, correlation_id=correlation_id,
        after={"sha256": document.sha256, "version_number": document.version_number},
    )
    await db.commit()
    return Response(
        content=content, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{document.original_filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


# ------------------------------------------------------------------ Revision attachment


@router.get("/revisions/{revision_id}/documents", response_model=list[DocumentOut])
async def list_revision_documents(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db), loaded=Depends(_revision_read_dependency)
) -> list[DocumentOut]:
    documents = list(
        (await db.execute(
            select(CatalogDocument)
            .join(CatalogRevisionDocument, CatalogRevisionDocument.catalog_document_id == CatalogDocument.id)
            .where(CatalogRevisionDocument.catalog_model_revision_id == revision_id)
            .order_by(CatalogDocument.version_number.desc())
        )).scalars()
    )
    return await _describe(db, documents)


async def _attach_response(
    db: AsyncSession, document: CatalogDocument, revision: CatalogModelRevision, *, created: bool
) -> RevisionDocumentOut:
    described = (await _describe(db, [document]))[0]
    return RevisionDocumentOut(**described.model_dump(), revision_version=revision.version, created=created)


@router.post("/revisions/{revision_id}/documents", response_model=RevisionDocumentOut, status_code=201)
async def upload_and_attach_document(
    revision_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    scanner: MalwareScanner = Depends(get_malware_scanner),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> RevisionDocumentOut:
    """Upload and attach in one request. The revision must be a draft; a published
    revision is immutable, so a newer datasheet is attached to a new draft (clone) instead."""
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    document, created = await _stage_or_reject(
        db, request, ctx, file=file, catalog_model_id=revision.catalog_model_id, settings=settings, scanner=scanner
    )
    if created:
        await _audit_upload(db, request, ctx, document)
    await attach_document(db, revision=revision, document=document, user_id=ctx.user.id)
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"document_id": str(document.id), "version_number": document.version_number, "attached": True},
    )
    await db.commit()
    return await _attach_response(db, document, revision, created=created)


@router.post("/revisions/{revision_id}/documents/{document_id}", response_model=RevisionDocumentOut, status_code=201)
async def attach_existing_document(
    revision_id: uuid.UUID, document_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> RevisionDocumentOut:
    revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    document = await _load_document(db, document_id)
    await attach_document(db, revision=revision, document=document, user_id=ctx.user.id)
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.update_draft",
        after={"document_id": str(document.id), "version_number": document.version_number, "attached": True},
    )
    await db.commit()
    return await _attach_response(db, document, revision, created=False)


@router.delete("/revisions/{revision_id}/documents/{document_id}", status_code=204)
async def detach_document(
    revision_id: uuid.UUID, document_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx: AuthContext = Depends(require_catalog_administrator("catalog:manage")),
) -> Response:
    """Detaches from a draft only. The document row and file stay (version history); an
    unattached document is purged after the staging retention window."""
    await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=if_match_version)
    link = (
        await db.execute(
            select(CatalogRevisionDocument).where(
                CatalogRevisionDocument.catalog_model_revision_id == revision_id,
                CatalogRevisionDocument.catalog_document_id == document_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFoundError(f"Document {document_id} is not attached to revision {revision_id}.")
    await db.delete(link)
    await _write_child_audit(
        db, request, ctx, revision_id, action="catalog.revision.component_remove",
        before={"document_id": str(document_id), "attached": True},
    )
    await db.commit()
    return Response(status_code=204)

