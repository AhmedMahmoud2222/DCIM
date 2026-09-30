"""Database-level guarantees for datasheet documents (migration 0031), independent of the
API: immutability of document rows, draft-only links, model/scan checks, version chain
constraints, and the staging-retention purge."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.application.catalog_documents.service import purge_expired_staged_documents
from app.db.sync_session import get_sync_db
from app.domain.catalog.document_models import CatalogDocument
from app.infrastructure.storage import LocalFileSystemStorageBackend
from tests.api._document_helpers import make_draft, make_manufacturer, make_model, make_published_rack_revision

NOW = datetime.now(UTC)


async def _user_id(db_session) -> uuid.UUID:
    row = (
        await db_session.execute(
            text(
                "INSERT INTO app_user (id, email, full_name, password_hash, is_active) "
                "VALUES (gen_random_uuid(), :e, 'T', 'x', true) RETURNING id"
            ),
            {"e": f"{uuid.uuid4().hex}@example.com"},
        )
    ).scalar_one()
    await db_session.commit()
    return row


async def _insert_document(db_session, user_id, *, model_id=None, sha=None, group=None, version=1, supersedes=None,
                           scan_status="clean", uploaded_at=None) -> uuid.UUID:
    sha = sha or uuid.uuid4().hex + uuid.uuid4().hex
    row = (
        await db_session.execute(
            text(
                "INSERT INTO catalog_document (document_group_id, version_number, supersedes_document_id, catalog_model_id, "
                "storage_key, sha256, original_filename, file_size_bytes, page_count, scan_status, uploaded_by_user_id, uploaded_at) "
                "VALUES (:g, :v, :s, :m, :k, :h, 'a.pdf', 10, 1, :ss, :u, :at) RETURNING id"
            ),
            {"g": group or uuid.uuid4(), "v": version, "s": supersedes, "m": model_id, "k": f"{sha}.pdf", "h": sha,
             "ss": scan_status, "u": user_id, "at": uploaded_at or NOW},
        )
    ).scalar_one()
    await db_session.commit()
    return row


async def _setup(client, auth_headers):
    admin = await auth_headers("Administrator")
    model_id = await make_model(client, admin, await make_manufacturer(client, admin))
    return admin, uuid.UUID(model_id)


async def test_document_row_is_immutable_except_first_model_assignment(client, auth_headers, db_session):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    doc = await _insert_document(db_session, user_id)
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(text("UPDATE catalog_document SET page_count = 9 WHERE id = :i"), {"i": doc})
    await db_session.rollback()
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(text("UPDATE catalog_document SET scan_status = 'skipped' WHERE id = :i"), {"i": doc})
    await db_session.rollback()

    await db_session.execute(text("UPDATE catalog_document SET catalog_model_id = :m WHERE id = :i"), {"m": model_id, "i": doc})
    await db_session.commit()
    other_model = uuid.UUID(await make_model(client, _admin, await make_manufacturer(client, _admin)))
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(text("UPDATE catalog_document SET catalog_model_id = :m WHERE id = :i"), {"m": other_model, "i": doc})
    await db_session.rollback()


async def test_version_chain_constraints(client, auth_headers, db_session):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    group = uuid.uuid4()
    v1 = await _insert_document(db_session, user_id, model_id=model_id, group=group, version=1)
    with pytest.raises(IntegrityError):  # duplicate (group, version)
        await _insert_document(db_session, user_id, model_id=model_id, group=group, version=1)
    await db_session.rollback()
    with pytest.raises(IntegrityError):  # supersedes on version 1
        await _insert_document(db_session, user_id, model_id=model_id, version=1, supersedes=v1)
    await db_session.rollback()
    await _insert_document(db_session, user_id, model_id=model_id, group=group, version=2, supersedes=v1)
    with pytest.raises(IntegrityError):  # a document can be superseded once
        await _insert_document(db_session, user_id, model_id=model_id, group=group, version=3, supersedes=v1)
    await db_session.rollback()
    sha = "a" * 64
    await _insert_document(db_session, user_id, model_id=model_id, sha=sha)
    with pytest.raises(IntegrityError):  # same bytes twice for one model
        await _insert_document(db_session, user_id, model_id=model_id, sha=sha)
    await db_session.rollback()


async def test_link_trigger_requires_matching_model_and_clean_scan(client, auth_headers, db_session):
    admin, model_id = await _setup(client, auth_headers)
    other_model = uuid.UUID(await make_model(client, admin, await make_manufacturer(client, admin)))
    user_id = await _user_id(db_session)
    revision = await make_draft(client, admin, str(model_id))

    def _link(doc):
        return db_session.execute(
            text(
                "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, attached_by_user_id, attached_at) "
                "VALUES (:r, :d, :u, now())"
            ),
            {"r": revision["id"], "d": doc, "u": user_id},
        )

    unassigned = await _insert_document(db_session, user_id)
    foreign = await _insert_document(db_session, user_id, model_id=other_model)
    infected = await _insert_document(db_session, user_id, model_id=model_id, scan_status="infected")
    for bad in (unassigned, foreign, infected):
        with pytest.raises((IntegrityError, DBAPIError)):
            await _link(bad)
        await db_session.rollback()
    good = await _insert_document(db_session, user_id, model_id=model_id)
    await _link(good)
    await db_session.commit()


async def test_links_on_published_revision_are_frozen_by_trigger(client, auth_headers, db_session):
    admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    draft = await make_draft(client, admin, str(model_id))
    doc = await _insert_document(db_session, user_id, model_id=model_id)
    await db_session.execute(
        text(
            "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, attached_by_user_id, attached_at) "
            "VALUES (:r, :d, :u, now())"
        ),
        {"r": draft["id"], "d": doc, "u": user_id},
    )
    await db_session.commit()
    await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json={"dimension_unit": "mm", "width_value": 482.6, "height_value": 1000, "depth_value": 1000, "rack_unit_height": 42,
              "weight_unit": "kg", "weight_value": 100},
        headers={**admin, "If-Match": str(draft["version"])},
    )
    assert (await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=admin)).status_code == 200

    newer = await _insert_document(db_session, user_id, model_id=model_id)
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(
            text(
                "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, attached_by_user_id, "
                "attached_at) VALUES (:r, :d, :u, now())"
            ),
            {"r": draft["id"], "d": newer, "u": user_id},
        )
    await db_session.rollback()
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.execute(text("DELETE FROM catalog_revision_document WHERE catalog_model_revision_id = :r"), {"r": draft["id"]})
    await db_session.rollback()
    with pytest.raises(IntegrityError):  # a linked document cannot be deleted
        await db_session.execute(text("DELETE FROM catalog_document WHERE id = :d"), {"d": doc})
    await db_session.rollback()


async def test_purge_removes_only_expired_unattached_unsuperseded_documents(client, auth_headers, db_session, tmp_path):
    admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    old = NOW - timedelta(days=30)

    expired = await _insert_document(db_session, user_id, uploaded_at=old, sha="e" * 64)
    fresh = await _insert_document(db_session, user_id, uploaded_at=NOW - timedelta(days=2), sha="f" * 64)
    group = uuid.uuid4()
    predecessor = await _insert_document(db_session, user_id, model_id=model_id, group=group, version=1, uploaded_at=old, sha="1" * 64)
    await _insert_document(db_session, user_id, model_id=model_id, group=group, version=2, supersedes=predecessor, uploaded_at=old, sha="2" * 64)
    draft = await make_draft(client, admin, str(model_id))
    attached = await _insert_document(db_session, user_id, model_id=model_id, uploaded_at=old, sha="3" * 64)
    await db_session.execute(
        text(
            "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, attached_by_user_id, attached_at) "
            "VALUES (:r, :d, :u, now())"
        ),
        {"r": draft["id"], "d": attached, "u": user_id},
    )
    await db_session.commit()
    for sha in ("e", "f", "1", "2", "3"):
        storage.save(f"{sha * 64}.pdf", b"%PDF-")

    with get_sync_db() as sync_db:
        deleted = purge_expired_staged_documents(sync_db, storage=storage, retention_days=14)
    # Deleted: the expired staged doc and the unattached v2 (v1 is protected by v2 until v2 goes).
    assert deleted == 2
    remaining = set((await db_session.execute(select(CatalogDocument.id))).scalars().all())
    assert expired not in remaining and fresh in remaining and attached in remaining and predecessor in remaining
    assert not storage.exists("e" * 64 + ".pdf") and not storage.exists("2" * 64 + ".pdf")
    assert storage.exists("f" * 64 + ".pdf") and storage.exists("1" * 64 + ".pdf") and storage.exists("3" * 64 + ".pdf")

    with get_sync_db() as sync_db:  # v1 is now eligible on the next run
        assert purge_expired_staged_documents(sync_db, storage=storage, retention_days=14) == 1
    assert not storage.exists("1" * 64 + ".pdf")


async def test_purge_keeps_shared_storage_object_referenced_by_another_row(client, auth_headers, db_session, tmp_path):
    admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    sha = "9" * 64
    await _insert_document(db_session, user_id, sha=sha, uploaded_at=NOW - timedelta(days=30))
    await _insert_document(db_session, user_id, model_id=model_id, sha=sha)  # fresh, other model
    storage.save(f"{sha}.pdf", b"%PDF-")
    with get_sync_db() as sync_db:
        assert purge_expired_staged_documents(sync_db, storage=storage, retention_days=14) == 1
    assert storage.exists(f"{sha}.pdf")


async def test_published_rack_revision_still_publishes_without_documents(client, auth_headers):
    """Regression: the new triggers and tables leave the publish path untouched."""
    admin = await auth_headers("Administrator")
    published = await make_published_rack_revision(client, admin, await make_model(client, admin, await make_manufacturer(client, admin)))
    assert published["lifecycle_status"] == "published"


# ------------------------------------------------------------------ Concurrency


async def _stage_in_own_session(db_engine, content: bytes, model_id, user_id, storage, hold_seconds: float = 0.0):
    """One upload in its own session/transaction, like a separate API request."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.catalog_documents.service import stage_document
    from app.core.config import get_settings
    from tests.api._document_helpers import FakeScanner

    settings = get_settings().model_copy(update={"catalog_pdf_scan_mode": "required"})
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)() as session:
        document, created = await stage_document(
            session, content=content, original_filename="c.pdf", uploaded_by_user_id=user_id, catalog_model_id=model_id,
            settings=settings, storage=storage, scanner=FakeScanner(),
        )
        if hold_seconds:
            await asyncio.sleep(hold_seconds)
        await session.commit()
        return document.id, document.version_number, created


