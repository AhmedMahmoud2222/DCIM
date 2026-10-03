"""Hostile review of PR #70 (catalog datasheet PDFs) through the real HTTP boundary.

Each test asserts the SECURE outcome. Concurrency tests use independent database sessions
and connections (never a shared session) and probe the advisory locks from a second
connection, so a lock released too early is detected rather than assumed."""

import asyncio
import hashlib
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.catalog_documents import get_malware_scanner
from app.application.catalog_documents import service as document_service
from app.application.catalog_documents.service import object_lock_key, purge_expired_staged_documents, stage_document
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.db.sync_session import get_sync_db
from app.infrastructure.storage import LocalFileSystemStorageBackend, get_document_storage_backend
from app.main import app
from tests.api._document_helpers import (
    FakeScanner,
    make_draft,
    make_manufacturer,
    make_model,
    make_pdf,
    pdf_files,
)

ROLES = ["Viewer", "Operator", "Engineer", "DCIM Manager"]


async def _model(client, admin):
    return await make_model(client, admin, await make_manufacturer(client, admin))


async def _upload(client, headers, content, *, model_id=None, name="ds.pdf", **kwargs):
    params = {"catalog_model_id": model_id} if model_id else {}
    return await client.post("/api/v1/catalog/documents", params=params, files=pdf_files(content, name), headers=headers, **kwargs)


async def _attach_to_new_draft(client, admin, model_id, content):
    draft = await make_draft(client, admin, model_id)
    resp = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(content),
        headers={**admin, "If-Match": str(draft["version"])},
    )
    assert resp.status_code == 201, resp.text
    return draft["id"], resp.json()


