"""DCIM01 PDF datasheet import, PR-A: upload, validation, scanning, versioning, attachment,
download permissions, and the guarantee that a newer datasheet never changes a published
revision. End to end through the real HTTP boundary."""

import uuid

import pytest
from sqlalchemy import select, text

from app.domain.audit.models import AuditLog
from tests.api._document_helpers import (
    make_draft,
    make_manufacturer,
    make_model,
    make_pdf,
    make_published_rack_revision,
    pdf_encrypted,
    pdf_files,
    pdf_with_attachment,
    pdf_with_javascript,
)

ALL_ROLES = ["Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer"]


async def _setup_model(client, headers, *, category: str = "rack") -> str:
    return await make_model(client, headers, await make_manufacturer(client, headers), category=category)


async def _upload(client, headers, content: bytes, *, model_id: str | None = None, name: str = "ds.pdf"):
    params = {"catalog_model_id": model_id} if model_id else {}
    return await client.post("/api/v1/catalog/documents", params=params, files=pdf_files(content, name), headers=headers)


# ------------------------------------------------------------------------- Upload and validation


async def test_admin_uploads_valid_pdf_and_metadata_is_recorded(client, auth_headers, scanner):
    headers = await auth_headers("Administrator")
    content = make_pdf("Vendor X 42U", pages=3)
    resp = await _upload(client, headers, content, name="../../etc/Vendor X datasheet.pdf")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version_number"] == 1
    assert body["page_count"] == 3
    assert body["file_size_bytes"] == len(content)
    assert body["scan_status"] == "clean"
    assert body["catalog_model_id"] is None
    assert body["is_latest_version"] is True
    assert "/" not in body["original_filename"] and ".." not in body["original_filename"]
    assert scanner.scanned == [len(content)]


@pytest.mark.parametrize(
    ("content", "status"),
    [
        (b"", 422),
        (b"not a pdf at all", 422),
        (pdf_with_javascript(), 422),
        (pdf_with_attachment(), 422),
        (pdf_encrypted(), 422),
        (make_pdf()[:-150], 422),
    ],
)
async def test_invalid_or_hostile_pdfs_are_rejected_and_not_stored(client, auth_headers, scanner, content, status):
    headers = await auth_headers("Administrator")
    resp = await _upload(client, headers, content)
    assert resp.status_code == status, resp.text
    listing = await client.get("/api/v1/catalog/documents/" + str(uuid.uuid4()), headers=headers)
    assert listing.status_code == 404
    assert scanner.scanned == []  # validation runs before scanning


async def test_extension_and_declared_type_are_not_trusted(client, auth_headers, scanner):
    headers = await auth_headers("Administrator")
    resp = await client.post(
        "/api/v1/catalog/documents", files={"file": ("real.pdf", b"PK\x03\x04zipbytes", "application/pdf")}, headers=headers
    )
    assert resp.status_code == 422


async def test_oversized_upload_returns_413(client, auth_headers, scanner, settings_override):
    settings_override(catalog_document_max_bytes=2000)
    headers = await auth_headers("Administrator")
    resp = await _upload(client, headers, make_pdf("x", pages=1) + b"0" * 5000)
    assert resp.status_code == 413


async def test_page_cap_is_configurable(client, auth_headers, scanner, settings_override):
    settings_override(catalog_document_max_pages=2)
    headers = await auth_headers("Administrator")
    assert (await _upload(client, headers, make_pdf(pages=3))).status_code == 422
    assert (await _upload(client, headers, make_pdf("ok", pages=2))).status_code == 201


# ---------------------------------------------------------------------------- Malware scanning


async def test_infected_upload_is_rejected_audited_and_never_stored(client, auth_headers, scanner, db_session):
    scanner.mode = "infected"
    headers = await auth_headers("Administrator")
    resp = await _upload(client, headers, make_pdf("unique"))
    assert resp.status_code == 422
    assert "malware" in resp.json()["detail"].lower()
    count = (await db_session.execute(text("SELECT count(*) FROM catalog_document"))).scalar_one()
    assert count == 0
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "catalog.document.upload_rejected_malware" in actions


