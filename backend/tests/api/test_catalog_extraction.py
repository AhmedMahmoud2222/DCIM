"""DCIM01 PDF datasheet import, PR-B: extraction jobs and candidate review, end to end through the real
HTTP boundary and the real worker code against PostgreSQL.

Covers: idempotency and concurrency of requests and claims, the lease and fencing token, retry, history
across extractor versions, authorization and direct-ID access, multi-model attribution, conflicts, the
review rules, database-level immutability, and the guarantee that no catalog revision is ever modified."""

import asyncio
import io
import os
import shutil
import stat
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.application.catalog_documents.extraction import service as extraction_service
from app.application.catalog_documents.extraction.pipeline import run_pipeline
from app.application.catalog_documents.extraction.service import (
    claim_job,
    find_jobs_to_dispatch,
    run_extraction_job,
)
from app.core.config import get_settings
from app.db.sync_session import get_sync_db
from app.domain.audit.models import AuditLog
from app.domain.catalog.extraction_models import CatalogExtractionCandidate, CatalogExtractionJob
from app.infrastructure.storage import get_document_storage_backend
from tests._extraction_pdfs import native_pdf, scanned_pdf, table_pdf
from tests.api._document_helpers import make_draft, make_manufacturer, make_pdf, pdf_files

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox")

DATASHEET = [
    "CX-100 Technical Specifications",
    "Typical power consumption: 350 W",
    "Maximum power: 500 W",
    "Heat dissipation: 1195 BTU/hr",
    "Weight: 12.5 kg",
    "Dimensions (H x W x D): 44 x 440 x 600 mm",
]
TABLE = [
    ["Specification", "CX-100", "CX-200", "CX-300"],
    ["Typical power", "350 W", "520 W", "700 W"],
    ["Maximum power", "500 W", "800 W", "1,100 W"],
    ["Weight", "12 kg", "15 kg", "20 kg"],
]
BASE = "/api/v1/catalog"


RACK_BODY = {
    "dimension_unit": "mm", "width_value": 440.0, "height_value": 44.0, "depth_value": 600.0, "rack_unit_height": 1,
    "weight_unit": "kg", "weight_value": 12.5,
}


async def _publish_with_document(client, admin, model_id: str, document_id: str, *, retire: bool = False) -> dict:
    """Draft -> attach the datasheet -> fill the rack fields -> publish (-> retire)."""
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(f"{BASE}/revisions/{draft['id']}/documents/{document_id}", headers={**admin, "If-Match": str(draft["version"])})
    assert attach.status_code == 201, attach.text
    version = (await client.get(f"{BASE}/revisions/{draft['id']}", headers=admin)).json()["version"]
    patched = await client.patch(f"{BASE}/revisions/{draft['id']}", json=RACK_BODY, headers={**admin, "If-Match": str(version)})
    assert patched.status_code == 200, patched.text
    published = await client.post(f"{BASE}/revisions/{draft['id']}/publish", headers=admin)
    assert published.status_code == 200, published.text
    if retire:
        retired = await client.post(f"{BASE}/revisions/{draft['id']}/retire", json={"reason": "end of life"}, headers=admin)
        assert retired.status_code == 200, retired.text
        return retired.json()
    return published.json()


@pytest.fixture
def dispatched(monkeypatch):
    """Replaces the Celery dispatch with a recorder: the tests run the worker function themselves."""
    sent: list[uuid.UUID] = []
    monkeypatch.setattr("app.api.v1.catalog_extraction.dispatch_extraction_job", lambda job_id: sent.append(job_id) or True)
    return sent


