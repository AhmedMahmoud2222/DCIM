"""Phase 10A PR-3: catalog designer concurrency guarantees, verified against a real
PostgreSQL instance with genuinely separate sessions/connections, not simulated (matching
this repo's `test_catalog_designer_schema.py`/`test_db_constraints.py` convention).

Covers the three races the task calls out explicitly:
- Two concurrent draft edits racing via If-Match/version.
- Simultaneous publication of the same revision (the parent-row lock, spec §5.4/§4.7).
- Two concurrent publishes of different revisions under the same model, racing the legacy
  model find-or-create (spec §4.7 step 1)."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.catalog_designer_service import publish_revision
from app.core.errors import ConflictError
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer
from app.domain.catalog.models import RackModel, RackModelRevision


async def _make_user(db_session) -> User:
    user = User(
        id=uuid.uuid4(), email=f"concurrency-{uuid.uuid4().hex[:8]}@example.com", full_name="Concurrency Test User",
        password_hash=hash_password("correct horse battery staple"),
    )
    db_session.add(user)
    await db_session.flush()
    return user


def _rack_ready_revision_kwargs(catalog_model_id, user_id) -> dict:
    return dict(
        catalog_model_id=catalog_model_id, revision_number=1, created_by_user_id=user_id,
        dimension_unit="mm", width_value=600, height_value=2000, depth_value=1000,
        rack_unit_height=42, weight_unit="kg", weight_value=100,
    )


async def test_stale_if_match_on_concurrent_draft_edit_is_rejected(client, auth_headers):
    """Two admins editing the same draft race exactly like two admins editing the same
    Room (spec §5.1) — the second, working off a version already superseded, gets 409."""
    headers = await auth_headers("Administrator")
    manufacturer = await client.post("/api/v1/catalog/manufacturers", json={"name": "If-Match Co"}, headers=headers)
    model = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer.json()["id"], "category": "rack", "model_name": "If-Match Model"},
        headers=headers,
    )
    revision = (await client.post(f"/api/v1/catalog/models/{model.json()['id']}/revisions", headers=headers)).json()

    first = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json={"weight_value": 10},
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == revision["version"] + 1

    stale = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json={"weight_value": 20},
        headers={**headers, "If-Match": str(revision["version"])},  # the now-superseded version
    )
    assert stale.status_code == 409, stale.text


async def test_concurrent_publish_of_the_same_revision_serializes_one_wins_one_conflicts(db_engine):
    """spec §5.4/§4.7: publish's own SELECT ... FOR UPDATE (app/application/
    catalog_designer_service.py's publish_revision) is the parent-row lock. Verified with
    two genuinely separate AsyncSessions, not two ORM sessions sharing one connection."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Concurrent Publish Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Concurrent Publish Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
        setup.add(revision)
        await setup.commit()
        revision_id = revision.id
        user_id = user.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        # A runs publish_revision() fully (its own FOR UPDATE lock + every write) but does
        # not commit yet — the lock is held for the rest of A's open transaction.
        published_a = await publish_revision(session_a, revision_id=revision_id, user_id=user_id)
        assert published_a.lifecycle_status == "published"

        # B concurrently attempts to publish the same still-uncommitted revision; its own
        # FOR UPDATE must block on A's held lock.
        task_b = asyncio.create_task(publish_revision(session_b, revision_id=revision_id, user_id=user_id))
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock"

        await session_a.commit()

        # Now that A has committed (published), B's blocked call proceeds and must itself
        # be rejected — its trigger/service re-reads the committed 'published' status.
        with pytest.raises(ConflictError):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        legacy_count = (
            await verify.execute(
                select(func.count()).select_from(RackModelRevision).where(
                    RackModelRevision.bridged_from_catalog_revision_id == revision_id
                )
            )
        ).scalar_one()
        assert legacy_count == 1, "exactly one legacy bridge row must exist — never a corrupted partial publish"