async def test_scanner_down_fails_closed_in_required_mode(client, auth_headers, scanner, settings_override):
    scanner.mode = "down"
    settings_override(catalog_pdf_scan_mode="required")
    headers = await auth_headers("Administrator")
    assert (await _upload(client, headers, make_pdf("unique"))).status_code == 503


async def test_scanner_down_records_skipped_in_optional_mode(client, auth_headers, scanner, settings_override):
    scanner.mode = "down"
    settings_override(catalog_pdf_scan_mode="optional")
    headers = await auth_headers("Administrator")
    resp = await _upload(client, headers, make_pdf("unique"))
    assert resp.status_code == 201
    assert resp.json()["scan_status"] == "skipped"


async def test_scan_mode_off_skips_scanner(client, auth_headers, scanner, settings_override):
    settings_override(catalog_pdf_scan_mode="off")
    headers = await auth_headers("Administrator")
    assert (await _upload(client, headers, make_pdf("unique"))).json()["scan_status"] == "skipped"
    assert scanner.scanned == []


def test_production_refuses_non_required_scan_mode():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            environment="production", catalog_pdf_scan_mode="optional", database_url="postgresql+asyncpg://u:p@h/d",
            redis_url="redis://h/0", jwt_secret_key="x" * 32, credential_encryption_key="y" * 32,
        )


# ---------------------------------------------------------------------------- Access control


@pytest.mark.parametrize("role", ["DCIM Manager", "Engineer", "Operator", "Viewer"])
async def test_only_administrators_can_upload_or_attach(client, auth_headers, scanner, role):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    headers = await auth_headers(role)
    assert (await _upload(client, headers, make_pdf("unique"))).status_code == 403
    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 403


async def test_unauthenticated_requests_are_rejected(client, scanner):
    assert (await client.post("/api/v1/catalog/documents", files=pdf_files(make_pdf()))).status_code == 401
    assert (await client.get(f"/api/v1/catalog/documents/{uuid.uuid4()}/file")).status_code == 401


async def _published_revision_with_document(client, admin, content: bytes) -> tuple[dict, dict]:
    model_id = await _setup_model(client, admin)
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(content),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    assert attach.status_code == 201, attach.text
    patched = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json={"dimension_unit": "mm", "width_value": 482.6, "height_value": 1000, "depth_value": 1000, "rack_unit_height": 42,
              "weight_unit": "kg", "weight_value": 100},
        headers={**admin, "If-Match": str(attach.json()["revision_version"])},
    )
    assert patched.status_code == 200, patched.text
    assert (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).status_code == 200
    return draft, attach.json()


@pytest.mark.parametrize(
    ("role", "can_download"),
    [("Administrator", True), ("DCIM Manager", True), ("Engineer", True), ("Operator", False), ("Viewer", False)],
)
async def test_download_permission_matrix_for_published_datasheets(client, auth_headers, scanner, role, can_download):
    admin = await auth_headers("Administrator")
    _revision, document = await _published_revision_with_document(client, admin, make_pdf("unique"))
    resp = await client.get(f"/api/v1/catalog/documents/{document['id']}/file", headers=await auth_headers(role))
    assert resp.status_code == (200 if can_download else 403), resp.text


@pytest.mark.parametrize("role", ["DCIM Manager", "Engineer", "Operator", "Viewer"])
async def test_staged_and_draft_datasheets_are_administrator_only(client, auth_headers, scanner, role):
    admin = await auth_headers("Administrator")
    staged = (await _upload(client, admin, make_pdf("unique"))).json()
    assert (await client.get(f"/api/v1/catalog/documents/{staged['id']}/file", headers=await auth_headers(role))).status_code == 403
    assert (await client.get(f"/api/v1/catalog/documents/{staged['id']}", headers=await auth_headers(role))).status_code == 403