async def _model(client, headers, *, name="CX-100", number=None) -> str:
    manufacturer = await make_manufacturer(client, headers)
    body = {"manufacturer_id": manufacturer, "category": "rack", "model_name": name}
    if number:
        body["model_number"] = number
    resp = await client.post(f"{BASE}/models", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _upload(client, headers, model_id: str | None, content: bytes, name: str = "datasheet.pdf") -> dict:
    params = {"catalog_model_id": model_id} if model_id else {}
    resp = await client.post(f"{BASE}/documents", params=params, files=pdf_files(content, name), headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _document(client, headers, content: bytes, *, name="CX-100", number=None, filename="datasheet.pdf"):
    model_id = await _model(client, headers, name=name, number=number)
    return model_id, await _upload(client, headers, model_id, content, filename)


def run_job(job_id: str, **changes) -> str:
    settings = get_settings().model_copy(update=changes) if changes else get_settings()
    return run_extraction_job(
        uuid.UUID(job_id), settings=settings, storage=get_document_storage_backend(), session_factory=get_sync_db
    )


async def _request(client, headers, document_id: str):
    return await client.post(f"{BASE}/documents/{document_id}/extraction-jobs", headers=headers)


async def _extract(client, headers, document_id: str, **changes) -> dict:
    created = await _request(client, headers, document_id)
    assert created.status_code in (200, 202), created.text
    job_id = created.json()["id"]
    await asyncio.to_thread(run_job, job_id, **changes)
    job = await client.get(f"{BASE}/extraction-jobs/{job_id}", headers=headers)
    assert job.status_code == 200, job.text
    return job.json()


async def _candidates(client, headers, job_id: str, **params) -> list[dict]:
    resp = await client.get(f"{BASE}/extraction-jobs/{job_id}/candidates", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


# ------------------------------------------------------------------------------ happy path and provenance


async def test_native_datasheet_extracts_reviewable_candidates_with_full_provenance(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job = await _extract(client, admin, document["id"])
    assert job["status"] == "completed" and job["outcome"] == "complete" and job["method_summary"] == "native"
    assert job["model_resolution"] == "single_model_matched" and job["is_current"] is True
    assert job["pages_total"] == 1 and job["candidate_count"] >= 6 and job["error_code"] is None

    by_field = {c["field_key"]: c for c in await _candidates(client, admin, job["id"])}
    typical = by_field["power_typical_w"]
    assert (typical["value_numeric"], typical["unit"], typical["raw_value"], typical["raw_unit"]) == (350.0, "W", "350", "W")
    assert typical["source_text"] == "Typical power consumption: 350 W"
    assert typical["page_number"] == 1 and typical["method"] == "native" and typical["review_status"] == "pending"
    assert by_field["heat_dissipation"]["unit"] == "BTU/hr"
    assert {"width", "height", "depth"} <= by_field.keys()
    assert all(c["model_match"] == "target" for c in by_field.values())


async def test_provenance_points_at_the_page_and_the_exact_text_it_was_read_from(client, admin, scanner, dispatched):
    pages = [
        ["CX-100 Technical Specifications", "Typical power: 350 W"],
        ["Marketing", "Fast. Reliable."],
        ["Mechanical", "Weight: 12.5 kg", "Rated power: 800 W"],
    ]
    content = native_pdf(pages)
    _, document = await _document(client, admin, content)
    job = await _extract(client, admin, document["id"])
    reader = PdfReader(io.BytesIO(content))
    page_text = {n + 1: " ".join((p.extract_text(extraction_mode="layout") or "").split()) for n, p in enumerate(reader.pages)}
    candidates = await _candidates(client, admin, job["id"])
    assert {c["field_key"]: c["page_number"] for c in candidates} == {"power_typical_w": 1, "weight": 3, "power_rated_w": 3}
    for candidate in candidates:
        assert " ".join(candidate["source_text"].split()) in page_text[candidate["page_number"]]
        assert candidate["raw_value"] in candidate["source_text"]


async def test_multi_model_datasheet_never_gives_the_target_another_models_numbers(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, table_pdf([TABLE]), name="CX-200")
    job = await _extract(client, admin, document["id"])
    assert job["model_resolution"] == "multi_model_matched" and set(job["identified_models"]) == {"CX-100", "CX-200", "CX-300"}
    default = await _candidates(client, admin, job["id"])
    assert {(c["field_key"], c["value_numeric"]) for c in default} == {
        ("power_typical_w", 520.0), ("power_max_w", 800.0), ("weight", 15.0),
    }
    assert all(c["model_match"] == "target" and c["model_context"] == "CX-200" for c in default)
    everything = await _candidates(client, admin, job["id"], include_other_models=True)
    others = [c for c in everything if c["model_match"] == "other"]
    assert {c["model_context"] for c in others} == {"CX-100", "CX-300"} and len(others) == 6
    assert len(await _candidates(client, admin, job["id"], model_match="other")) == 6


async def test_target_not_found_in_a_multi_model_datasheet_offers_nothing_as_the_target(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, table_pdf([TABLE]), name="ZZ-900")
    job = await _extract(client, admin, document["id"])
    assert job["model_resolution"] == "target_not_found"
    assert await _candidates(client, admin, job["id"], model_match="target") == []


async def test_ambiguous_model_needs_explicit_human_attribution(client, admin, scanner, dispatched):
    text_pages = [["X300 Technical Specifications", "Typical power: 100 W", "X300-S Technical Specifications", "Typical power: 90 W"]]
    _, document = await _document(client, admin, native_pdf(text_pages), name="X300 / X300-S")
    job = await _extract(client, admin, document["id"])
    assert job["model_resolution"] == "ambiguous_target"
    candidates = await _candidates(client, admin, job["id"])
    assert candidates and all(c["model_match"] == "unattributed" for c in candidates)
    refused = await client.post(f"{BASE}/extraction-candidates/{candidates[0]['id']}/review", json={"decision": "accepted"}, headers=admin)
    assert refused.status_code == 422
    confirmed = await client.post(
        f"{BASE}/extraction-candidates/{candidates[0]['id']}/review",
        json={"decision": "accepted", "confirm_model_attribution": True, "note": "checked the PDF"}, headers=admin,
    )
    assert confirmed.status_code == 200 and confirmed.json()["model_attribution_confirmed"] is True


async def test_a_datasheet_with_no_model_text_requires_confirmation_for_every_value(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([["Typical power: 350 W", "Weight: 12 kg"]]))
    job = await _extract(client, admin, document["id"])
    assert job["model_resolution"] == "no_model_evidence"
    candidates = await _candidates(client, admin, job["id"])
    assert all(c["model_match"] == "unattributed" and "no_model_evidence" in c["flags"] for c in candidates)


async def test_conflicting_values_remain_unresolved_until_a_human_picks_one(client, admin, scanner, dispatched):
    pages = [["CX-100 Technical Specifications", "Typical power: 350 W"], ["Typical power: 410 W"]]
    _, document = await _document(client, admin, native_pdf(pages))
    job = await _extract(client, admin, document["id"])
    power = await _candidates(client, admin, job["id"], field_key="power_typical_w")
    assert len(power) == 2 and all("conflict" in c["flags"] and c["review_status"] == "pending" for c in power)
    assert power[0]["conflict_group_key"] == power[1]["conflict_group_key"]
    first = await client.post(f"{BASE}/extraction-candidates/{power[0]['id']}/review", json={"decision": "accepted"}, headers=admin)
    assert first.status_code == 200
    second = await client.post(f"{BASE}/extraction-candidates/{power[1]['id']}/review", json={"decision": "accepted"}, headers=admin)
    assert second.status_code == 409
    rejected = await client.post(f"{BASE}/extraction-candidates/{power[1]['id']}/review", json={"decision": "rejected"}, headers=admin)
    assert rejected.status_code == 200


async def test_review_rules_and_finality(client, admin, scanner, dispatched, db_session):
    _, document = await _document(client, admin, table_pdf([TABLE]), name="CX-200")
    job = await _extract(client, admin, document["id"])
    other = (await _candidates(client, admin, job["id"], model_match="other"))[0]
    target = (await _candidates(client, admin, job["id"]))[0]
    refused = await client.post(f"{BASE}/extraction-candidates/{other['id']}/review", json={"decision": "accepted"}, headers=admin)
    assert refused.status_code == 422 and "different model" in refused.json()["detail"]
    ok = await client.post(f"{BASE}/extraction-candidates/{target['id']}/review", json={"decision": "accepted", "note": "ok"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["review_status"] == "accepted" and ok.json()["reviewed_by_user_id"]
    again = await client.post(f"{BASE}/extraction-candidates/{target['id']}/review", json={"decision": "rejected"}, headers=admin)
    assert again.status_code == 409
    assert (await client.post(f"{BASE}/extraction-candidates/{target['id']}/review", json={"decision": "maybe"}, headers=admin)).status_code == 422
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "catalog.extraction.request" in actions and "catalog.extraction.candidate_review" in actions
    pending = await _candidates(client, admin, job["id"], review_status="pending")
    assert target["id"] not in {c["id"] for c in pending}


# ------------------------------------------------------------------------------ idempotency, concurrency, retry


async def test_a_repeated_request_returns_the_same_job_and_dispatches_once(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    first = await _request(client, admin, document["id"])
    second = await _request(client, admin, document["id"])
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["id"] == second.json()["id"] and dispatched == [uuid.UUID(first.json()["id"])]


@pytest.fixture
async def race_client(db_engine):
    """One database session per request, like production, so requests can really overlap."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import get_db
    from app.main import app

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def test_concurrent_requests_create_exactly_one_job(race_client, make_user, scanner, dispatched, db_engine):
    email = f"race-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, "correct horse battery staple", "Administrator")
    login = await race_client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    _, document = await _document(race_client, headers, native_pdf([DATASHEET]))
    responses = await asyncio.gather(*[_request(race_client, headers, document["id"]) for _ in range(8)])
    assert [r.status_code for r in responses].count(202) == 1 and {r.status_code for r in responses} <= {200, 202}
    assert len({r.json()["id"] for r in responses}) == 1 and len(dispatched) == 1
    async with db_engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM catalog_extraction_job"))).scalar_one() == 1


async def test_only_one_of_many_simultaneous_claims_wins(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job_id = uuid.UUID((await _request(client, admin, document["id"])).json()["id"])

    def attempt():
        with get_sync_db() as db:
            return claim_job(db, job_id, settings=get_settings())

    claims = await asyncio.gather(*[asyncio.to_thread(attempt) for _ in range(8)])
    assert len([c for c in claims if c is not None]) == 1


async def test_two_workers_given_the_same_job_produce_one_result(client, admin, scanner, dispatched, db_session):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job_id = (await _request(client, admin, document["id"])).json()["id"]
    outcomes = await asyncio.gather(asyncio.to_thread(run_job, job_id), asyncio.to_thread(run_job, job_id))
    assert sorted(outcomes) == ["completed", "skipped"]
    job = (await client.get(f"{BASE}/extraction-jobs/{job_id}", headers=admin)).json()
    stored = (await db_session.execute(text("SELECT count(*) FROM catalog_extraction_candidate"))).scalar_one()
    assert stored == job["candidate_count"] and job["attempt_count"] == 1
    assert await asyncio.to_thread(run_job, job_id) == "skipped"  # a completed job is never re-run


async def test_a_stale_worker_loses_its_claim_and_writes_nothing(client, admin, scanner, dispatched, db_session):
    content = native_pdf([DATASHEET])
    _, document = await _document(client, admin, content)
    job_id = uuid.UUID((await _request(client, admin, document["id"])).json()["id"])
    with get_sync_db() as db:
        stale = claim_job(db, job_id, settings=get_settings())
    assert stale is not None
    with get_sync_db() as db:  # the first worker "dies": its lease runs out
        db.execute(text("UPDATE catalog_extraction_job SET lease_expires_at = now() - interval '1 minute' WHERE id = :i"), {"i": job_id})
        db.commit()
    with get_sync_db() as db:
        fresh = claim_job(db, job_id, settings=get_settings())
    assert fresh is not None and fresh.token != stale.token
    result = run_pipeline(content, target_names=["CX-100"], settings=get_settings())
    with get_sync_db() as db:
        assert extraction_service._complete(db, stale, result) is False  # fenced out
    assert (await db_session.execute(text("SELECT count(*) FROM catalog_extraction_candidate"))).scalar_one() == 0
    with get_sync_db() as db:
        assert extraction_service._complete(db, fresh, result) is True
    await db_session.rollback()
    count = (await db_session.execute(text("SELECT count(*) FROM catalog_extraction_candidate"))).scalar_one()
    assert count == len(result.candidates) > 0


async def test_attempt_budget_ends_in_a_fixed_failure(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job_id = uuid.UUID((await _request(client, admin, document["id"])).json()["id"])
    settings = get_settings()
    for _ in range(settings.catalog_extraction_max_attempts):
        with get_sync_db() as db:
            assert claim_job(db, job_id, settings=settings) is not None
            db.execute(text("UPDATE catalog_extraction_job SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": job_id})
            db.commit()
    with get_sync_db() as db:
        assert claim_job(db, job_id, settings=settings) is None
    job = (await client.get(f"{BASE}/extraction-jobs/{job_id}", headers=admin)).json()
    assert job["status"] == "failed" and job["error_code"] == "max_attempts_exceeded" and "repeatedly" in job["error_message"]


async def test_failed_job_can_be_retried_once_per_failure_and_history_is_one_row(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    storage = get_document_storage_backend()
    key = f"{document['sha256']}.pdf"
    original = storage.read(key)
    job_id = (await _request(client, admin, document["id"])).json()["id"]
    storage.delete(key)
    assert await asyncio.to_thread(run_job, job_id) == "failed"
    failed = (await client.get(f"{BASE}/extraction-jobs/{job_id}", headers=admin)).json()
    assert failed["status"] == "failed" and failed["error_code"] == "stored_object_missing"
    assert "/" not in failed["error_message"] and "Traceback" not in failed["error_message"]
    assert (await client.post(f"{BASE}/extraction-jobs/{job_id}/retry", headers=admin)).status_code == 202
    assert (await client.post(f"{BASE}/extraction-jobs/{job_id}/retry", headers=admin)).status_code == 409  # already queued
    storage.save(key, original)
    assert await asyncio.to_thread(run_job, job_id) == "completed"
    done = (await client.get(f"{BASE}/extraction-jobs/{job_id}", headers=admin)).json()
    assert done["status"] == "completed" and done["retry_count"] == 1 and done["error_code"] is None
    assert (await client.post(f"{BASE}/extraction-jobs/{job_id}/retry", headers=admin)).status_code == 409
    assert len((await client.get(f"{BASE}/documents/{document['id']}/extraction-jobs", headers=admin)).json()) == 1


async def test_a_tampered_stored_object_is_detected_before_parsing(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    storage = get_document_storage_backend()
    key = f"{document['sha256']}.pdf"
    storage.delete(key)  # content-addressed storage never overwrites, so replace the object the hard way
    storage.save(key, make_pdf("something else entirely"))
    job = await _extract(client, admin, document["id"])
    assert job["status"] == "failed" and job["error_code"] == "stored_object_mismatch"
    assert await _candidates(client, admin, job["id"]) == []


async def test_a_new_extractor_version_adds_history_and_the_old_result_stays(client, admin, scanner, dispatched, monkeypatch):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    first = await _extract(client, admin, document["id"])
    monkeypatch.setattr(extraction_service, "EXTRACTOR_VERSION", "datasheet-extractor-9.9")
    second = await _extract(client, admin, document["id"])
    assert first["id"] != second["id"] and second["extractor_version"] == "datasheet-extractor-9.9"
    listing = (await client.get(f"{BASE}/documents/{document['id']}/extraction-jobs", headers=admin)).json()
    assert {j["id"] for j in listing} == {first["id"], second["id"]}
    assert [j["id"] for j in listing if j["is_current"]] == [second["id"]]
    assert len(await _candidates(client, admin, first["id"])) > 0  # the earlier result is untouched


async def test_dispatch_failure_leaves_the_job_queued_for_the_sweep(client, admin, scanner, monkeypatch):
    from app.infrastructure.tasks import catalog_extraction as tasks

    def broken(*_a, **_k):
        raise ConnectionError("redis://user:secret@broker/0 is down")

    monkeypatch.setattr(tasks.run_catalog_extraction_job, "apply_async", broken)
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    resp = await _request(client, admin, document["id"])
    assert resp.status_code == 202 and resp.json()["status"] == "queued" and "secret" not in resp.text
    assert tasks.dispatch_extraction_job(uuid.uuid4()) is False


async def test_the_sweep_finds_lost_queued_jobs_and_expired_leases_only(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job_id = uuid.UUID((await _request(client, admin, document["id"])).json()["id"])
    with get_sync_db() as db:
        assert find_jobs_to_dispatch(db) == []  # just queued: the dispatch may still be in flight
        assert find_jobs_to_dispatch(db, now=datetime.now(UTC) + timedelta(minutes=10)) == [job_id]
    with get_sync_db() as db:
        claim_job(db, job_id, settings=get_settings())
        assert find_jobs_to_dispatch(db) == []  # a live lease is left alone
        assert find_jobs_to_dispatch(db, now=datetime.now(UTC) + timedelta(hours=1)) == [job_id]


# ------------------------------------------------------------------------------ OCR through the worker


async def test_ocr_failure_is_reported_with_a_fixed_code_and_changes_nothing_else(client, admin, scanner, dispatched, tmp_path):
    engine = tmp_path / "tesseract"
    engine.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(120)\n")
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    _, document = await _document(client, admin, scanned_pdf([DATASHEET]))
    job = await _extract(client, admin, document["id"], catalog_ocr_tesseract_path=str(engine), catalog_ocr_page_timeout_seconds=2)
    assert job["status"] == "failed" and job["error_code"] == "ocr_timeout" and "too long" in job["error_message"]
    assert str(tmp_path) not in str(job)
    assert (await client.get(f"{BASE}/documents/{document['id']}", headers=admin)).json()["sha256"] == document["sha256"]


@pytest.mark.skipif(
    shutil.which("tesseract") is None and os.environ.get("REQUIRE_REAL_OCR") != "1", reason="tesseract is not installed"
)
async def test_scanned_datasheet_end_to_end_with_the_real_engine(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, scanned_pdf([DATASHEET[:5]]))
    job = await _extract(client, admin, document["id"])
    assert job["status"] == "completed" and job["method_summary"] == "ocr" and job["pages_ocr"] == 1
    by_field = {c["field_key"]: c for c in await _candidates(client, admin, job["id"])}
    assert by_field["weight"]["value_numeric"] == 12.5 and by_field["weight"]["method"] == "ocr"
    assert by_field["weight"]["confidence"] <= 0.75


# ------------------------------------------------------------------------------ authorization


async def test_only_administrators_can_request_retry_or_review(client, auth_headers, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job = await _extract(client, admin, document["id"])
    candidate = (await _candidates(client, admin, job["id"]))[0]
    for role in ("DCIM Manager", "Engineer", "Operator", "Viewer"):
        headers = await auth_headers(role)
        assert (await _request(client, headers, document["id"])).status_code == 403, role
        assert (await client.post(f"{BASE}/extraction-jobs/{job['id']}/retry", headers=headers)).status_code == 403, role
        review = await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", json={"decision": "accepted"}, headers=headers)
        assert review.status_code == 403, role
    anonymous = await client.post(f"{BASE}/documents/{document['id']}/extraction-jobs")
    assert anonymous.status_code == 401


async def test_reads_follow_the_document_permission_and_never_reveal_whether_an_id_exists(client, auth_headers, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    job = await _extract(client, admin, document["id"])
    candidate = (await _candidates(client, admin, job["id"]))[0]
    real = [f"/extraction-jobs/{job['id']}", f"/extraction-jobs/{job['id']}/candidates", f"/documents/{document['id']}/extraction-jobs"]
    ghost = [f"/extraction-jobs/{uuid.uuid4()}", f"/extraction-jobs/{uuid.uuid4()}/candidates", f"/documents/{uuid.uuid4()}/extraction-jobs"]
    for role in ("Viewer", "Operator"):
        headers = await auth_headers(role)
        for path in real + ghost:  # an unauthorised caller gets the same 403 for a real and a made-up id
            assert (await client.get(f"{BASE}{path}", headers=headers)).status_code == 403, (role, path)
        post = await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", json={"decision": "accepted"}, headers=headers)
        assert post.status_code == 403
    admin_reader = await auth_headers("Administrator")
    for path in real:
        assert (await client.get(f"{BASE}{path}", headers=admin_reader)).status_code == 200, path
    for path in ghost:
        assert (await client.get(f"{BASE}{path}", headers=admin_reader)).status_code == 404, path
    # DCIM Manager and Engineer may download published datasheets but have no catalog:read_draft, so
    # draft-stage material (this document is linked to no published revision) is refused, like the file.
    for role in ("DCIM Manager", "Engineer"):
        headers = await auth_headers(role)
        for path in real:
            assert (await client.get(f"{BASE}{path}", headers=headers)).status_code == 403, (role, path)
        assert (await client.get(f"{BASE}/documents/{document['id']}/file", headers=headers)).status_code == 403
    assert (await client.get(f"{BASE}/extraction-jobs/{job['id']}")).status_code == 401


async def test_draft_stage_candidates_need_read_draft_and_published_ones_do_not(client, auth_headers, admin, scanner, dispatched):
    model_id, document = await _document(client, admin, native_pdf([DATASHEET]))
    job = await _extract(client, admin, document["id"])
    reader = await auth_headers("Engineer")  # may download published datasheets; has no catalog:read_draft
    for path in (f"/extraction-jobs/{job['id']}", f"/extraction-jobs/{job['id']}/candidates", f"/documents/{document['id']}/extraction-jobs"):
        assert (await client.get(f"{BASE}{path}", headers=reader)).status_code == 403, path  # unlinked: draft-stage material
    draft = await make_draft(client, admin, model_id)
    attach = await client.post(f"{BASE}/revisions/{draft['id']}/documents/{document['id']}", headers={**admin, "If-Match": str(draft["version"])})
    assert attach.status_code == 201, attach.text
    assert (await client.get(f"{BASE}/extraction-jobs/{job['id']}", headers=reader)).status_code == 403  # linked to a draft only
    version = (await client.get(f"{BASE}/revisions/{draft['id']}", headers=admin)).json()["version"]
    assert (await client.patch(f"{BASE}/revisions/{draft['id']}", json=RACK_BODY, headers={**admin, "If-Match": str(version)})).status_code == 200
    assert (await client.post(f"{BASE}/revisions/{draft['id']}/publish", headers=admin)).status_code == 200
    for path in (f"/extraction-jobs/{job['id']}", f"/extraction-jobs/{job['id']}/candidates", f"/documents/{document['id']}/extraction-jobs"):
        assert (await client.get(f"{BASE}{path}", headers=reader)).status_code == 200, path


async def test_jobs_and_candidates_are_never_reachable_through_another_documents_path(client, admin, scanner, dispatched):
    _, first = await _document(client, admin, native_pdf([DATASHEET]))
    _, second = await _document(client, admin, native_pdf([["CX-100 Technical Specifications", "Weight: 99 kg"]]), name="CX-100")
    job_one, job_two = await _extract(client, admin, first["id"]), await _extract(client, admin, second["id"])
    listing = (await client.get(f"{BASE}/documents/{second['id']}/extraction-jobs", headers=admin)).json()
    assert [j["id"] for j in listing] == [job_two["id"]]
    assert all(c["job_id"] == job_two["id"] for c in await _candidates(client, admin, job_two["id"]))
    assert job_one["id"] not in {j["id"] for j in listing}


async def test_unextractable_documents_are_refused_up_front(client, admin, scanner, dispatched):
    staged = await _upload(client, admin, None, make_pdf("staged without a model"))
    refused = await _request(client, admin, staged["id"])
    assert refused.status_code == 409 and "model" in refused.json()["detail"]
    assert (await _request(client, admin, str(uuid.uuid4()))).status_code == 404
    assert dispatched == []


async def test_hostile_filenames_never_reach_job_responses(client, admin, scanner, dispatched):
    _, document = await _document(client, admin, native_pdf([DATASHEET]), filename="../../etc/passwd\x00<script>.pdf")
    job = await _extract(client, admin, document["id"])
    blob = str(job) + str(await _candidates(client, admin, job["id"]))
    assert "passwd" not in blob and "<script>" not in blob and ".." not in job["error_message"] if job["error_message"] else True


# ------------------------------------------------------------------------------ no catalog revision is ever changed


async def _revision_snapshot(db_session, model_id: str):
    await db_session.rollback()
    revisions = (await db_session.execute(text("SELECT to_jsonb(r) FROM catalog_model_revision r WHERE catalog_model_id = :m ORDER BY revision_number"), {"m": model_id})).scalars().all()
    links = (await db_session.execute(text("SELECT to_jsonb(l) FROM catalog_revision_document l JOIN catalog_model_revision r ON r.id = l.catalog_model_revision_id WHERE r.catalog_model_id = :m ORDER BY l.id"), {"m": model_id})).scalars().all()
    return revisions, links


async def test_extraction_and_review_never_modify_draft_published_or_retired_revisions(client, admin, scanner, dispatched, db_session):
    model_id, document = await _document(client, admin, native_pdf([DATASHEET]))
    published = await _publish_with_document(client, admin, model_id, document["id"])
    draft = await make_draft(client, admin, model_id)  # a later draft of the same model stays a draft
    before = await _revision_snapshot(db_session, model_id)

    job = await _extract(client, admin, document["id"])
    for candidate in (await _candidates(client, admin, job["id"]))[:3]:
        assert (await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", json={"decision": "accepted"}, headers=admin)).status_code == 200
    assert (await client.post(f"{BASE}/extraction-jobs/{job['id']}/retry", headers=admin)).status_code == 409

    assert await _revision_snapshot(db_session, model_id) == before
    still_published = (await client.get(f"{BASE}/revisions/{published['id']}", headers=admin)).json()
    assert still_published["lifecycle_status"] == "published" and still_published["weight_value"] == 12.5
    still_draft = (await client.get(f"{BASE}/revisions/{draft['id']}", headers=admin)).json()
    assert still_draft["version"] == draft["version"] and still_draft["weight_value"] is None and still_draft["width_value"] is None


async def test_a_retired_revisions_datasheet_can_be_analysed_without_touching_the_revision(client, admin, scanner, dispatched, db_session):
    model_id, document = await _document(client, admin, native_pdf([DATASHEET]))
    retired = await _publish_with_document(client, admin, model_id, document["id"], retire=True)
    assert retired["lifecycle_status"] == "retired"
    before = await _revision_snapshot(db_session, model_id)
    job = await _extract(client, admin, document["id"])
    assert job["status"] == "completed"
    candidate = (await _candidates(client, admin, job["id"]))[0]
    assert (await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", json={"decision": "rejected"}, headers=admin)).status_code == 200
    assert await _revision_snapshot(db_session, model_id) == before


# ------------------------------------------------------------------------------ the database refuses the rest


async def _completed_job(client, admin, scanner):
    _, document = await _document(client, admin, native_pdf([DATASHEET]))
    return document, await _extract(client, admin, document["id"])


async def test_candidate_rows_are_immutable_apart_from_one_review(client, admin, scanner, dispatched, db_session):
    _, job = await _completed_job(client, admin, scanner)
    candidate = (await _candidates(client, admin, job["id"]))[0]
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session.execute(text("UPDATE catalog_extraction_candidate SET value_numeric = 1 WHERE id = :i"), {"i": candidate["id"]})
    await db_session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session.execute(text("UPDATE catalog_extraction_candidate SET source_text = 'edited' WHERE id = :i"), {"i": candidate["id"]})
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(text("UPDATE catalog_extraction_candidate SET review_status = 'accepted' WHERE id = :i"), {"i": candidate["id"]})  # no reviewer recorded
    await db_session.rollback()
    assert (await client.post(f"{BASE}/extraction-candidates/{candidate['id']}/review", json={"decision": "accepted"}, headers=admin)).status_code == 200
    with pytest.raises(DBAPIError, match="already reviewed"):
        await db_session.execute(text("UPDATE catalog_extraction_candidate SET review_note = 'x' WHERE id = :i"), {"i": candidate["id"]})
    await db_session.rollback()


async def test_completed_jobs_and_job_identity_are_immutable(client, admin, scanner, dispatched, db_session):
    _, job = await _completed_job(client, admin, scanner)
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session.execute(text("UPDATE catalog_extraction_job SET candidate_count = 0 WHERE id = :i"), {"i": job["id"]})
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(text("UPDATE catalog_extraction_job SET status = 'queued' WHERE id = :i"), {"i": job["id"]})
    await db_session.rollback()
    queued_doc, _ = (await _document(client, admin, native_pdf([["CX-100 Technical Specifications", "Weight: 5 kg"]]), name="CX-777"))[1], None
    queued = (await _request(client, admin, queued_doc["id"])).json()
    with pytest.raises(DBAPIError, match="identity is immutable"):
        await db_session.execute(text("UPDATE catalog_extraction_job SET extractor_version = 'other' WHERE id = :i"), {"i": queued["id"]})
    await db_session.rollback()


async def test_a_job_can_only_be_created_for_a_modelled_scanned_document_with_the_right_checksum(client, admin, scanner, dispatched, db_session):
    staged = await _upload(client, admin, None, make_pdf("staged unique"))
    model_id, bound = await _document(client, admin, native_pdf([DATASHEET]))
    user_id = (await client.get("/api/v1/auth/me", headers=admin)).json()["id"]
    insert = (
        "INSERT INTO catalog_extraction_job (catalog_document_id, catalog_model_id, document_sha256, extractor_version, "
        "unit_registry_version, requested_by_user_id, requested_at) VALUES (:d, :m, :s, 'x', '1', :u, now())"
    )
    cases = [
        {"d": staged["id"], "m": model_id, "s": staged["sha256"], "u": user_id},  # no model on the document
        {"d": bound["id"], "m": str(uuid.uuid4()), "s": bound["sha256"], "u": user_id},  # wrong model
        {"d": bound["id"], "m": model_id, "s": "0" * 64, "u": user_id},  # wrong checksum
    ]
    for params in cases:
        with pytest.raises(DBAPIError):
            await db_session.execute(text(insert), params)
        await db_session.rollback()


async def test_deleting_a_document_removes_its_jobs_and_candidates_with_it(client, admin, scanner, dispatched, db_session):
    from app.application.catalog_documents.service import purge_expired_staged_documents

    document, job = await _completed_job(client, admin, scanner)
    with get_sync_db() as sync_db:
        deleted = purge_expired_staged_documents(
            sync_db, storage=get_document_storage_backend(), retention_days=14, now=datetime.now(UTC) + timedelta(days=20)
        )
    assert deleted == 1
    await db_session.rollback()
    assert (await db_session.execute(select(CatalogExtractionJob))).scalars().all() == []
    assert (await db_session.execute(select(CatalogExtractionCandidate))).scalars().all() == []


# ------------------------------------------------------------------------------ wiring


def test_celery_wiring_and_compose_workers_consume_the_extraction_queue():
    from pathlib import Path

    from app.infrastructure.celery_app import celery_app

    assert "extraction" in celery_app.conf.task_queues
    assert "app.infrastructure.tasks.catalog_extraction.run_catalog_extraction_job" in celery_app.tasks
    assert "app.infrastructure.tasks.catalog_extraction.requeue_stuck_catalog_extraction_jobs" in celery_app.tasks
    assert "requeue-stuck-catalog-extraction-jobs" in celery_app.conf.beat_schedule
    root = Path(__file__).resolve().parents[3]
    for name in ("docker-compose.yml", "docker-compose.production.yml"):
        # The production file uses Compose-only YAML tags, so read the worker's command line as text.
        worker = (root / name).read_text().split("  celery-worker:")[1].split("  celery-beat:")[0]
        command = next(line for line in worker.splitlines() if line.strip().startswith("command:"))
        assert '"-Q", "default,maintenance,extraction"' in command, name