async def test_concurrent_publish_of_different_revisions_races_legacy_model_find_or_create(db_engine):
    """spec §4.7 step 1: two different draft revisions under the *same* catalog_model_id
    (no row-lock contention between them — they lock different catalog_model_revision
    rows) genuinely racing _find_or_create_rack_model's INSERT ... ON CONFLICT DO NOTHING
    for the identical (manufacturer.name, model.model_name) legacy RackModel row. Both
    publishes must succeed, both must bridge to the *same* rack_model_id, and only one
    RackModel row may ever be created for that pair.

    Postgres holds an INSERT ... ON CONFLICT's lock on the conflicting unique-index entry
    for the rest of the *transaction*, not just the statement — so B's own INSERT does not
    resolve (proceed to insert, or discover the row and skip) until A's transaction ends.
    A genuine two-session test therefore cannot run both calls to completion concurrently
    via `asyncio.gather` without an intervening commit (that deadlocks: gather never
    returns because B waits on a commit that only happens after gather returns). Instead,
    same pattern as the revision-lock test above: run A to completion but hold it
    uncommitted, start B as a background task, prove B is genuinely blocked, commit A, then
    let B's now-unblocked INSERT discover A's committed row via the fallback SELECT and
    succeed using it."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Find-Or-Create Race Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Find-Or-Create Race Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision_1 = CatalogModelRevision(**{**_rack_ready_revision_kwargs(model.id, user.id), "revision_number": 1})
        revision_2 = CatalogModelRevision(**{**_rack_ready_revision_kwargs(model.id, user.id), "revision_number": 2})
        setup.add_all([revision_1, revision_2])
        await setup.commit()
        revision_1_id, revision_2_id, user_id = revision_1.id, revision_2.id, user.id
        manufacturer_name = manufacturer.name

    session_a = session_factory()
    session_b = session_factory()
    try:
        result_a = await publish_revision(session_a, revision_id=revision_1_id, user_id=user_id)
        assert result_a.lifecycle_status == "published"

        task_b = asyncio.create_task(publish_revision(session_b, revision_id=revision_2_id, user_id=user_id))
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's uncommitted legacy-model INSERT"

        await session_a.commit()

        result_b = await task_b
        await session_b.commit()
    finally:
        await session_a.close()
        await session_b.close()

    assert result_a.lifecycle_status == "published"
    assert result_b.lifecycle_status == "published"

    async with session_factory() as verify:
        rack_models = (
            await verify.execute(select(RackModel).where(RackModel.manufacturer == manufacturer_name))
        ).scalars().all()
        assert len(rack_models) == 1, "exactly one legacy RackModel row must exist despite the concurrent race"

        legacy_revisions = (
            await verify.execute(select(RackModelRevision).where(RackModelRevision.rack_model_id == rack_models[0].id))
        ).scalars().all()
        assert len(legacy_revisions) == 2, "each catalog revision gets its own distinct legacy revision row"
        assert {r.bridged_from_catalog_revision_id for r in legacy_revisions} == {revision_1_id, revision_2_id}


async def test_publish_failure_rolls_back_the_legacy_bridge_insert_too(db_session):
    """'A failure must roll all of it back' (task instruction): publish_revision() never
    commits internally — it only flushes. If the caller rolls back after it has already
    inserted the legacy revision row and set both halves of the bridge (but before
    committing, e.g. because a later step in the same request raised), that legacy row
    must not persist — it is not a separate, independently-committed side effect that
    could be left orphaned by a failure elsewhere in the same request."""
    manufacturer = Manufacturer(name="Rollback Co")
    db_session.add(manufacturer)
    await db_session.flush()
    model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Rollback Model")
    db_session.add(model)
    await db_session.flush()
    user = await _make_user(db_session)
    revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
    db_session.add(revision)
    await db_session.commit()
    revision_id = revision.id
    user_id = user.id

    result = await publish_revision(db_session, revision_id=revision_id, user_id=user_id)
    assert result.lifecycle_status == "published"
    legacy_id = result.legacy_rack_model_revision_id
    assert legacy_id is not None

    await db_session.rollback()  # simulates the route handler's except-block rollback

    reread_legacy = await db_session.get(RackModelRevision, legacy_id)
    assert reread_legacy is None, "the legacy revision INSERT must not survive a rollback of the publish transaction"
    reread_revision = await db_session.get(CatalogModelRevision, revision_id)
    assert reread_revision is not None
    assert reread_revision.lifecycle_status == "draft", "the revision itself must revert to its pre-publish state too"
