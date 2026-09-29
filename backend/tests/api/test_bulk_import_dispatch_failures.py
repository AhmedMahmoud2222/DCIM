"""SEC (Codex PR #50 review, finding #7): `.delay(...)` itself can fail synchronously
(broker unreachable, serialization error) — without handling, the caller would receive a
202 for a job no worker will ever pick up. Each of the four dispatch call sites
(app/api/v1/racks.py / equipment.py / catalog_designer.py upload endpoints, and
app/api/v1/bulk_import.py's commit endpoint) must instead surface a clean 502 and leave
the job's own status in a sane, non-stuck state — never claiming to be queued/in-progress
forever.

`parse_and_validate_bulk_import_job`/`commit_bulk_import_job` are the same Celery Task
singleton object regardless of which module imported it, so monkeypatching `.delay` on
the object reached via `app.infrastructure.tasks.bulk_import` affects every call site
that dispatches it (app/api/v1/bulk_import.py's `dispatch_parse_job_or_fail` helper and
the commit endpoint)."""

import hashlib
import uuid

from sqlalchemy import text as sql_text

import app.infrastructure.tasks.bulk_import as bulk_import_tasks
from app.infrastructure.tasks.bulk_import import parse_and_validate_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_catalog import CATALOG_HEADERS, _base_row
from tests.api.test_bulk_import_equipment import EQUIPMENT_HEADERS, _create_equipment_model, _floor_standing_row
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model


def _raise_broker_unreachable(*args, **kwargs):
    raise RuntimeError("broker unreachable (simulated)")


async def _job_by_file_hash(db_session, content: bytes) -> dict:
    file_hash = hashlib.sha256(content).hexdigest()
    row = (
        await db_session.execute(
            sql_text("SELECT id, status, rejection_reason FROM bulk_import_job WHERE file_hash = :h"), {"h": file_hash}
        )
    ).mappings().one()
    return dict(row)


async def test_rack_upload_dispatch_failure_returns_502_and_job_ends_failed_parse(
    client, auth_headers, db_session, monkeypatch
):
    monkeypatch.setattr(bulk_import_tasks.parse_and_validate_bulk_import_job, "delay", _raise_broker_unreachable)

    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    content = build_workbook(
        RACK_HEADERS,
        [[f"RACK-{uuid.uuid4().hex[:8]}", "Rack", model["manufacturer"], model["model_name"], "", room["site_code"],
          room["building_code"], room["floor_level"], room["room_code"], "", "", "", "", ""]],
    )
    resp = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert resp.status_code == 502, resp.text

    job = await _job_by_file_hash(db_session, content)
    assert job["status"] == "failed_parse", job
    assert job["rejection_reason"]


async def test_equipment_upload_dispatch_failure_returns_502_and_job_ends_failed_parse(
    client, auth_headers, db_session, monkeypatch
):
    monkeypatch.setattr(bulk_import_tasks.parse_and_validate_bulk_import_job, "delay", _raise_broker_unreachable)

    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_equipment_model(client, auth_headers)
    content = build_workbook(EQUIPMENT_HEADERS, [_floor_standing_row(f"EQ-{uuid.uuid4().hex[:8]}", model, room)])
    resp = await client.post(
        "/api/v1/equipment/import-jobs?mode=create_only",
        files={"file": ("equipment.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert resp.status_code == 502, resp.text

    job = await _job_by_file_hash(db_session, content)
    assert job["status"] == "failed_parse", job
    assert job["rejection_reason"]


async def test_catalog_upload_dispatch_failure_returns_502_and_job_ends_failed_parse(
    client, auth_headers, db_session, monkeypatch
):
    monkeypatch.setattr(bulk_import_tasks.parse_and_validate_bulk_import_job, "delay", _raise_broker_unreachable)

    headers = await auth_headers("Administrator")
    content = build_workbook(
        CATALOG_HEADERS, [_base_row(f"Mfr-{uuid.uuid4().hex[:8]}", f"Model-{uuid.uuid4().hex[:8]}")]
    )
    resp = await client.post(
        "/api/v1/catalog/import-jobs?mode=create_only",
        files={"file": ("catalog.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert resp.status_code == 502, resp.text

    job = await _job_by_file_hash(db_session, content)
    assert job["status"] == "failed_parse", job
    assert job["rejection_reason"]


async def test_commit_dispatch_failure_returns_502_and_job_reverts_to_validated(
    client, auth_headers, db_session, monkeypatch
):
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    content = build_workbook(
        RACK_HEADERS,
        [[f"RACK-{uuid.uuid4().hex[:8]}", "Rack", model["manufacturer"], model["model_name"], "", room["site_code"],
          room["building_code"], room["floor_level"], room["room_code"], "", "", "", "", ""]],
    )
    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    assert upload.status_code == 202, upload.text
    job = upload.json()

    parse_and_validate_bulk_import_job.run(job["id"])
    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "validated", job_status

    monkeypatch.setattr(bulk_import_tasks.commit_bulk_import_job, "delay", _raise_broker_unreachable)

    resp = await client.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers)
    assert resp.status_code == 502, resp.text

    reverted = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    # Never left claiming to be in-progress ('committing') forever — reverted back to the
    # state it can actually be retried from.
    assert reverted["status"] == "validated", reverted

    # And it really is retryable: a normal commit (dispatch succeeding this time) still
    # works end to end.
    monkeypatch.undo()
    retry = await client.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers)
    assert retry.status_code == 202, retry.text

    from app.infrastructure.tasks.bulk_import import commit_bulk_import_job

    commit_bulk_import_job.run(job["id"])
    final = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert final["status"] == "committed", final
