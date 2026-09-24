"""Phase 10A PR-5: concurrent marker mutation, verified against a real PostgreSQL
instance with two genuinely independent AsyncSessions — same convention as
test_catalog_designer_concurrency.py's `test_two_independent_sessions_racing_child_
mutation_is_serialized_not_lost`, which this test mirrors exactly, substituting a marker
create for a network-port create. Markers reuse the identical `lock_draft_revision_for_
edit` primitive (catalog_graphic/catalog_graphic_marker carry no version column of their
own — see app/application/catalog_designer_service.py's module docstring), so this proves
the same atomicity guarantee extends to the new child type rather than re-deriving it."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.catalog_designer_service import lock_draft_revision_for_edit
from app.core.errors import ConflictError
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import (
    CatalogGraphic,
    CatalogGraphicMarker,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
)


async def _make_user(db_session) -> User:
    user = User(
        id=uuid.uuid4(), email=f"graphics-concurrency-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Graphics Concurrency Test User", password_hash=hash_password("correct horse battery staple"),
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_two_independent_sessions_racing_marker_create_is_serialized_not_lost(db_engine):
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        manufacturer = Manufacturer(name="Two-Session Marker Co")
        setup.add(manufacturer)
        await setup.flush()
        model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name="Two-Session Marker Model")
        setup.add(model)
        await setup.flush()
        user = await _make_user(setup)
        revision = CatalogModelRevision(
            catalog_model_id=model.id, revision_number=1, created_by_user_id=user.id,
        )
        setup.add(revision)
        await setup.flush()
        graphic = CatalogGraphic(
            catalog_model_revision_id=revision.id, side="front", storage_key="deadbeef.png",
            original_filename="front.png", mime_type="image/png", file_size_bytes=100, width_px=10, height_px=10,
            uploaded_by_user_id=user.id, uploaded_at=datetime.now(UTC),
        )
        setup.add(graphic)
        await setup.commit()
        revision_id = revision.id
        graphic_id = graphic.id
        starting_version = revision.version

    session_a = session_factory()
    session_b = session_factory()
    try:
        await lock_draft_revision_for_edit(session_a, revision_id=revision_id, if_match_version=starting_version)
        marker_a = CatalogGraphicMarker(
            catalog_graphic_id=graphic_id, marker_type="other", label="from A", marker_x=0.1, marker_y=0.1,
        )
        session_a.add(marker_a)
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
        markers = (
            await verify.execute(select(CatalogGraphicMarker).where(CatalogGraphicMarker.catalog_graphic_id == graphic_id))
        ).scalars().all()
        assert len(markers) == 1, "B's rejected attempt must not have inserted a second marker"
        assert markers[0].label == "from A"
        reread = await verify.get(CatalogModelRevision, revision_id)
        assert reread.version == starting_version + 1, "only A's mutation may count"
