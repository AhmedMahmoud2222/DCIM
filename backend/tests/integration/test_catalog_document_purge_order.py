"""Deterministic coverage for the purge pass's treatment of version chains (found while validating PR #122).

`_purge_expired_rows` used to judge a predecessor inside its delete loop, so whether it was purged in the same pass as
its successor depended on which of the two rows the database returned first. For equal `uploaded_at` that is the
physical row order, which a long suite run (row reuse after TRUNCATE) can reverse. Every test here forces that order
with a no-op UPDATE and proves it with `ctid` before purging, so none of them relies on suite-order luck.
"""

import uuid
from datetime import timedelta

from sqlalchemy import select, text

from app.application.catalog_documents.service import purge_expired_staged_documents
from app.db.sync_session import get_sync_db
from app.domain.catalog.document_models import CatalogDocument
from app.infrastructure.storage import LocalFileSystemStorageBackend
from tests.api._document_helpers import make_draft
from tests.integration.test_catalog_document_schema import NOW, _insert_document, _setup, _user_id

OLD = NOW - timedelta(days=30)


def _sha(char: str) -> str:
    return char * 64


async def _ctid_position(db_session, doc_id) -> tuple[int, int]:
    row = (await db_session.execute(text("SELECT ctid::text FROM catalog_document WHERE id = :i"), {"i": doc_id})).scalar_one()
    page, tup = row.strip("()").split(",")
    return int(page), int(tup)


async def _chain(db_session, user_id, model_id, length, *, uploaded_at=OLD, first_sha="a"):
    """v1..vN in one group, all with the same timestamp, rewritten so every row sits AFTER its successor in the heap
    (the physical order that used to purge a predecessor in the same pass as its successor)."""
    group, docs = uuid.uuid4(), []
    for version in range(1, length + 1):
        docs.append(
            await _insert_document(
                db_session, user_id, model_id=model_id, group=group, version=version,
                supersedes=docs[-1] if docs else None, uploaded_at=uploaded_at, sha=_sha(chr(ord(first_sha) + version - 1)),
            )
        )
    for doc in docs:  # oldest version first: each rewrite lands after every row rewritten before it
        await db_session.execute(text("UPDATE catalog_document SET original_filename = original_filename WHERE id = :i"), {"i": doc})
    await db_session.commit()
    positions = [await _ctid_position(db_session, doc) for doc in docs]
    assert positions == sorted(positions), "premise: later-written rows sit later in the heap"
    # Heap order is now v1, v2, ..., vN; reverse it so the newest comes first.
    for doc in reversed(docs):
        await db_session.execute(text("UPDATE catalog_document SET original_filename = original_filename WHERE id = :i"), {"i": doc})
    await db_session.commit()
    positions = [await _ctid_position(db_session, doc) for doc in docs]
    assert positions == sorted(positions, reverse=True), "premise: successors are physically before their predecessors"
    return docs


def _purge(storage, *, batch_size=100):
    with get_sync_db() as sync_db:
        return purge_expired_staged_documents(sync_db, storage=storage, retention_days=14, batch_size=batch_size)


async def _remaining(db_session):
    return set((await db_session.execute(select(CatalogDocument.id))).scalars().all())


def _store(storage, *chars):
    for char in chars:
        storage.save(f"{_sha(char)}.pdf", b"%PDF-")


async def test_predecessor_is_not_purged_in_the_pass_that_purges_its_successor_whatever_the_row_order(
    client, auth_headers, db_session, tmp_path
):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    v1, v2 = await _chain(db_session, user_id, model_id, 2)
    _store(storage, "a", "b")

    assert _purge(storage) == 1  # only the successor; the predecessor is judged against the rows present when the pass began
    assert await _remaining(db_session) == {v1}
    assert storage.exists(_sha("a") + ".pdf") and not storage.exists(_sha("b") + ".pdf")

    assert _purge(storage) == 1  # the predecessor becomes eligible on the next pass
    assert await _remaining(db_session) == set()
    assert not storage.exists(_sha("a") + ".pdf")


async def test_a_chain_is_purged_one_link_per_pass_from_the_newest(client, auth_headers, db_session, tmp_path):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    v1, v2, v3 = await _chain(db_session, user_id, model_id, 3)
    _store(storage, "a", "b", "c")

    for expected_left in ({v1, v2}, {v1}, set()):
        assert _purge(storage) == 1
        assert await _remaining(db_session) == expected_left
    assert _purge(storage) == 0


