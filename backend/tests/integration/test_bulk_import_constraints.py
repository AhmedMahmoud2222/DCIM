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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.bulk_import import service as bulk_import_service
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


# ------------------------------------------------------------------------ ROUND 4 finding (fallback ownership race)


async def test_two_sessions_racing_stale_fallback_against_a_live_claim_a_wins_the_lock(client, auth_headers, db_engine):
    """SEC (Codex PR #50 review, ROUND 4): `_mark_commit_failed_if_still_owner`
    (service.py) previously read the job row with a plain `db.get(BulkImportJob, job_id)`
    -- no lock -- compared its (possibly soon-to-be-stale) `commit_lease_id`/`status`
    snapshot in Python, then wrote. A second delivery's claim UPDATE could commit in the
    gap between that read and this function's own eventual write, and the write -- a
    plain UPDATE-by-primary-key with no `commit_lease_id` predicate of its own -- would
    then silently clobber whatever the second delivery had already done. This is a
    genuine two-session interleaving where delivery B's claim attempt (via the real
    `run_commit`, not a hand-rolled reimplementation of its claim UPDATE) happens WHILE
    delivery A is inside its own fallback -- not before A starts and not only after A has
    already reached a terminal state (the earlier two-worker fencing test only exercised
    the latter). This is lock order 1: A reaches the row lock first, so B's claim
    genuinely blocks in Postgres until A finishes. `_mark_commit_failed_if_still_owner`
    fuses its lock acquisition, writes, and final `db.commit()` into one coroutine with no
    external seam to pause it from outside, and A's own execution -- once started -- runs
    end to end in a few milliseconds, far too fast for a fixed `asyncio.sleep()` before
    dispatching B to reliably land B's dispatch inside A's still-open transaction. So this
    test instruments session_a's own `execute()` (the real method the real function's own
    `with_for_update` read goes through -- not a bespoke reimplementation of it) to signal
    an event the instant that first real DB round trip returns, and only dispatches B once
    that fires -- guaranteeing B starts while A's transaction (and, once fixed, its row
    lock) is still open, deterministically, without relying on wall-clock luck."""
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)
    asset_tag = f"RACK-{uuid.uuid4().hex[:8]}"

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        user_id = await _make_bulk_import_user(setup)
        lease_id_a = uuid.uuid4()
        job = BulkImportJob(
            import_type="rack", mode="create_only", status="committing", uploaded_by_user_id=user_id,
            original_filename="a.xlsx", file_hash="a" * 64, file_size_bytes=1, row_count=1, valid_row_count=1,
            commit_lease_id=lease_id_a, commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        setup.add(job)
        await setup.flush()
        job_id = job.id
        row = BulkImportRow(
            job_id=job_id, row_number=1, status="valid", action="create",
            raw_data={
                "asset_tag": asset_tag, "rack_name": "Stuck Row", "manufacturer": model["manufacturer"],
                "model_name": model["model_name"], "revision_number": None, "site_code": room["site_code"],
                "building_code": room["building_code"], "floor_level": room["floor_level"],
                "room_code": room["room_code"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0, "owner": None, "notes": None,
            },
        )
        setup.add(row)
        await setup.commit()

    session_a = session_factory()
    session_b = session_factory()
    real_execute = session_a.execute
    a_has_read = asyncio.Event()
    a_may_continue = asyncio.Event()

    async def _execute_pause_after_first_call(*args, **kwargs):
        result = await real_execute(*args, **kwargs)
        # Genuinely suspend A's coroutine right after its own first real DB round trip (its
        # `with_for_update` read) returns -- before the ownership check's writes are even
        # set on the Python object, let alone flushed. A's transaction (and, once fixed,
        # its row lock) stays open across this suspension exactly as it would across any
        # other slow `await` in real production code; only the timing is test-controlled.
        if not a_has_read.is_set():
            a_has_read.set()
            await a_may_continue.wait()
        return result

    session_a.execute = _execute_pause_after_first_call
    try:
        # A's real fallback -- unmodified production code, its own `with_for_update` read
        # is the first statement it issues.
        task_a = asyncio.create_task(
            bulk_import_service._mark_commit_failed_if_still_owner(session_a, job_id, lease_id_a)
        )
        await a_has_read.wait()  # A has read; A is now paused, its transaction still open

        # B's claim attempt is real production code (run_commit's own atomic claim UPDATE
        # is its very first statement) -- not a bespoke reimplementation. Dispatched only
        # now, deterministically while A's transaction is still open, rather than at some
        # unproven point relative to it. `shield` keeps task_b alive (not cancelled) if the
        # timeout below fires, so it can still be awaited to a real conclusion afterward.
        task_b = asyncio.create_task(bulk_import_service.run_commit(session_b, job_id))
        try:
            await asyncio.wait_for(asyncio.shield(task_b), timeout=0.3)
            b_finished_while_a_was_paused = True
        except TimeoutError:
            b_finished_while_a_was_paused = False
        assert not b_finished_while_a_was_paused, (
            "B's claim must genuinely block on A's held row lock while A's transaction is "
            "still open -- not race ahead and commit the row while A still believes (from "
            "its now-stale in-memory snapshot) that it owns the job"
        )

        a_may_continue.set()  # let A finish: row -> failed, job -> committed_with_errors, commits -- releasing the lock
        await task_a
        await task_b  # B's now-unblocked claim UPDATE re-reads the post-A row: status != 'committing' -> 0 rows -> clean no-op
    finally:
        session_a.execute = real_execute
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        job_after = await verify.get(BulkImportJob, job_id)
        assert job_after.status == "committed_with_errors", "A's fallback must reach a real terminal status"
        assert job_after.failed_row_count == 1
        assert job_after.commit_lease_id == lease_id_a, "B's blocked claim must never have taken effect"
        assert job_after.report_storage_key is None, "the fallback path never builds a report"

        row_after = (await verify.execute(text("SELECT status FROM bulk_import_row WHERE job_id = :id"), {"id": str(job_id)})).scalar_one()
        assert row_after == "failed", "the row A's fallback marked must stay failed, not be silently reprocessed by B"

        audit_count = (
            await verify.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
                {"id": str(job_id)},
            )
        ).scalar_one()
        # _mark_commit_failed_if_still_owner is a fallback finalize, not the normal
        # success-path finalize (service.py:425) -- it never writes a job-level audit row
        # itself. What matters here is that there are zero, not one: B's blocked claim
        # must never have reached that success path and written its own.
        assert audit_count == 0, "B's blocked claim must never have taken effect and written its own audit row"


async def test_two_sessions_stale_fallback_after_a_live_claim_already_won_is_a_clean_noop(db_engine):
    """SEC (Codex PR #50 review, ROUND 4): lock order 2 -- delivery B claims and finishes
    the job for real FIRST (an ordinary, uncontested reclaim of A's expired lease), and
    only then does A's own stale fallback (still carrying A's original, now-superseded
    `lease_id`) get invoked. A's fenced ownership check must see B's live token and the
    job's new terminal status and cleanly no-op -- including rolling back its own
    transaction -- rather than raising or clobbering any part of what B already
    committed (status, counts, lease, report). Uses only direct-ORM/db_engine setup (no
    client/auth_headers) since this job has zero rows and never touches the HTTP API."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup:
        user_id = await _make_bulk_import_user(setup)
        lease_id_a = uuid.uuid4()
        # Zero rows: this job's only purpose is to prove the claim/finalize/no-op
        # interleaving, not to exercise row-commit logic (already covered elsewhere) --
        # B's real run_commit() still claims, finds an empty batch, builds a real (empty)
        # report, and finalizes to 'committed' entirely through production code.
        job = BulkImportJob(
            import_type="rack", mode="create_only", status="committing", uploaded_by_user_id=user_id,
            original_filename="b.xlsx", file_hash="b" * 64, file_size_bytes=1, row_count=0, valid_row_count=0,
            commit_lease_id=lease_id_a, commit_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        setup.add(job)
        await setup.commit()
        job_id = job.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        # B reclaims A's expired lease and finishes the (empty) job for real, uncontested.
        await bulk_import_service.run_commit(session_b, job_id)

        async with session_factory() as check:
            job_after_b = await check.get(BulkImportJob, job_id)
            assert job_after_b.status == "committed"
            lease_id_b = job_after_b.commit_lease_id
            assert lease_id_b != lease_id_a
            report_key_after_b = job_after_b.report_storage_key
            assert report_key_after_b is not None

        # A's own, separately-triggered fallback finally runs, still carrying the
        # original, now-stale lease_id_a -- must be a clean no-op.
        await bulk_import_service._mark_commit_failed_if_still_owner(session_a, job_id, lease_id_a)
        # The function's own no-op path already calls db.rollback() -- confirm the
        # session is left clean and usable, not stuck in a broken transaction.
        await session_a.execute(text("SELECT 1"))
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        job_final = await verify.get(BulkImportJob, job_id)
        assert job_final.status == "committed", "A's no-op fallback must never revert B's terminal status"
        assert job_final.commit_lease_id == lease_id_b, "A's no-op fallback must never touch B's lease token"
        assert job_final.report_storage_key == report_key_after_b, "A's no-op fallback must never touch B's report"
        assert job_final.failed_row_count == 0, "A's no-op fallback must never mark any row failed"

        audit_count = (
            await verify.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'rack.bulk_import.commit_job' AND entity_id = :id"),
                {"id": str(job_id)},
            )
        ).scalar_one()
        assert audit_count == 1, "only B's audit row must exist -- A's no-op must never write a second one"