async def test_download_headers_body_and_audit(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    content = make_pdf("unique")
    document_id = (await _upload(client, admin, content, name="Spec Sheet (v2).pdf")).json()["id"]
    resp = await client.get(f"/api/v1/catalog/documents/{document_id}/file", headers=admin)
    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in resp.headers["cache-control"]
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "catalog.document.download" in actions


# ------------------------------------------------------------------------- Versions and history


async def test_new_datasheet_becomes_next_version_and_identical_bytes_dedupe(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    v1 = (await _upload(client, admin, make_pdf("revision A"), model_id=model_id)).json()
    assert v1["version_number"] == 1

    again = await _upload(client, admin, make_pdf("revision A") if False else await _bytes_of(client, admin, v1["id"]), model_id=model_id)
    assert again.status_code == 200
    assert again.json()["id"] == v1["id"]

    v2_resp = await _upload(client, admin, make_pdf("revision B"), model_id=model_id)
    assert v2_resp.status_code == 201
    v2 = v2_resp.json()
    assert v2["version_number"] == 2
    assert v2["supersedes_document_id"] == v1["id"]
    assert v2["document_group_id"] == v1["document_group_id"]

    history = (await client.get(f"/api/v1/catalog/models/{model_id}/documents", headers=admin)).json()
    assert [d["version_number"] for d in history] == [2, 1]
    assert history[1]["newer_version_id"] == v2["id"] and history[1]["is_latest_version"] is False
    assert history[0]["is_latest_version"] is True


async def _bytes_of(client, headers, document_id: str) -> bytes:
    return (await client.get(f"/api/v1/catalog/documents/{document_id}/file", headers=headers)).content


async def test_new_datasheet_version_does_not_change_published_revision_or_its_links(client, auth_headers, scanner, db_session):
    """Requirement: a manufacturer revising a datasheet after publication creates a new
    document version and never alters an existing published revision."""
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(make_pdf("original")),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    assert attach.status_code == 201, attach.text
    doc_v1 = attach.json()
    fields = {
        "dimension_unit": "in", "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4,
        "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220.0,
    }
    patched = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}", json=fields,
        headers={**admin, "If-Match": str(attach.json()["revision_version"])},
    )
    assert patched.status_code == 200, patched.text
    published = (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).json()
    before = (await client.get(f"/api/v1/catalog/revisions/{draft['id']}", headers=admin)).json()

    v2 = (await _upload(client, admin, make_pdf("manufacturer corrected the sheet"), model_id=model_id)).json()
    assert v2["version_number"] == 2

    # The published revision still links exactly the original document, and is unchanged.
    linked = (await client.get(f"/api/v1/catalog/revisions/{draft['id']}/documents", headers=admin)).json()
    assert [d["id"] for d in linked] == [doc_v1["id"]]
    assert linked[0]["newer_version_id"] == v2["id"]  # advisory only
    after = (await client.get(f"/api/v1/catalog/revisions/{draft['id']}", headers=admin)).json()
    assert after == before
    assert published["lifecycle_status"] == "published"

    # Attaching the new version to the published revision is refused (immutable)...
    refused = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents/{v2['id']}",
        headers={**admin, "If-Match": str(after["version"])},
    )
    assert refused.status_code == 409
    # ...and detaching the original is refused too.
    refused = await client.delete(
        f"/api/v1/catalog/revisions/{draft['id']}/documents/{doc_v1['id']}", headers={**admin, "If-Match": str(after["version"])}
    )
    assert refused.status_code == 409

    # The new version attaches to a cloned draft; the old document remains on the published revision.
    clone = await client.post(
        f"/api/v1/catalog/models/{model_id}/revisions/clone", params={"from_revision_id": draft["id"]}, headers=admin
    )
    assert clone.status_code == 201, clone.text
    clone_body = clone.json()
    cloned_docs = (await client.get(f"/api/v1/catalog/revisions/{clone_body['id']}/documents", headers=admin)).json()
    assert [d["id"] for d in cloned_docs] == [doc_v1["id"]]  # clone carries the link
    attached = await client.post(
        f"/api/v1/catalog/revisions/{clone_body['id']}/documents/{v2['id']}",
        headers={**admin, "If-Match": str(clone_body["version"])},
    )
    assert attached.status_code == 201, attached.text
    original_docs = (await client.get(f"/api/v1/catalog/revisions/{draft['id']}/documents", headers=admin)).json()
    assert [d["id"] for d in original_docs] == [doc_v1["id"]]