async def test_concurrent_identical_uploads_create_one_document(client, auth_headers, db_session, db_engine, tmp_path):
    import asyncio

    from tests.api._document_helpers import make_pdf

    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    content = make_pdf("concurrent identical unique")
    results = await asyncio.gather(*[_stage_in_own_session(db_engine, content, model_id, user_id, storage) for _ in range(4)])
    assert len({doc_id for doc_id, _v, _c in results}) == 1
    assert sum(1 for _d, _v, created in results if created) == 1
    assert (await db_session.execute(text("SELECT count(*) FROM catalog_document"))).scalar_one() == 1
    assert len(list(tmp_path.glob("*.pdf"))) == 1


async def test_concurrent_different_uploads_get_distinct_consecutive_versions(client, auth_headers, db_session, db_engine, tmp_path):
    import asyncio

    from tests.api._document_helpers import make_pdf

    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    contents = [make_pdf(f"concurrent version {i} unique") for i in range(4)]
    results = await asyncio.gather(*[_stage_in_own_session(db_engine, c, model_id, user_id, storage) for c in contents])
    assert sorted(v for _d, v, _c in results) == [1, 2, 3, 4]
    chain = (await db_session.execute(
        text("SELECT version_number, supersedes_document_id IS NOT NULL FROM catalog_document ORDER BY version_number")
    )).all()
    assert [tuple(r) for r in chain] == [(1, False), (2, True), (3, True), (4, True)]
    assert len({r[0] for r in (await db_session.execute(text("SELECT supersedes_document_id FROM catalog_document WHERE supersedes_document_id IS NOT NULL"))).all()}) == 3


