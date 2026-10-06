"""Apply through HTTP, real PostgreSQL locks/constraints and real extraction worker."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.catalog_documents.extraction.apply import apply_candidates
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.domain.audit.models import AuditLog
from app.domain.catalog.application_models import CatalogExtractionApplication
from app.domain.catalog.designer_models import CatalogModelRevision
from tests._extraction_pdfs import native_pdf
from tests.api._document_helpers import make_draft
from tests.api.test_catalog_extraction import _candidates, _document, _extract

BASE = "/api/v1/catalog"


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
def dispatched(monkeypatch):
    monkeypatch.setattr("app.api.v1.catalog_extraction.dispatch_extraction_job", lambda _: True)


async def setup(client, admin, lines=None, accept=True):
    model_id, document = await _document(client, admin, native_pdf([lines or [
        "CX-100 Technical Specifications", "Typical power consumption: 1.2 kW", "Weight: 20 lb",
        "Width: 440 mm", "Height: 44 mm", "Depth: 600 mm",
    ]]))
    draft = await make_draft(client, admin, model_id)
    response = await client.post(f"{BASE}/revisions/{draft['id']}/documents/{document['id']}",
                                 headers={**admin, "If-Match": str(draft["version"])})
    assert response.status_code == 201, response.text
    draft["version"] = response.json()["revision_version"]
    job = await _extract(client, admin, document["id"])
    assert job["status"] == "completed", job
    candidates = await _candidates(client, admin, job["id"])
    if not accept:
        return draft, document, job, candidates
    for candidate in candidates:
        response = await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", headers=admin,
                                     json={"decision": "accepted", "confirm_model_attribution": True})
        assert response.status_code == 200, response.text
    return draft, document, job, candidates


async def apply(client, admin, draft, document, job, candidates, **extra):
    return await client.post(f"{BASE}/revisions/{draft['id']}/extraction-applications",
                             headers={**admin, "If-Match": str(draft["version"])},
                             json={"document_id": document["id"], "job_id": job["id"],
                                   "candidate_ids": [c["id"] for c in candidates], **extra})


async def test_apply_canonical_values_and_append_only_provenance(client, admin, scanner, dispatched, db_session):
    draft, document, job, candidates = await setup(client, admin)
    response = await apply(client, admin, draft, document, job, candidates)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["revision_version"] == draft["version"] + 1
    assert result["document_sha256"] == document["sha256"]
    revision = (await client.get(f"{BASE}/revisions/{draft['id']}", headers=admin)).json()
    assert revision["typical_power_w"] == 1200
    assert revision["weight_unit"] == "kg" and revision["weight_value"] == 9.072
    assert revision["dimension_unit"] == "mm" and revision["width_value"] == 440
    assert revision["lifecycle_status"] == "draft"
    power = next(c for c in result["candidates"] if c["field_key"] == "power_typical_w")
    assert power["source_unit"] == "kW" and power["canonical_unit"] == "W"
    assert power["registry_version"] == "1" and power["source_text"] and power["reviewed_by_user_id"]
    assert (await db_session.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "catalog.extraction.apply"))).scalar_one() == 1
    history = await client.get(f"{BASE}/revisions/{draft['id']}/extraction-applications", headers=admin)
    assert history.status_code == 200 and history.json()[0]["id"] == result["id"]
    with pytest.raises(DBAPIError):
        await db_session.execute(text("UPDATE catalog_extraction_application SET after_values = '{}' WHERE id = :id"), {"id": result["id"]})
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(text("DELETE FROM catalog_extraction_application WHERE id = :id"), {"id": result["id"]})
    await db_session.rollback()


async def test_auth_and_wrong_model_document_job_candidate_ids(client, admin, scanner, dispatched, auth_headers, db_session):
    first = await setup(client, admin)
    second = await setup(client, admin)
    draft, document, job, candidates = first
    viewer = await auth_headers("Viewer")
    assert (await apply(client, viewer, *first)).status_code == 403
    assert (await client.get(f"{BASE}/revisions/{draft['id']}/extraction-applications", headers=viewer)).status_code == 403
    assert (await apply(client, admin, draft, second[1], job, candidates)).status_code == 409
    await db_session.rollback()
    assert (await apply(client, admin, draft, document, second[2], candidates)).status_code == 409
    await db_session.rollback()
    assert (await apply(client, admin, draft, document, job, second[3])).status_code == 409
    await db_session.rollback()
    assert (await apply(client, admin, second[0], document, job, candidates)).status_code == 409
    await db_session.rollback()
    row = await db_session.get(CatalogModelRevision, uuid.UUID(draft["id"]))
    assert row.version == draft["version"] and row.typical_power_w is None


async def test_precondition_stale_version_and_duplicate_selection(client, admin, scanner, dispatched, db_session):
    args = await setup(client, admin)
    draft, document, job, candidates = args
    body = {"document_id": document["id"], "job_id": job["id"], "candidate_ids": [candidates[0]["id"]]}
    response = await client.post(f"{BASE}/revisions/{draft['id']}/extraction-applications", headers=admin, json=body)
    assert response.status_code == 428
    assert (await apply(client, admin, draft, document, job, [candidates[0], candidates[0]])).status_code == 422
    assert (await apply(client, admin, *args)).status_code == 201
    assert (await apply(client, admin, *args)).status_code == 409
    await db_session.rollback()
    assert (await db_session.execute(select(func.count()).select_from(CatalogExtractionApplication))).scalar_one() == 1


async def test_preserves_authored_units_and_requires_explicit_overwrite(client, admin, scanner, dispatched, db_session):
    draft, document, job, candidates = await setup(client, admin)
    response = await client.patch(f"{BASE}/revisions/{draft['id']}", headers={**admin, "If-Match": str(draft["version"])},
                                  json={"dimension_unit": "in", "width_value": 19, "height_value": 2, "weight_unit": "lb", "weight_value": 1})
    assert response.status_code == 200, response.text
    draft["version"] = response.json()["version"]
    chosen = [c for c in candidates if c["field_key"] in {"width", "weight"}]
    assert (await apply(client, admin, draft, document, job, chosen)).status_code == 409
    await db_session.rollback()
    response = await apply(client, admin, draft, document, job, chosen, overwrite_existing=True)
    assert response.status_code == 201, response.text
    revision = (await client.get(f"{BASE}/revisions/{draft['id']}", headers=admin)).json()
    assert revision["dimension_unit"] == "in" and revision["height_value"] == 2
    assert revision["width_value"] == 17.323 and revision["weight_value"] == 20 and revision["weight_unit"] == "lb"


@pytest.mark.parametrize("lines", [
    ["CX-100 Technical Specifications", "Typical power consumption: 200-300 W"],
    ["CX-100 Technical Specifications", "Typical power consumption: 300"],
    ["CX-100 Technical Specifications", "Shipping weight: 20 kg"],
])
async def test_invalid_candidate_batch_has_no_partial_writes(client, admin, scanner, dispatched, db_session, lines):
    draft, document, job, candidates = await setup(client, admin, lines + ["Width: 440 mm"])
    assert (await apply(client, admin, draft, document, job, candidates)).status_code == 422
    await db_session.rollback()
    row = await db_session.get(CatalogModelRevision, uuid.UUID(draft["id"]))
    assert row.width_value is None and row.version == draft["version"]
    assert (await db_session.execute(select(func.count()).select_from(CatalogExtractionApplication))).scalar_one() == 0


@pytest.mark.parametrize("writer", ["write_audit_log", "write_outbox_event"])
async def test_audit_failure_rolls_back_values_version_and_provenance(client, admin, scanner, dispatched, db_session, monkeypatch, writer):
    args = await setup(client, admin)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(f"app.api.v1.catalog_extraction.{writer}", fail)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await apply(client, admin, *args)
    await db_session.rollback()
    row = await db_session.get(CatalogModelRevision, uuid.UUID(args[0]["id"]))
    assert row.typical_power_w is None and row.version == args[0]["version"]
    assert (await db_session.execute(select(func.count()).select_from(CatalogExtractionApplication))).scalar_one() == 0


async def test_postgresql_simultaneous_apply_one_winner(client, admin, scanner, dispatched, db_engine):
    draft, document, job, candidates = await setup(client, admin)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    locked = asyncio.Event()
    release = asyncio.Event()

    async def mutate(wait):
        async with factory() as db:
            try:
                revision, _, _, _ = await apply_candidates(
                    db, revision_id=uuid.UUID(draft["id"]), document_id=uuid.UUID(document["id"]), job_id=uuid.UUID(job["id"]),
                    candidate_ids=[uuid.UUID(candidates[0]["id"])], if_match_version=draft["version"],
                    overwrite_existing=True, settings=get_settings(),
                )
                if wait:
                    locked.set()
                    await release.wait()
                await db.commit()
                return revision.version
            except ConflictError:
                await db.rollback()
                return "conflict"

    winner = asyncio.create_task(mutate(True))
    await asyncio.wait_for(locked.wait(), 5)
    loser = asyncio.create_task(mutate(False))
    await asyncio.sleep(0.1)
    assert not loser.done(), "Second writer must wait for the revision lock"
    release.set()
    assert await asyncio.wait_for(winner, 5) == draft["version"] + 1
    assert await asyncio.wait_for(loser, 5) == "conflict"
    async with factory() as db:
        row = await db.get(CatalogModelRevision, uuid.UUID(draft["id"]))
        assert row.version == draft["version"] + 1
        assert row.width_value is None or Decimal(str(row.width_value)) > 0