# ------------------------------------------------------------------------------- Attachment


async def test_attach_requires_matching_model_and_current_version(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_a = await _setup_model(client, admin)
    model_b = await _setup_model(client, admin)
    doc_b = (await _upload(client, admin, make_pdf("unique"), model_id=model_b)).json()
    revision_a = await make_draft(client, admin, model_a)

    wrong_model = await client.post(
        f"/api/v1/catalog/revisions/{revision_a['id']}/documents/{doc_b['id']}",
        headers={**admin, "If-Match": str(revision_a["version"])},
    )
    assert wrong_model.status_code == 422

    staged = (await _upload(client, admin, make_pdf("unique"))).json()
    stale = await client.post(
        f"/api/v1/catalog/revisions/{revision_a['id']}/documents/{staged['id']}", headers={**admin, "If-Match": "99"}
    )
    assert stale.status_code == 409
    missing_header = await client.post(f"/api/v1/catalog/revisions/{revision_a['id']}/documents/{staged['id']}", headers=admin)
    assert missing_header.status_code == 428


async def test_staged_document_is_assigned_to_model_on_attach_then_versions_continue(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    staged = (await _upload(client, admin, make_pdf("unique"))).json()
    attached = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents/{staged['id']}",
        headers={**admin, "If-Match": str(revision["version"])},
    )
    assert attached.status_code == 201, attached.text
    assert attached.json()["catalog_model_id"] == model_id
    assert attached.json()["revision_version"] == revision["version"] + 1
    next_version = (await _upload(client, admin, make_pdf("unique"), model_id=model_id)).json()
    assert next_version["version_number"] == 2

    # A staged document cannot join a model that already has a datasheet lineage.
    other_staged = (await _upload(client, admin, make_pdf("unique"))).json()
    second_revision_version = attached.json()["revision_version"]
    conflict = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents/{other_staged['id']}",
        headers={**admin, "If-Match": str(second_revision_version)},
    )
    assert conflict.status_code == 409


async def test_attach_twice_conflicts_and_detach_works_on_draft(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    first = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(revision["version"])},
    )
    version = first.json()["revision_version"]
    document_id = first.json()["id"]
    again = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents/{document_id}", headers={**admin, "If-Match": str(version)}
    )
    assert again.status_code == 409
    # The test client shares one session across requests; production opens one per request,
    # so the failed request's uncommitted version bump is discarded there. Mirror that here.
    await db_session.rollback()
    detached = await client.delete(
        f"/api/v1/catalog/revisions/{revision['id']}/documents/{document_id}", headers={**admin, "If-Match": str(version)}
    )
    assert detached.status_code == 204
    assert (await client.get(f"/api/v1/catalog/revisions/{revision['id']}/documents", headers=admin)).json() == []
    # The document itself survives detaching (history is preserved).
    assert (await client.get(f"/api/v1/catalog/documents/{document_id}", headers=admin)).status_code == 200


async def test_published_revision_documents_are_readable_with_catalog_read(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json={"dimension_unit": "mm", "width_value": 482.6, "height_value": 1000, "depth_value": 1000, "rack_unit_height": 42,
              "weight_unit": "kg", "weight_value": 100},
        headers={**admin, "If-Match": str(attach.json()["revision_version"])},
    )
    assert (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).status_code == 200
    viewer = await auth_headers("Viewer")
    listing = await client.get(f"/api/v1/catalog/revisions/{draft['id']}/documents", headers=viewer)
    assert listing.status_code == 200 and len(listing.json()) == 1
    # A viewer still cannot download (no document_download) and cannot see draft-stage documents.
    assert (await client.get(f"/api/v1/catalog/documents/{attach.json()['id']}/file", headers=viewer)).status_code == 403
    assert (await client.get(f"/api/v1/catalog/documents/{attach.json()['id']}", headers=viewer)).status_code == 403


