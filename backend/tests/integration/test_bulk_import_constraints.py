"""Direct proof that the equipment_placement GiST exclusion constraint (migration
0004_phase2_physical_spatial_model's `no_front_overlap`/`no_rear_overlap`) is what
authoritatively stops a same-U-range double-commit — and, critically, that one row's
resulting IntegrityError is isolated to its own SAVEPOINT and never rolls back its
siblings or crashes the job/transaction (app/application/bulk_import/service.py::
run_commit). Three rows in one batch: two conflict with each other, one is entirely
independent — exactly two must commit (the independent row, plus whichever of the
conflicting pair won the race to insert first) and exactly one must fail, and the job
must still reach a clean terminal status rather than hanging or 500ing."""

import uuid

from app.domain.bulk_import.models import BulkImportRow
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api._phase2_helpers import create_rack

EQUIPMENT_HEADERS = [
    "asset_tag", "hostname", "manufacturer", "model_name", "revision_number", "placement_type", "site_code",
    "building_code", "floor_level", "room_code", "rack_asset_tag", "u_start", "u_end", "side", "ip_address",
    "mac_address", "owner", "service", "environment", "notes", "lifecycle_status",
]


async def _create_equipment_model(client, auth_headers) -> dict:
    admin_headers = await auth_headers("Administrator")
    model_name = f"EM-{uuid.uuid4().hex[:8]}"
    model = (
        await client.post(
            "/api/v1/equipment-models", json={"manufacturer": "Acme", "model_name": model_name}, headers=admin_headers,
        )
    ).json()
    revision = (
        await client.post(f"/api/v1/equipment-models/{model['id']}/revisions", json={}, headers=admin_headers)
    ).json()
    return {"manufacturer": "Acme", "model_name": model_name, "revision_id": revision["id"]}


async def test_gist_exclusion_constraint_isolates_one_conflicting_row_without_crashing_the_batch(client, auth_headers, db_session):
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_equipment_model(client, auth_headers)
    rack = await create_rack(client, headers, auth_headers, room_id=room["room_id"])

    def _row(tag, u_start, u_end):
        return [
            tag, tag, model["manufacturer"], model["model_name"], "", "rack_mounted", room["site_code"],
            room["building_code"], room["floor_level"], room["room_code"], rack["asset_tag"], u_start, u_end, "front",
            "", "", "", "", "", "", "planned",
        ]

    conflict_a, conflict_b, independent = (
        f"EQ-{uuid.uuid4().hex[:8]}", f"EQ-{uuid.uuid4().hex[:8]}", f"EQ-{uuid.uuid4().hex[:8]}",
    )
    content = build_workbook(
        EQUIPMENT_HEADERS,
        [_row(conflict_a, 10, 12), _row(conflict_b, 10, 12), _row(independent, 20, 22)],
    )
    upload = await client.post(
        "/api/v1/equipment/import-jobs?mode=create_only",
        files={"file": ("equipment.xlsx", content, "application/octet-stream")}, headers=headers,
    )
    job = upload.json()
    parse_and_validate_bulk_import_job.run(job["id"])
    commit_bulk_import_job.run(job["id"])

    job_status = (await client.get(f"/api/v1/import-jobs/{job['id']}", headers=headers)).json()
    assert job_status["status"] == "committed_with_errors", job_status
    assert job_status["committed_row_count"] == 2, "the two non-conflicting rows must both commit"
    assert job_status["failed_row_count"] == 1, "exactly one of the conflicting pair must fail"

    # Direct DB-level proof, bypassing the API's own summary: the job/row rows themselves
    # reflect this, and the independent row's equipment actually exists as a real,
    # placed asset — the transaction was never rolled back wholesale by the conflict.
    rows = (
        await db_session.execute(
            BulkImportRow.__table__.select().where(BulkImportRow.job_id == uuid.UUID(job["id"]))
        )
    ).mappings().all()
    statuses = {row["raw_data"]["asset_tag"]: row["status"] for row in rows}
    assert statuses[independent] == "committed"
    assert sorted([statuses[conflict_a], statuses[conflict_b]]) == ["committed", "failed"]

    equipment_list = (await client.get("/api/v1/equipment", headers=headers)).json()["items"]
    independent_equipment = next(e for e in equipment_list if e["asset_tag"] == independent)
    assert independent_equipment["placement"]["u_start"] == 20

    winner_tag = conflict_a if statuses[conflict_a] == "committed" else conflict_b
    loser_tag = conflict_b if winner_tag == conflict_a else conflict_a
    assert any(e["asset_tag"] == winner_tag for e in equipment_list)
    assert not any(e["asset_tag"] == loser_tag for e in equipment_list), "the losing row must not have a committed equipment row"
