"""Phase 10A PR-3: catalog designer concurrency guarantees, verified against a real
PostgreSQL instance with genuinely separate sessions/connections, not simulated (matching
this repo's `test_catalog_designer_schema.py`/`test_db_constraints.py` convention).

Covers the races the task calls out explicitly:
- Two concurrent draft edits racing via If-Match/version.
- Simultaneous publication of the same revision (the parent-row lock, spec §5.4/§4.7).
- Two concurrent publishes of different revisions under the same model, racing the legacy
  model find-or-create (spec §4.7 step 1).

PR-3 correction pass additions (two genuinely independent sessions throughout, per the
task instruction — not sequential calls against one already-serialized session, which
`test_stale_if_match_on_concurrent_draft_edit_is_rejected` above only demonstrates once A
has already committed):
- `lock_draft_revision_for_edit`'s own FOR UPDATE atomicity, racing two sessions that both
  read the identical starting version (the one scenario a plain load-then-compare cannot
  catch).
- The identical discipline extended to a child template create.
- `allocate_revision_number`'s model-row lock: two concurrent allocations under the same
  model never collide, both succeed with distinct numbers.
- Publication staying serialized against a concurrent draft edit of the same revision
  (`lock_draft_revision_for_edit` and `publish_revision` lock the same row).

PR-3 correction pass, round 2 addition:
- DELETE /catalog/revisions/{id} racing itself: two concurrent deletes of the same draft
  must never both report success or write a duplicate audit/outbox pair."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.audit_service import write_audit_log
from app.application.catalog_designer_service import allocate_revision_number, lock_draft_revision_for_edit, publish_revision
from app.application.outbox_service import write_outbox_event
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_password
from app.domain.audit.models import AuditLog
from app.domain.auth.models import User
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer, NetworkPortTemplate
from app.domain.catalog.models import RackModel, RackModelRevision
from app.domain.outbox.models import OutboxEvent


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


# ------------------------------------------------------------------------ PR-3 correction pass


async def test_two_independent_sessions_racing_revision_patch_serializes_one_wins_one_conflicts(db_engine):
    """Issue 2 (correction pass): the PATCH /catalog/revisions/{id} If-Match/version check
    must be atomic under two genuinely simultaneous requests. Both sessions here read the
    identical starting version before either writes — a plain load-then-compare cannot
    detect this race (both reads happen before either write), which is exactly why
    `lock_draft_revision_for_edit` takes the row lock before comparing versions."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Edit Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Edit Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
        setup.add(revision)
        await setup.commit()
        revision_id = revision.id
        starting_version = revision.version

    session_a = session_factory()
    session_b = session_factory()
    try:
        locked_a = await lock_draft_revision_for_edit(session_a, revision_id=revision_id, if_match_version=starting_version)
        locked_a.weight_value = 111
        await session_a.flush()

        task_b = asyncio.create_task(
            lock_draft_revision_for_edit(session_b, revision_id=revision_id, if_match_version=starting_version)
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock"

        await session_a.commit()

        # B's blocked call now proceeds, re-reads the committed (post-A) version, and
        # correctly detects that its own If-Match is stale — it must never silently apply
        # its own bump on top of A's already-committed change.
        with pytest.raises(ConflictError):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        reread = await verify.get(CatalogModelRevision, revision_id)
        assert reread.version == starting_version + 1, "exactly one increment — B's rejected attempt must not count"
        assert reread.weight_value == 111, "A's edit must survive, never silently overwritten"


async def test_two_independent_sessions_racing_child_mutation_is_serialized_not_lost(db_engine):
    """Issue 2: the version discipline extends to child template create/edit/delete — two
    administrators adding a network port under the same draft race through the identical
    `lock_draft_revision_for_edit` lock the revision's own PATCH uses, so the second's
    stale If-Match is rejected rather than silently applied alongside the first's."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Child Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Child Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
        setup.add(revision)
        await setup.commit()
        revision_id = revision.id
        starting_version = revision.version

    session_a = session_factory()
    session_b = session_factory()
    try:
        await lock_draft_revision_for_edit(session_a, revision_id=revision_id, if_match_version=starting_version)
        port_a = NetworkPortTemplate(
            catalog_model_revision_id=revision_id, stable_key="eth-a", display_name="Eth A", media_type="copper",
            supported_speeds_mbps=[1000], connector_type="rj45", side="front",
        )
        session_a.add(port_a)
        await session_a.flush()

        task_b = asyncio.create_task(
            lock_draft_revision_for_edit(session_b, revision_id=revision_id, if_match_version=starting_version)
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock"

        await session_a.commit()

        with pytest.raises(ConflictError):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        ports = (
            await verify.execute(select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == revision_id))
        ).scalars().all()
        assert len(ports) == 1, "B's rejected attempt must not have inserted a second port"
        reread = await verify.get(CatalogModelRevision, revision_id)
        assert reread.version == starting_version + 1, "only A's mutation may count"


async def test_two_concurrent_revision_number_allocations_under_the_same_model_never_collide(db_engine):
    """Issue 3 (correction pass): creating/cloning drafts computes max(revision_number)+1;
    two concurrent requests under the same model must never collide at
    catalog_model_revision's own UNIQUE(catalog_model_id, revision_number) constraint.
    `allocate_revision_number` locks the parent catalog_model row, so B genuinely blocks
    on A rather than racing it — both succeed, with distinct numbers, rather than one
    succeeding and one hitting a raw constraint violation."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Allocation Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Allocation Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        await setup.commit()
        model_id = model.id
        user_id = user.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        number_a = await allocate_revision_number(session_a, catalog_model_id=model_id)
        # allocate_revision_number only reserves the number by holding the model-row lock
        # across the caller's own subsequent insert — the caller inserts the revision row
        # itself, in the same still-open transaction, before the lock releases at commit.
        session_a.add(CatalogModelRevision(**{**_rack_ready_revision_kwargs(model_id, user_id), "revision_number": number_a}))
        await session_a.flush()

        task_b = asyncio.create_task(allocate_revision_number(session_b, catalog_model_id=model_id))
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock on the catalog_model row"

        await session_a.commit()

        number_b = await task_b
        assert number_b != number_a, "two concurrent allocations under the same model must never collide"
        session_b.add(CatalogModelRevision(**{**_rack_ready_revision_kwargs(model_id, user_id), "revision_number": number_b}))
        await session_b.commit()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        revisions = (
            await verify.execute(select(CatalogModelRevision).where(CatalogModelRevision.catalog_model_id == model_id))
        ).scalars().all()
        assert len(revisions) == 2, "both concurrent creates must succeed — never one lost to an uncaught IntegrityError"
        assert {r.revision_number for r in revisions} == {number_a, number_b}


async def test_concurrent_publish_and_draft_edit_of_the_same_revision_stays_serialized(db_engine):
    """'Keep publication serialized with these edits' (task instruction): publish_revision's
    own SELECT ... FOR UPDATE locks the identical catalog_model_revision row
    lock_draft_revision_for_edit locks, so a concurrent publish and a concurrent draft edit
    of the same revision race through the same lock — never both succeeding against the
    same pre-mutation state."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Publish-vs-Edit Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Publish-vs-Edit Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
        setup.add(revision)
        await setup.commit()
        revision_id = revision.id
        user_id = user.id
        starting_version = revision.version

    session_a = session_factory()
    session_b = session_factory()
    try:
        # A starts publishing (locks + validates + writes) but has not committed yet.
        published_a = await publish_revision(session_a, revision_id=revision_id, user_id=user_id)
        assert published_a.lifecycle_status == "published"

        # B concurrently attempts to edit the same, still-draft-from-B's-view revision.
        task_b = asyncio.create_task(
            lock_draft_revision_for_edit(session_b, revision_id=revision_id, if_match_version=starting_version)
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock"

        await session_a.commit()

        # B's blocked edit now proceeds and re-reads the committed row: it is published, so
        # the draft check rejects it outright — publish won, and the edit never silently
        # applies on top of (or underneath) the publication.
        with pytest.raises(ConflictError):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        reread = await verify.get(CatalogModelRevision, revision_id)
        assert reread.lifecycle_status == "published"


# ------------------------------------------------------------------------ PR-3 correction pass, round 2


async def test_two_independent_sessions_racing_revision_delete_only_one_succeeds(db_engine):
    """DELETE /catalog/revisions/{id} correction: previously loaded the row unlocked and
    checked only lifecycle_status, with no If-Match at all — two concurrent deletes of the
    same draft both passed that check and both committed a DELETE (the second a silent
    zero-row no-op Postgres does not error on), so both reported success and both wrote a
    full, duplicate audit/outbox pair for an entity already gone. Routing through
    lock_draft_revision_for_edit closes this exactly like every other mutation in this
    router: B blocks on A's FOR UPDATE lock, and once A commits (deleting the row), B's own
    re-read of the now-nonexistent row raises NotFoundError — one clean failure, never a
    duplicate success.

    A performs the identical write_audit_log/write_outbox_event calls, in the identical
    order, `delete_draft_revision` (app/api/v1/catalog_designer.py) itself makes — not
    just the bare row delete the rest of this file's concurrency tests use as a proxy for
    the router's DB effect — so this test directly proves the invariant its own docstring
    above already claimed but never checked: exactly one audit_log row and exactly one
    outbox_event row for this delete, never two, since B's blocked lock_draft_revision_
    for_edit call raises before it ever reaches its own would-be audit/outbox write."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Delete Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Delete Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(**_rack_ready_revision_kwargs(model.id, user.id))
        setup.add(revision)
        await setup.commit()
        revision_id = revision.id
        user_id = user.id
        starting_version = revision.version

    session_a = session_factory()
    session_b = session_factory()
    try:
        # Both A and B carry the identical starting If-Match — exactly the "two admins
        # click delete on the same draft" scenario, neither aware of the other.
        locked_a = await lock_draft_revision_for_edit(session_a, revision_id=revision_id, if_match_version=starting_version)
        # Mirrors delete_draft_revision's own call order exactly: audit + outbox writes
        # before the row delete, all in the one transaction A's commit below closes.
        await write_audit_log(
            session_a, actor_user_id=user_id, action="catalog.revision.delete_draft", entity_type="catalog_model_revision",
            entity_id=locked_a.id, request_id=None, correlation_id=None,
            before={"catalog_model_id": str(locked_a.catalog_model_id), "revision_number": locked_a.revision_number},
        )
        await write_outbox_event(
            session_a, event_type="CatalogModelRevisionDraftDeleted", aggregate_type="catalog_model_revision",
            aggregate_id=locked_a.id, payload={"catalog_model_id": str(locked_a.catalog_model_id)}, correlation_id=None,
        )
        await session_a.delete(locked_a)
        await session_a.flush()

        task_b = asyncio.create_task(
            lock_draft_revision_for_edit(session_b, revision_id=revision_id, if_match_version=starting_version)
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's FOR UPDATE lock"

        await session_a.commit()

        with pytest.raises(NotFoundError):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        reread = await verify.get(CatalogModelRevision, revision_id)
        assert reread is None, "the row must be deleted exactly once — B must never re-create or duplicate it"

        audit_count = (
            await verify.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == revision_id, AuditLog.action == "catalog.revision.delete_draft")
            )
        ).scalar_one()
        assert audit_count == 1, f"expected exactly one audit_log row for this delete, got {audit_count}"

        outbox_count = (
            await verify.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == revision_id, OutboxEvent.event_type == "CatalogModelRevisionDraftDeleted")
            )
        ).scalar_one()
        assert outbox_count == 1, f"expected exactly one outbox_event row for this delete, got {outbox_count}"