async def test_draft_revision_documents_need_read_draft(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    draft = await make_draft(client, admin, model_id)
    assert (await client.get(f"/api/v1/catalog/revisions/{draft['id']}/documents", headers=await auth_headers("Viewer"))).status_code == 403


async def test_upload_and_attach_writes_audit_and_outbox(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 201
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "catalog.document.upload" in actions
    assert "catalog.revision.update_draft" in actions
    outbox = (await db_session.execute(text("SELECT event_type FROM outbox_event"))).scalars().all()
    assert "CatalogModelRevisionDraftUpdated" in outbox


async def test_published_revision_publish_output_is_unchanged_by_documents(client, auth_headers, scanner, db_session):
    """Regression: a revision with an attached datasheet publishes the same legacy bridge as one without."""
    admin = await auth_headers("Administrator")
    plain = await make_published_rack_revision(client, admin, await _setup_model(client, admin))
    model_id = await _setup_model(client, admin)
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    patched = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json={"dimension_unit": "in", "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4, "rack_unit_height": 42,
              "weight_unit": "lb", "weight_value": 220.0},
        headers={**admin, "If-Match": str(attach.json()["revision_version"])},
    )
    assert patched.status_code == 200
    with_doc = (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).json()
    rows = (
        await db_session.execute(text("SELECT height_u, width_mm, depth_mm, weight_capacity_kg FROM rack_model_revision ORDER BY created_at"))
    ).all()
    assert len(rows) == 2 and rows[0] == rows[1]
    assert plain["lifecycle_status"] == with_doc["lifecycle_status"] == "published"


# ------------------------------------------------------------------ Security inspection additions


@pytest.mark.parametrize("role", ["DCIM Manager", "Engineer", "Operator", "Viewer"])
async def test_non_administrators_cannot_attach_detach_or_use_draft_endpoints(client, auth_headers, scanner, role):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    attached = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(revision["version"])},
    )
    document_id, version = attached.json()["id"], str(attached.json()["revision_version"])
    headers = await auth_headers(role)
    assert (await client.post(f"/api/v1/catalog/revisions/{revision['id']}/documents/{document_id}", headers={**headers, "If-Match": version})).status_code == 403
    assert (await client.delete(f"/api/v1/catalog/revisions/{revision['id']}/documents/{document_id}", headers={**headers, "If-Match": version})).status_code == 403
    assert (await client.get(f"/api/v1/catalog/models/{model_id}/documents", headers=headers)).status_code == 403
    # Nothing changed.
    assert len((await client.get(f"/api/v1/catalog/revisions/{revision['id']}/documents", headers=admin)).json()) == 1


