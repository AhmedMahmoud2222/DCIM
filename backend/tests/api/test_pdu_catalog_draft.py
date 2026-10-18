import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer


@pytest.mark.asyncio
async def test_get_pdu_draft_as_viewer(client: AsyncClient, make_user, auth_headers, db: AsyncSession):
    await make_user("viewer@example.com", "correct horse battery staple", "catalog_read")
    m = Manufacturer(name=f"M{uuid.uuid4().hex[:6]}")
    db.add(m)
    await db.flush()
    cm = CatalogModel(manufacturer_id=m.id, category="pdu", model_name=f"P{uuid.uuid4().hex[:6]}")
    db.add(cm)
    await db.flush()
    rev = CatalogModelRevision(catalog_model_id=cm.id, revision_number=1, created_by_user_id=(await make_user("admin@example.com", "correct horse battery staple", "catalog_admin")).id)
    db.add(rev)
    await db.flush()
    await db.commit()
    r = await client.get(f"/api/v1/catalog/revisions/{rev.id}/pdu", headers=auth_headers("viewer@example.com"))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_published_cannot_update(client: AsyncClient, make_user, auth_headers, db: AsyncSession):
    admin = await make_user("admin2@example.com", "correct horse battery staple", "catalog_admin")
    m = Manufacturer(name=f"M{uuid.uuid4().hex[:6]}")
    db.add(m)
    await db.flush()
    cm = CatalogModel(manufacturer_id=m.id, category="pdu", model_name=f"P{uuid.uuid4().hex[:6]}")
    db.add(cm)
    await db.flush()
    rev = CatalogModelRevision(catalog_model_id=cm.id, revision_number=1, created_by_user_id=admin.id, lifecycle_status="published", version=1)
    db.add(rev)
    await db.flush()
    await db.commit()
    r = await client.patch(f"/api/v1/catalog/revisions/{rev.id}/pdu", json={"outlets":[]}, headers={**auth_headers("admin2@example.com"), "If-Match": "1"})
    assert r.status_code == 409