async def test_purge_cannot_delete_an_object_a_concurrent_upload_is_about_to_reference(client, auth_headers, db_session, db_engine, tmp_path):
    """The race the advisory lock closes: an expired, unattached document is being purged while
    another upload of the *same bytes* (for a different model) is mid-transaction. Without the
    lock the purge could count zero references and delete the object the upload then commits."""
    import asyncio

    from tests.api._document_helpers import make_pdf

    admin, model_id = await _setup(client, auth_headers)
    other_model = uuid.UUID(await make_model(client, admin, await make_manufacturer(client, admin)))
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    content = make_pdf("purge race unique")
    first_id, _v, _c = await _stage_in_own_session(db_engine, content, None, user_id, storage)
    await db_session.execute(text("SELECT 1"))
    # Age the staged row is impossible (immutable), so purge with a future clock instead.
    future = datetime.now(UTC) + timedelta(days=30)

    upload = asyncio.create_task(_stage_in_own_session(db_engine, content, other_model, user_id, storage, hold_seconds=1.5))
    await asyncio.sleep(0.5)  # the upload now holds the object lock inside its open transaction

    def _purge() -> int:
        with get_sync_db() as sync_db:
            return purge_expired_staged_documents(sync_db, storage=storage, retention_days=14, now=future)

    purged = await asyncio.to_thread(_purge)
    second_id, _version, created = await upload
    assert created and first_id != second_id
    assert purged == 1  # the expired staged row is gone...
    assert storage.exists(f"{__import__('hashlib').sha256(content).hexdigest()}.pdf")  # ...but the shared object survives
    assert storage.read(f"{__import__('hashlib').sha256(content).hexdigest()}.pdf") == content
    remaining = (await db_session.execute(select(CatalogDocument.id))).scalars().all()
    assert second_id in remaining and first_id not in remaining