async def _publish_with_document(client, admin, model_id, content):
    draft_id, document = await _attach_to_new_draft(client, admin, model_id, content)
    current = (await client.get(f"/api/v1/catalog/revisions/{draft_id}", headers=admin)).json()
    body = {"dimension_unit": "in", "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4,
            "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220.0}
    patched = await client.patch(f"/api/v1/catalog/revisions/{draft_id}", json=body, headers={**admin, "If-Match": str(current["version"])})
    assert patched.status_code == 200, patched.text
    published = await client.post(f"/api/v1/catalog/revisions/{draft_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    return draft_id, document


# ------------------------------------------------------------------ filenames and headers
HOSTILE_NAMES = [
    "../../../etc/passwd.pdf", "..\\..\\windows\\system32\\x.pdf", "/abs/path/x.pdf", "C:\\abs\\x.pdf",
    'quote".pdf', "semi;colon.pdf", "new\nline.pdf", "carriage\rreturn.pdf", "tab\tname.pdf", "uni\u2028sep.pdf",
    "uni\u202eRTL.pdf", "a" * 400 + ".pdf", "..", ".", "   ", "näme-ü.pdf", "<script>alert(1)</script>.pdf",
    "x\x00.pdf", "CON.pdf", "name.pdf.exe",
]


@pytest.mark.parametrize("raw", range(len(HOSTILE_NAMES)))
async def test_hostile_filenames_never_reach_storage_keys_headers_or_audit(client, auth_headers, scanner, raw):
    name = HOSTILE_NAMES[raw]
    admin = await auth_headers("Administrator")
    content = make_pdf(f"filename case {raw} unique")
    resp = await _upload(client, admin, content, name=name)
    assert resp.status_code == 201, (name, resp.text)
    document = resp.json()
    shown = document["original_filename"]
    assert shown.lower().endswith(".pdf") and len(shown) <= 255
    assert not any(c in shown for c in '/\\"\r\n\t\x00;<>') and all(ord(c) >= 32 and ord(c) < 127 for c in shown)
    sha = hashlib.sha256(content).hexdigest()
    assert document["sha256"] == sha
    stored = get_document_storage_backend()
    assert stored.exists(f"{sha}.pdf")  # keyed by content hash only

    # Authorised download: one header line, one quoted filename, safe fixed headers.
    await client.post("/api/v1/catalog/models", json={}, headers=admin)  # no-op noise; ignored
    downloaded = await client.get(f"/api/v1/catalog/documents/{document['id']}/file", headers=admin)
    assert downloaded.status_code == 200
    disposition = downloaded.headers["content-disposition"]
    assert disposition == f'attachment; filename="{shown}"'
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in downloaded.headers["cache-control"]
    assert downloaded.content == content


async def test_malware_rejection_audit_does_not_store_a_raw_hostile_filename(client, auth_headers, scanner, db_session):
    admin = await auth_headers("Administrator")
    scanner.mode = "infected"
    raw = "evil\r\n<b>x</b>/../name.pdf"
    resp = await _upload(client, admin, make_pdf("infected unique"), name=raw)
    assert resp.status_code == 422
    rows = (await db_session.execute(text(
        "SELECT after->>'filename' FROM audit_log WHERE action = 'catalog.document.upload_rejected_malware'"
    ))).scalars().all()
    assert rows, "the rejection must be audited"
    for value in rows:
        assert not any(c in value for c in '\r\n\t<>/\\'), repr(value)


# ------------------------------------------------------------------ authorization matrix
async def test_download_and_metadata_authorization_matrix(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _model(client, admin)
    staged = (await _upload(client, admin, make_pdf("matrix staged unique"))).json()
    _draft_id, draft_doc = await _attach_to_new_draft(client, admin, await _model(client, admin), make_pdf("matrix draft unique"))
    _pub_id, pub_doc = await _publish_with_document(client, admin, model_id, make_pdf("matrix published unique"))

    def url(doc):
        return f"/api/v1/catalog/documents/{doc['id']}/file"

    # anonymous
    for doc in (staged, draft_doc, pub_doc):
        assert (await client.get(url(doc))).status_code == 401
        assert (await client.get(f"/api/v1/catalog/documents/{doc['id']}")).status_code == 401
    # Administrator reads everything
    for doc in (staged, draft_doc, pub_doc):
        assert (await client.get(url(doc), headers=admin)).status_code == 200
    # Everyone else: only PUBLISHED documents, and only with catalog:document_download
    expected_published = {"Viewer": 403, "Operator": 403, "Engineer": 200, "DCIM Manager": 200}
    for role in ROLES:
        headers = await auth_headers(role)
        assert (await client.get(url(pub_doc), headers=headers)).status_code == expected_published[role], role
        assert (await client.get(url(staged), headers=headers)).status_code == 403, role  # draft disclosure
        assert (await client.get(url(draft_doc), headers=headers)).status_code == 403, role
        # unpublished metadata is read_draft-only
        assert (await client.get(f"/api/v1/catalog/documents/{draft_doc['id']}", headers=headers)).status_code == 403, role
        assert (await client.get(f"/api/v1/catalog/models/{model_id}/documents", headers=headers)).status_code == 403, role


async def test_mutations_require_catalog_administrator_and_ids_are_not_guessable(client, auth_headers, scanner):
    admin = await auth_headers("Administrator")
    model_id = await _model(client, admin)
    draft = await make_draft(client, admin, model_id)
    doc = (await _upload(client, admin, make_pdf("mutation matrix unique"), model_id=model_id)).json()
    for role in ROLES:
        headers = await auth_headers(role)
        assert (await _upload(client, headers, make_pdf(f"{role} unique"))).status_code == 403, role
        attach = await client.post(f"/api/v1/catalog/revisions/{draft['id']}/documents/{doc['id']}", headers={**headers, "If-Match": "1"})
        assert attach.status_code == 403, role
        detach = await client.delete(f"/api/v1/catalog/revisions/{draft['id']}/documents/{doc['id']}", headers={**headers, "If-Match": "1"})
        assert detach.status_code == 403, role
    unknown = uuid.uuid4()
    assert (await client.get(f"/api/v1/catalog/documents/{unknown}/file", headers=admin)).status_code == 404
    assert (await client.get(f"/api/v1/catalog/documents/{unknown}", headers=admin)).status_code == 404
    other_model = await _model(client, admin)
    other_draft = await make_draft(client, admin, other_model)
    cross = await client.post(
        f"/api/v1/catalog/revisions/{other_draft['id']}/documents/{doc['id']}",
        headers={**admin, "If-Match": str(other_draft["version"])},
    )
    assert cross.status_code == 422  # a document never attaches to another model's revision


# ------------------------------------------------------------------ malware scanner policy over a real socket
class _FakeClamd:
    """Minimal clamd INSTREAM peer. `behaviour`: ok | found | garbage | error | hang | close."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            if self.behaviour == "close":
                return
            buf = b""
            while not buf.endswith(b"\x00\x00\x00\x00"):  # zero-length chunk terminates INSTREAM
                part = conn.recv(65536)
                if not part:
                    return
                buf += part
            if self.behaviour == "hang":
                self._stop.wait(8)
                return
            reply = {"ok": b"stream: OK\0", "found": b"stream: Test-Signature FOUND\0", "garbage": b"\xff\xfe nonsense",
                     "error": b"INSTREAM size limit exceeded. ERROR\0"}[self.behaviour]
            conn.sendall(reply)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._server.close()


@pytest.mark.parametrize(
    ("behaviour", "status"),
    [("close", 503), ("hang", 503), ("garbage", 503), ("error", 503), ("found", 422), ("ok", 201)],
)
async def test_required_scan_mode_fails_closed_on_every_scanner_failure(client, auth_headers, settings_override, db_session, behaviour, status):
    admin = await auth_headers("Administrator")
    peer = _FakeClamd(behaviour)
    try:
        settings_override(clamd_host="127.0.0.1", clamd_port=peer.port, clamd_timeout_seconds=1.0, catalog_pdf_scan_mode="required")
        content = make_pdf(f"scan {behaviour} unique")
        resp = await _upload(client, admin, content)
    finally:
        peer.close()
    assert resp.status_code == status, resp.text
    sha = hashlib.sha256(content).hexdigest()
    rows = (await db_session.execute(text("SELECT count(*) FROM catalog_document WHERE sha256 = :s"), {"s": sha})).scalar_one()
    if status == 201:
        assert rows == 1 and get_document_storage_backend().exists(f"{sha}.pdf")
    else:
        assert rows == 0 and not get_document_storage_backend().exists(f"{sha}.pdf")  # nothing stored, nothing recorded
        assert "traceback" not in resp.text.lower() and "127.0.0.1" not in resp.text  # no internals leaked


async def test_production_refuses_to_start_without_required_scanning():
    with pytest.raises(ValueError, match="catalog_pdf_scan_mode"):
        Settings(environment="production", catalog_pdf_scan_mode="optional")
    with pytest.raises(ValueError, match="catalog_pdf_scan_mode"):
        Settings(environment="production", catalog_pdf_scan_mode="off")
    Settings(environment="production", catalog_pdf_scan_mode="required")


async def test_documents_that_were_not_scanned_in_required_mode_are_not_served_in_production(client, auth_headers, settings_override, db_session):
    """A 'skipped' row can only come from a non-production mode (or migrated data). Production
    must not serve content that never passed a scan."""
    admin = await auth_headers("Administrator")
    settings_override(catalog_pdf_scan_mode="off")
    peer_dead = _FakeClamd("close")
    peer_dead.close()
    resp = await _upload(client, admin, make_pdf("skipped unique"))
    assert resp.status_code == 201 and resp.json()["scan_status"] == "skipped"
    settings_override(environment="production", catalog_pdf_scan_mode="required")
    downloaded = await client.get(f"/api/v1/catalog/documents/{resp.json()['id']}/file", headers=admin)
    assert downloaded.status_code != 200


# ------------------------------------------------------------------ locks span the critical sections (probed from a second connection)
async def _lock_is_held(engine, sha: str) -> bool:
    k1, k2 = object_lock_key(sha)
    async with engine.connect() as conn:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": k1, "b": k2})).scalar_one()
        if got:
            await conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": k1, "b": k2})
        return not got


class _ProbingStorage(LocalFileSystemStorageBackend):
    """Records whether the object lock was held, as seen from another connection, at the moment
    the critical storage operation runs."""

    def __init__(self, root, engine, loop):
        super().__init__(root)
        self._engine, self._loop, self.held_during_save, self.held_during_delete = engine, loop, None, None

    def _probe(self, key):
        return asyncio.run_coroutine_threadsafe(_lock_is_held(self._engine, key.removesuffix(".pdf")), self._loop).result(10)

    def save(self, key, content):
        self.held_during_save = self._probe(key)
        super().save(key, content)

    def delete(self, key):
        self.held_during_delete = self._probe(key)
        super().delete(key)


async def _user_id(db_session):
    return (await db_session.execute(text("SELECT id FROM app_user LIMIT 1"))).scalar_one()


async def test_upload_lock_is_held_while_the_object_is_written_and_released_at_commit(client, auth_headers, db_engine, db_session, tmp_path):
    admin = await auth_headers("Administrator")
    model_id = uuid.UUID(await _model(client, admin))
    user_id = await _user_id(db_session)
    storage = _ProbingStorage(tmp_path, db_engine, asyncio.get_running_loop())
    content = make_pdf("lock span upload unique")
    sha = hashlib.sha256(content).hexdigest()
    settings = get_settings().model_copy(update={"catalog_pdf_scan_mode": "required"})
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)() as session:
        await stage_document(session, content=content, original_filename="a.pdf", uploaded_by_user_id=user_id,
                             catalog_model_id=model_id, settings=settings, storage=storage, scanner=FakeScanner())
        assert storage.held_during_save is True  # nobody can delete the object while it is being written
        assert await _lock_is_held(db_engine, sha) is True  # still held until the caller commits
        await session.commit()
    assert await _lock_is_held(db_engine, sha) is False


async def test_purge_holds_the_object_lock_until_the_object_is_deleted(client, auth_headers, db_engine, db_session, tmp_path):
    admin = await auth_headers("Administrator")
    user_id = await _user_id(db_session)
    storage = _ProbingStorage(tmp_path, db_engine, asyncio.get_running_loop())
    content = make_pdf("lock span purge unique")
    settings = get_settings().model_copy(update={"catalog_pdf_scan_mode": "required"})
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)() as session:
        await stage_document(session, content=content, original_filename="a.pdf", uploaded_by_user_id=user_id,
                             catalog_model_id=None, settings=settings, storage=storage, scanner=FakeScanner())
        await session.commit()
    future = datetime.now(UTC) + timedelta(days=30)

    def _purge():
        with get_sync_db() as db:
            return purge_expired_staged_documents(db, storage=storage, retention_days=14, now=future)

    assert await asyncio.to_thread(_purge) == 1
    assert storage.held_during_delete is True  # an upload of the same bytes cannot slip in before the delete
    assert not storage.exists(f"{hashlib.sha256(content).hexdigest()}.pdf")
    del admin


# ------------------------------------------------------------------ duplicate upload racing the purge of the row it returns
async def test_duplicate_upload_never_reports_a_document_the_purge_is_deleting(client, auth_headers, scanner, db_session, monkeypatch):
    admin = await auth_headers("Administrator")
    model_id = await _model(client, admin)
    content = make_pdf("dedupe vs purge unique")
    first = await _upload(client, admin, content, model_id=model_id)
    assert first.status_code == 201
    storage = get_document_storage_backend()
    future = datetime.now(UTC) + timedelta(days=30)
    original = document_service._existing_by_sha
    fired = {"done": False}

    async def _existing_then_purge(db, catalog_model_id, sha256):
        found = await original(db, catalog_model_id, sha256)
        if found is not None and not fired["done"]:
            fired["done"] = True
            def _purge():
                with get_sync_db() as sync_db:
                    return purge_expired_staged_documents(sync_db, storage=storage, retention_days=14, now=future)
            await asyncio.to_thread(_purge)
        return found

    monkeypatch.setattr(document_service, "_existing_by_sha", _existing_then_purge)
    second = await _upload(client, admin, content, model_id=model_id)
    monkeypatch.setattr(document_service, "_existing_by_sha", original)
    if second.status_code in (200, 201):
        # a success response must refer to a document that still exists
        got = await client.get(f"/api/v1/catalog/documents/{second.json()['id']}", headers=admin)
        assert got.status_code == 200, "upload reported success for a document that was purged underneath it"
    else:
        assert second.status_code in (409, 503)


# ------------------------------------------------------------------ orphan objects
async def test_object_left_by_a_failed_transaction_is_eventually_swept(client, auth_headers, db_engine, db_session, tmp_path):
    admin = await auth_headers("Administrator")
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    content = make_pdf("orphan unique")
    sha = hashlib.sha256(content).hexdigest()
    settings = get_settings().model_copy(update={"catalog_pdf_scan_mode": "required"})
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)() as session:
        await stage_document(session, content=content, original_filename="a.pdf", uploaded_by_user_id=user_id,
                             catalog_model_id=None, settings=settings, storage=storage, scanner=FakeScanner())
        await session.rollback()  # the request failed after the file was written
    assert storage.exists(f"{sha}.pdf")
    with get_sync_db() as db:
        purge_expired_staged_documents(db, storage=storage, retention_days=14, now=datetime.now(UTC) + timedelta(days=90))
    assert not storage.exists(f"{sha}.pdf"), "an unreferenced object must not live forever"
    del admin


# ------------------------------------------------------------------ independent-session race: upload+attach vs publish
@pytest_asyncio.fixture
async def race_client(db_engine):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


class _SlowScanner(FakeScanner):
    def scan(self, content):
        import time

        time.sleep(1.5)
        return super().scan(content)


async def test_publish_racing_an_attach_never_changes_a_published_revisions_documents(race_client, make_user, db_engine):
    client = race_client
    user = await make_user(f"racer-{uuid.uuid4().hex[:6]}@example.com", "correct horse battery staple", "Administrator")
    login = await client.post("/api/v1/auth/login", json={"email": user.email, "password": "correct horse battery staple"})
    admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
    app.dependency_overrides[get_malware_scanner] = lambda: _SlowScanner()
    try:
        model_id = await _model(client, admin)
        draft = await make_draft(client, admin, model_id)
        body = {"dimension_unit": "in", "width_value": 19.0, "height_value": 73.5, "depth_value": 39.4,
                "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220.0}
        patched = await client.patch(f"/api/v1/catalog/revisions/{draft['id']}", json=body, headers={**admin, "If-Match": str(draft["version"])})
        version = patched.json()["version"]

        async def attach():
            return await client.post(f"/api/v1/catalog/revisions/{draft['id']}/documents", files=pdf_files(make_pdf("race attach unique")),
                                     headers={**admin, "If-Match": str(version)})

        async def publish():
            await asyncio.sleep(0.4)  # the attach is now inside its slow scan, holding the revision row
            return await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)

        attach_resp, publish_resp = await asyncio.gather(attach(), publish())
    finally:
        app.dependency_overrides.pop(get_malware_scanner, None)
    assert attach_resp.status_code < 500 and publish_resp.status_code < 500, (attach_resp.text, publish_resp.text)
    async with db_engine.connect() as conn:
        published_at = (await conn.execute(text("SELECT published_at FROM catalog_model_revision WHERE id = :i"), {"i": draft["id"]})).scalar_one()
        late_links = (await conn.execute(
            text("SELECT count(*) FROM catalog_revision_document WHERE catalog_model_revision_id = :i AND attached_at > :p"),
            {"i": draft["id"], "p": published_at},
        )).scalar_one() if published_at is not None else 0
    assert late_links == 0, "a document was linked after the revision was published"


# ------------------------------------------------------------------ the orphan sweep never deletes live or young objects
async def test_orphan_sweep_keeps_referenced_young_and_foreign_files(client, auth_headers, db_engine, db_session, tmp_path):
    await auth_headers("Administrator")
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    settings = get_settings().model_copy(update={"catalog_pdf_scan_mode": "required"})
    referenced = make_pdf("sweep referenced unique")
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)() as session:
        await stage_document(session, content=referenced, original_filename="a.pdf", uploaded_by_user_id=user_id,
                             catalog_model_id=None, settings=settings, storage=storage, scanner=FakeScanner())
        await session.commit()
    young = make_pdf("sweep young unique")
    storage.save(f"{hashlib.sha256(young).hexdigest()}.pdf", young)
    (tmp_path / "notes.txt").write_text("not an object key")
    far_future = datetime.now(UTC) + timedelta(days=90)
    with get_sync_db() as db:
        # the referenced row is also old enough to purge, so use a clock where only the grace period matters
        document_service.sweep_orphan_objects(db, storage=storage, now=datetime.now(UTC) + timedelta(seconds=60))
    assert storage.exists(f"{hashlib.sha256(referenced).hexdigest()}.pdf")
    assert storage.exists(f"{hashlib.sha256(young).hexdigest()}.pdf")
    with get_sync_db() as db:
        document_service.sweep_orphan_objects(db, storage=storage, now=far_future)
    assert storage.exists(f"{hashlib.sha256(referenced).hexdigest()}.pdf"), "a referenced object must survive"
    assert not storage.exists(f"{hashlib.sha256(young).hexdigest()}.pdf")
    assert (tmp_path / "notes.txt").exists()