async def test_detach_through_the_wrong_revision_is_not_found(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision_a = await make_draft(client, admin, model_id)
    attached = await client.post(
        f"/api/v1/catalog/revisions/{revision_a['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(revision_a["version"])},
    )
    document_id = attached.json()["id"]
    other_model = await _setup_model(client, admin)
    revision_b = await make_draft(client, admin, other_model)
    resp = await client.delete(
        f"/api/v1/catalog/revisions/{revision_b['id']}/documents/{document_id}",
        headers={**admin, "If-Match": str(revision_b["version"])},
    )
    assert resp.status_code == 404
    assert len((await client.get(f"/api/v1/catalog/revisions/{revision_a['id']}/documents", headers=admin)).json()) == 1


@pytest.mark.parametrize(
    "hostile_name",
    [
        '../../../etc/passwd',
        'a"; filename="evil.exe',
        "report\r\nX-Injected: yes.pdf",
        "C:\\Windows\\system32\\cmd.pdf",
        "résumé datasheet ✓.pdf",
        "." * 300 + ".pdf",
    ],
)
async def test_hostile_filenames_are_sanitized_in_storage_record_and_download_header(client, auth_headers, scanner, hostile_name):
    admin = await auth_headers("Administrator")
    resp = await client.post(
        "/api/v1/catalog/documents", files={"file": (hostile_name, make_pdf("unique"), "application/pdf")}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    stored = body["original_filename"]
    assert stored.endswith(".pdf") and len(stored) <= 255
    assert all(ch not in stored for ch in '/\\"\r\n;')
    download = await client.get(f"/api/v1/catalog/documents/{body['id']}/file", headers=admin)
    disposition = download.headers["content-disposition"]
    assert disposition == f'attachment; filename="{stored}"'
    assert "\r" not in disposition and "\n" not in disposition
    assert "X-Injected" not in download.headers


async def test_identical_bytes_share_one_physical_object_across_models(client, auth_headers, scanner):
    from pathlib import Path

    from app.core.config import get_settings

    admin = await auth_headers("Administrator")
    model_a = await _setup_model(client, admin)
    model_b = await _setup_model(client, admin)
    content = make_pdf("shared bytes unique")
    doc_a = (await _upload(client, admin, content, model_id=model_a)).json()
    doc_b = (await _upload(client, admin, content, model_id=model_b)).json()
    assert doc_a["id"] != doc_b["id"] and doc_a["sha256"] == doc_b["sha256"]
    root = Path(get_settings().catalog_documents_storage_root)
    assert [p.name for p in root.glob(f"{doc_a['sha256']}*")] == [f"{doc_a['sha256']}.pdf"]
    assert (await client.get(f"/api/v1/catalog/documents/{doc_b['id']}/file", headers=admin)).content == content


async def test_lifecycle_upload_scan_stage_attach_publish_then_immutable(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)

    staged = (await _upload(client, admin, make_pdf("lifecycle unique"))).json()  # upload -> scan -> stage
    assert staged["scan_status"] == "clean" and staged["revisions"] == [] and scanner.scanned

    attached = await client.post(  # attach
        f"/api/v1/catalog/revisions/{revision['id']}/documents/{staged['id']}",
        headers={**admin, "If-Match": str(revision["version"])},
    )
    assert attached.status_code == 201
    assert attached.json()["revisions"] == [
        {"revision_id": revision["id"], "revision_number": 1, "lifecycle_status": "draft"}
    ]
    patched = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={"dimension_unit": "mm", "width_value": 482.6, "height_value": 1000, "depth_value": 1000, "rack_unit_height": 42,
              "weight_unit": "kg", "weight_value": 100},
        headers={**admin, "If-Match": str(attached.json()["revision_version"])},
    )
    assert patched.status_code == 200
    assert (await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=admin)).status_code == 200  # publish

    # immutable: cannot detach, cannot attach another document, and the DB trigger agrees.
    other = (await _upload(client, admin, make_pdf("lifecycle other unique"), model_id=model_id)).json()
    current = (await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=admin)).json()["version"]
    for method, path in (
        ("delete", f"/api/v1/catalog/revisions/{revision['id']}/documents/{staged['id']}"),
        ("post", f"/api/v1/catalog/revisions/{revision['id']}/documents/{other['id']}"),
    ):
        resp = await getattr(client, method)(path, headers={**admin, "If-Match": str(current)})
        assert resp.status_code == 409, (method, resp.text)
    with pytest.raises(Exception, match="not a draft|immutable"):
        await db_session.execute(
            text("DELETE FROM catalog_revision_document WHERE catalog_model_revision_id = :r"), {"r": revision["id"]}
        )
    await db_session.rollback()
    assert len((await client.get(f"/api/v1/catalog/revisions/{revision['id']}/documents", headers=admin)).json()) == 1