async def test_purge_skips_a_document_being_attached_in_an_open_transaction(client, auth_headers, db_session, tmp_path):
    """An attach that has inserted its link (uncommitted) holds a key-share lock on the document
    row, so the purge's `FOR UPDATE SKIP LOCKED` selection skips it. Once the attach commits, the
    link keeps it from ever becoming eligible."""
    admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    draft = await make_draft(client, admin, str(model_id))
    storage = LocalFileSystemStorageBackend(tmp_path)
    sha = "7" * 64
    doc = await _insert_document(db_session, user_id, model_id=model_id, sha=sha, uploaded_at=NOW - timedelta(days=30))
    storage.save(f"{sha}.pdf", b"%PDF-")

    with get_sync_db() as attacher:
        attacher.execute(
            text(
                "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, "
                "attached_by_user_id, attached_at) VALUES (:r, :d, :u, now())"
            ),
            {"r": draft["id"], "d": doc, "u": user_id},
        )  # open transaction, not committed
        with get_sync_db() as sync_db:
            assert purge_expired_staged_documents(sync_db, storage=storage, retention_days=14) == 0
        attacher.commit()
    with get_sync_db() as sync_db:
        assert purge_expired_staged_documents(sync_db, storage=storage, retention_days=14) == 0
    assert storage.exists(f"{sha}.pdf")
    assert doc in (await db_session.execute(select(CatalogDocument.id))).scalars().all()
