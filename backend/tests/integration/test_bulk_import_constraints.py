"""Direct proof that the equipment_placement GiST exclusion constraint (migration
0004_phase2_physical_spatial_model's `no_front_overlap`/`no_rear_overlap`) is what
authoritatively stops a same-U-range double-commit — and, critically, that one row's
resulting IntegrityError is isolated to its own SAVEPOINT and never rolls back its
siblings or crashes the job/transaction (app/application/bulk_import/service.py::
run_commit). Three rows in one batch: two conflict with each other, one is entirely
independent — exactly two must commit (the independent row, plus whichever of the
conflicting pair won the race to insert first) and exactly one must fail, and the job
must still reach a clean terminal status rather than hanging or 500ing."""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.bulk_import.commit import rack as commit_rack
from app.application.bulk_import.resolvers import RowRejected
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.domain.physical.models import Rack
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api._phase2_helpers import create_rack
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model

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

    # SEC (Codex PR #50 review, finding #1): run_commit now expects the job to already be
    # 'committing' -- only the real POST .../commit endpoint's atomic UPDATE ...
    # WHERE status='validated' puts it there. No worker consumes the broker queue in this
    # test environment, so the dispatched task is then drained synchronously.
    commit_resp = await client.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers)
    assert commit_resp.status_code == 202, commit_resp.text
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


# ------------------------------------------------------------------------ ROUND 2 finding #3


async def _make_bulk_import_user(session) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(), email=f"toctou-{uuid.uuid4().hex[:8]}@example.com", full_name="TOCTOU Test User",
        password_hash=hash_password("correct horse battery staple"),
    )
    session.add(user)
    await session.flush()
    return user.id


async def test_two_sessions_racing_rack_update_commit_row_toctou_is_rejected_not_lost(
    client, auth_headers, db_engine,
):
    """SEC (Codex PR #50 review, ROUND 2, finding #3): commit/rack.py's update-mode branch
    previously read the target rack with a plain `db.get(Rack, asset.id)` (no lock) before
    comparing `row.expected_version` -- a genuine two-session interleaving (not just
    "mutate before commit starts", which the GiST-exclusion test above already covers for
    a different constraint) could let a second transaction's stale-version write proceed
    anyway between the first's read and its eventual commit. `with_for_update=True` closes
    this exactly the way `lock_draft_revision_for_edit`
    (app/application/catalog_designer_service.py) already does for the same class of race
    in this codebase: session B's own lock attempt genuinely blocks on session A's still-
    open transaction, then re-reads the post-A version and correctly rejects."""
    headers = await auth_headers("Engineer")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

    # Create the rack for real via the ordinary create-only bulk-import path (reuses
    # existing infrastructure rather than hand-building ManagedAsset/Rack/RackPlacement
    # rows directly), so it has a real placement and a real starting version.
    create_content = build_workbook(
        RACK_HEADERS,
        [[asset_tag, "Original Name", model["manufacturer"], model["model_name"], "", room["site_code"],
          room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]],
    )
    upload = await client.post(
        "/api/v1/racks/import-jobs?mode=create_only",
        files={"file": ("racks.xlsx", create_content, "application/octet-stream")}, headers=headers,
    )
    job = upload.json()
    parse_and_validate_bulk_import_job.run(job["id"])
    commit_resp = await client.post(f"/api/v1/import-jobs/{job['id']}/commit", headers=headers)
    assert commit_resp.status_code == 202, commit_resp.text
    commit_bulk_import_job.run(job["id"])

    racks = (await client.get("/api/v1/racks", headers=headers)).json()["items"]
    rack_out = next(r for r in racks if r["asset_tag"] == asset_tag)
    rack_id = uuid.UUID(rack_out["id"])
    starting_version = rack_out["version"]
    assert starting_version == 1

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        user_id = await _make_bulk_import_user(setup)
        job_a = BulkImportJob(
            import_type="rack", mode="update_existing", status="committing", uploaded_by_user_id=user_id,
            original_filename="a.xlsx", file_hash="a" * 64, file_size_bytes=1,
        )
        job_b = BulkImportJob(
            import_type="rack", mode="update_existing", status="committing", uploaded_by_user_id=user_id,
            original_filename="b.xlsx", file_hash="b" * 64, file_size_bytes=1,
        )
        setup.add_all([job_a, job_b])
        await setup.commit()
        job_a_id, job_b_id = job_a.id, job_b.id

    def _raw_row(name: str) -> dict:
        return {
            "asset_tag": asset_tag, "rack_name": name, "manufacturer": model["manufacturer"],
            "model_name": model["model_name"], "revision_number": None, "site_code": room["site_code"],
            "building_code": room["building_code"], "floor_level": room["floor_level"], "room_code": room["room_code"],
            "x_mm": 0, "y_mm": 0, "rotation_deg": 0, "owner": None, "notes": None,
        }

    session_a = session_factory()
    session_b = session_factory()
    try:
        job_a_reread = await session_a.get(BulkImportJob, job_a_id)
        row_a = BulkImportRow(
            job_id=job_a_id, row_number=1, status="valid", action="update",
            raw_data=_raw_row("A's New Name"), expected_version=starting_version,
        )
        session_a.add(row_a)
        await session_a.flush()

        # A runs commit_row() fully (its own with_for_update lock + every write) but does
        # not commit yet -- the lock is held for the rest of A's open transaction.
        result_a = await commit_rack.commit_row(session_a, job=job_a_reread, row=row_a, mode="update_existing")
        assert result_a.status == "committed"

        job_b_reread = await session_b.get(BulkImportJob, job_b_id)
        row_b = BulkImportRow(
            job_id=job_b_id, row_number=1, status="valid", action="update",
            # B's own If-Match: the version as it was BEFORE A's (not yet committed) write --
            # exactly "two admins editing the same rack, neither aware of the other".
            raw_data=_raw_row("B's New Name"), expected_version=starting_version,
        )
        session_b.add(row_b)
        await session_b.flush()

        task_b = asyncio.create_task(
            commit_rack.commit_row(session_b, job=job_b_reread, row=row_b, mode="update_existing")
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should still be blocked on A's with_for_update lock"

        await session_a.commit()

        # B's blocked call now proceeds, re-reads the committed (post-A) version via its
        # own with_for_update lock, and correctly detects that its own expected_version is
        # now stale -- it must never silently apply its own write on top of A's.
        with pytest.raises(RowRejected):
            await task_b
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        reread = await verify.get(Rack, rack_id)
        assert reread.version == starting_version + 1, "exactly one increment -- B's rejected attempt must not count"
        assert reread.name == "A's New Name", "A's edit must survive, never silently overwritten or lost"