async def test_lifecycle_upload_unattached_expiry_delete(client, auth_headers, scanner, db_session):
    from datetime import UTC, datetime, timedelta

    from app.application.catalog_documents.service import purge_expired_staged_documents
    from app.db.sync_session import get_sync_db
    from app.infrastructure.storage import get_document_storage_backend

    admin = await auth_headers("Administrator")
    document = (await _upload(client, admin, make_pdf("expiring unique"))).json()
    storage = get_document_storage_backend()
    key = f"{document['sha256']}.pdf"
    assert storage.exists(key)

    with get_sync_db() as sync_db:  # inside the window: kept
        assert purge_expired_staged_documents(sync_db, storage=storage, retention_days=14) == 0
    assert storage.exists(key)
    with get_sync_db() as sync_db:  # 15 days later: deleted, row and object
        assert purge_expired_staged_documents(
            sync_db, storage=storage, retention_days=14, now=datetime.now(UTC) + timedelta(days=15)
        ) == 1
    assert not storage.exists(key)
    assert (await client.get(f"/api/v1/catalog/documents/{document['id']}", headers=admin)).status_code == 404
    assert (await client.get(f"/api/v1/catalog/documents/{document['id']}/file", headers=admin)).status_code == 404


async def test_new_datasheet_version_leaves_instantiated_equipment_unchanged(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin, category="equipment")
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(make_pdf("server sheet v1 unique")),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    version = attach.json()["revision_version"]
    fields = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json={"dimension_unit": "mm", "width_value": 440, "height_value": 88.9, "depth_value": 600, "weight_unit": "kg",
              "weight_value": 10, "rack_unit_height": 2, "supported_placement_types": ["rack_mounted"]},
        headers={**admin, "If-Match": str(version)},
    )
    version = fields.json()["version"]
    port = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/network-ports",
        json={"stable_key": "eth0", "display_name": "eth0", "media_type": "copper", "supported_speeds_mbps": [1000],
              "connector_type": "rj45", "side": "rear"},
        headers={**admin, "If-Match": str(version)},
    )
    version = port.json()["revision_version"]
    psu = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/power-supplies",
        json={"stable_key": "psu1", "label": "PSU", "quantity": 2, "connector_type": "C14"},
        headers={**admin, "If-Match": str(version)},
    )
    assert psu.status_code == 201, psu.text
    assert (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).status_code == 200

    equipment = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": draft["id"], "hostname": "srv-doc"},
        headers=admin,
    )
    assert equipment.status_code == 201, equipment.text
    equipment_id = equipment.json()["id"]

    async def _snapshot():
        detail = (await client.get(f"/api/v1/equipment/{equipment_id}", headers=admin)).json()
        ports = (await client.get(f"/api/v1/equipment/{equipment_id}/ports", headers=admin)).json()
        revision = (await client.get(f"/api/v1/catalog/revisions/{draft['id']}", headers=admin)).json()
        bridge = (await db_session.execute(
            text("SELECT height_u, width_mm, depth_mm, weight_kg FROM equipment_model_revision")
        )).all()
        return detail, ports, revision, [tuple(row) for row in bridge]

    before = await _snapshot()
    v2 = await _upload(client, admin, make_pdf("server sheet v2 corrected unique"), model_id=model_id)
    assert v2.status_code == 201 and v2.json()["version_number"] == 2
    db_session.expire_all()
    assert await _snapshot() == before


async def test_upload_with_empty_filename_is_rejected(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    resp = await client.post("/api/v1/catalog/documents", files={"file": ("", make_pdf("unique"), "application/pdf")}, headers=admin)
    assert resp.status_code == 422


async def test_rejected_upload_on_revision_does_not_change_the_revision_version(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    model_id = await _setup_model(client, admin)
    revision = await make_draft(client, admin, model_id)
    scanner.mode = "infected"
    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/documents", files=pdf_files(make_pdf("unique")),
        headers={**admin, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 422
    db_session.expire_all()
    current = (await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=admin)).json()
    assert current["version"] == revision["version"]
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "catalog.document.upload_rejected_malware" in actions