async def test_a_live_successor_keeps_its_predecessor_and_an_attached_successor_keeps_both(
    client, auth_headers, db_session, tmp_path
):
    admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    v1, v2 = await _chain(db_session, user_id, model_id, 2)
    draft = await make_draft(client, admin, str(model_id))
    await db_session.execute(
        text(
            "INSERT INTO catalog_revision_document (catalog_model_revision_id, catalog_document_id, attached_by_user_id, "
            "attached_at) VALUES (:r, :d, :u, now())"
        ),
        {"r": draft["id"], "d": v2, "u": user_id},
    )
    await db_session.commit()
    _store(storage, "a", "b")

    assert _purge(storage) == 0
    assert _purge(storage) == 0
    assert await _remaining(db_session) == {v1, v2}
    assert storage.exists(_sha("a") + ".pdf") and storage.exists(_sha("b") + ".pdf")


async def test_unrelated_expired_rows_still_purge_alongside_a_protected_predecessor(
    client, auth_headers, db_session, tmp_path
):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    v1, v2 = await _chain(db_session, user_id, model_id, 2)
    await _insert_document(db_session, user_id, uploaded_at=OLD, sha=_sha("5"))
    fresh = await _insert_document(db_session, user_id, uploaded_at=NOW - timedelta(days=1), sha=_sha("f"))
    _store(storage, "a", "b", "5", "f")

    assert _purge(storage) == 2  # the successor and the unrelated staged row
    assert await _remaining(db_session) == {v1, fresh}
    assert not storage.exists(_sha("5") + ".pdf") and storage.exists(_sha("f") + ".pdf")


async def test_batch_limit_selects_a_deterministic_set_and_never_starves_a_successor_behind_its_predecessor(
    client, auth_headers, db_session, tmp_path
):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    # Five unrelated expired rows with identical timestamps: a batch of two always takes the two lowest ids.
    ids = sorted([await _insert_document(db_session, user_id, uploaded_at=OLD, sha=_sha(c)) for c in "01234"])
    _store(storage, *"01234")
    assert _purge(storage, batch_size=2) == 2
    assert await _remaining(db_session) == set(ids[2:])
    assert _purge(storage, batch_size=2) == 2
    assert await _remaining(db_session) == set(ids[4:])
    assert _purge(storage, batch_size=2) == 1
    assert await _remaining(db_session) == set()

    # A one-row batch: the predecessor (excluded while a successor exists) must not occupy the slot the successor needs.
    v1, v2 = await _chain(db_session, user_id, model_id, 2, first_sha="a")
    _store(storage, "a", "b")
    assert _purge(storage, batch_size=1) == 1
    assert await _remaining(db_session) == {v1}
    assert _purge(storage, batch_size=1) == 1
    assert await _remaining(db_session) == set()


async def test_a_storage_object_shared_by_a_surviving_row_is_kept_and_an_unshared_one_is_removed(
    client, auth_headers, db_session, tmp_path
):
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    v1, v2 = await _chain(db_session, user_id, model_id, 2)  # v2 uses key "b", v1 uses key "a"
    survivor = await _insert_document(db_session, user_id, uploaded_at=NOW - timedelta(days=1), sha=_sha("b"))  # shares v2's bytes
    _store(storage, "a", "b")

    assert _purge(storage) == 1  # v2 goes, but the object is still referenced by the fresh row
    assert storage.exists(_sha("b") + ".pdf") and storage.exists(_sha("a") + ".pdf")
    assert await _remaining(db_session) == {v1, survivor}

    assert _purge(storage) == 1  # v1 goes; nobody else references key "a"
    assert not storage.exists(_sha("a") + ".pdf") and storage.exists(_sha("b") + ".pdf")


async def test_two_expired_rows_sharing_one_object_remove_it_once_both_are_gone(client, auth_headers, db_session, tmp_path):
    _admin, _model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    await _insert_document(db_session, user_id, uploaded_at=OLD, sha=_sha("9"))
    await _insert_document(db_session, user_id, uploaded_at=OLD, sha=_sha("9"))
    _store(storage, "9")

    assert _purge(storage) == 2
    assert await _remaining(db_session) == set()
    assert not storage.exists(_sha("9") + ".pdf")


async def test_a_predecessor_stamped_later_than_its_successor_is_still_not_purged_in_the_same_pass(
    client, auth_headers, db_session, tmp_path
):
    """Clock steps or concurrent uploads can leave a predecessor with a later `uploaded_at` than its successor, so
    neither timestamp nor version ordering may be what protects it."""
    _admin, model_id = await _setup(client, auth_headers)
    user_id = await _user_id(db_session)
    storage = LocalFileSystemStorageBackend(tmp_path)
    group = uuid.uuid4()
    v1 = await _insert_document(db_session, user_id, model_id=model_id, group=group, version=1, uploaded_at=OLD + timedelta(hours=1), sha=_sha("a"))
    v2 = await _insert_document(db_session, user_id, model_id=model_id, group=group, version=2, supersedes=v1, uploaded_at=OLD, sha=_sha("b"))
    _store(storage, "a", "b")

    assert _purge(storage) == 1
    assert await _remaining(db_session) == {v1}
    assert _purge(storage) == 1
    assert await _remaining(db_session) == set()
    assert v2 not in await _remaining(db_session)
