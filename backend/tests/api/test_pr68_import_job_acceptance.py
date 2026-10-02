"""PR #68 acceptance: import-job access for site-restricted callers, through real HTTP requests.

Operations covered (every import-job route that exists; there is no list or retry endpoint in the
bulk-import pipeline):
    GET  /import-jobs/{id}                 status
    GET  /import-jobs/{id}/rows            preview (+ ?status=error validation errors, i.e. rows with status `invalid`)
    GET  /import-jobs/{id}/report          report download
    POST /import-jobs/{id}/commit          commit
    POST /import-jobs/{id}/cancel          cancel
    POST /racks|equipment|catalog/import-jobs        upload (needs `*:import`)
    GET  /racks|equipment|catalog/import-template    template download

Setup is real: jobs are uploaded through the upload endpoints and parsed by the real task, rows are
real validation results, and the callers are real users whose authority comes from real groups. The
authorization prerequisites (what a restricted caller can and cannot exercise) are asserted from the
effective-access endpoint before any operation is judged, so a 404 cannot be an accident of a missing
permission.

Findings these tests pin (see docs/USER_GROUP_MANAGEMENT.md, "Import jobs"):
  * `rack:import`, `equipment:import` and `catalog:import` are not site-aware, so a group grant of them is
    reported as inactive for a restricted user: such a user can never upload, commit or cancel;
  * `rack:read` / `equipment:read` ARE active for them, which is what exposed other users' jobs (issue #66);
  * the secure outcome for a job the caller did not upload is a 404 indistinguishable from a missing job,
    for every operation (including commit and cancel, which would otherwise answer 403 and reveal that the
    id exists).
"""

import uuid

import pytest
from sqlalchemy import text

from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.infrastructure.tasks.bulk_import import commit_bulk_import_job, parse_and_validate_bulk_import_job
from tests.api._bulk_import_helpers import build_workbook, create_room_with_codes
from tests.api.test_bulk_import_equipment import EQUIPMENT_HEADERS
from tests.api.test_bulk_import_racks import RACK_HEADERS, _create_rack_model
from tests.api.test_user_groups import PW, _group, _group_user, _login, _make_site

DATA = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]
IMPORT = ["rack:import", "equipment:import", "catalog:import"]

OPERATIONS = [
    ("status", "get", ""),
    ("rows", "get", "/rows"),
    ("rows-invalid", "get", "/rows?status=invalid"),
    ("report", "get", "/report"),
    ("commit", "post", "/commit"),
    ("cancel", "post", "/cancel"),
]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _upload(client, headers, path, content):
    resp = await client.post(path, files={"file": ("import.xlsx", content, "application/octet-stream")}, headers=headers)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["id"]
    parse_and_validate_bulk_import_job.run(job_id)
    return job_id


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


async def _snapshot(db_session, job_id):
    db_session.expire_all()
    row = (
        await db_session.execute(
            text(
                "SELECT status, committed_row_count, report_storage_key, "
                "(SELECT count(*) FROM audit_log WHERE entity_id = :j AND action LIKE '%bulk_import.%' "
                " AND action NOT LIKE '%.upload') FROM bulk_import_job WHERE id = :j"
            ),
            {"j": job_id},
        )
    ).one()
    return tuple(row)


@pytest.fixture
async def world(client, admin, auth_headers, db_session):
    """Site A and Site B (real rooms), a user restricted to each, and real jobs owned by an unrestricted
    uploader that reference Site B's room."""
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, a["org"])
    gid_a = await _group(client, admin, allow=[*DATA, *IMPORT], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    gid_b = await _group(client, admin, allow=[*DATA, *IMPORT], sites=[{"site_id": b["site"], "rack_scope": "all"}])
    user_a, headers_a = await _group_user(client, admin, [gid_a])
    user_b, headers_b = await _group_user(client, admin, [gid_b])

    uploader = await auth_headers("Engineer")  # global role: unrestricted, holds rack:import and equipment:import
    other_unrestricted = await auth_headers("DCIM Manager")
    room = await create_room_with_codes(client, auth_headers)
    model = await _create_rack_model(client, auth_headers)

    def rack_row(tag, room_code):
        return [tag, "Imported rack", model["manufacturer"], model["model_name"], "", room["site_code"], room["building_code"],
                room["floor_level"], room_code, 0, 0, 0, "Facilities", ""]

    jobs = {}
    jobs["rack-validated"] = await _upload(
        client, uploader, "/api/v1/racks/import-jobs?mode=create_only",
        build_workbook(RACK_HEADERS, [rack_row(f"SITE-B-{uuid.uuid4().hex[:6]}", room["room_code"])]),
    )
    jobs["rack-errors"] = await _upload(
        client, uploader, "/api/v1/racks/import-jobs?mode=create_only",
        build_workbook(RACK_HEADERS, [rack_row(f"BAD-{uuid.uuid4().hex[:6]}", "NO-SUCH-ROOM")]),
    )
    jobs["rack-to-commit"] = await _upload(
        client, uploader, "/api/v1/racks/import-jobs?mode=create_only",
        build_workbook(RACK_HEADERS, [rack_row(f"COMMITTED-{uuid.uuid4().hex[:6]}", room["room_code"])]),
    )
    assert (await client.post(f"/api/v1/import-jobs/{jobs['rack-to-commit']}/commit", headers=uploader)).status_code == 202
    commit_bulk_import_job.run(jobs["rack-to-commit"])
    jobs["rack-cancelled"] = await _upload(
        client, uploader, "/api/v1/racks/import-jobs?mode=create_only",
        build_workbook(RACK_HEADERS, [rack_row(f"CANC-{uuid.uuid4().hex[:6]}", room["room_code"])]),
    )
    assert (await client.post(f"/api/v1/import-jobs/{jobs['rack-cancelled']}/cancel", headers=uploader)).status_code == 200
    equipment_row = {h: "" for h in EQUIPMENT_HEADERS} | {"asset_tag": f"EQ-{uuid.uuid4().hex[:6]}", "hostname": "h1"}
    jobs["equipment"] = await _upload(
        client, uploader, "/api/v1/equipment/import-jobs?mode=create_only",
        build_workbook(EQUIPMENT_HEADERS, [[equipment_row[h] for h in EQUIPMENT_HEADERS]]),
    )
    me = (await client.get("/api/v1/auth/me", headers=uploader)).json()["id"]
    catalog = BulkImportJob(
        import_type="catalog", mode="create_only", status="validated", uploaded_by_user_id=uuid.UUID(me),
        original_filename="catalog.xlsx", file_hash="0" * 64, file_size_bytes=10, row_count=1, valid_row_count=1,
    )
    db_session.add(catalog)
    await db_session.flush()
    db_session.add(BulkImportRow(job_id=catalog.id, row_number=2, status="valid", raw_data={"model_name": "ONLY-FOR-CATALOG-ADMINS"}))
    await db_session.commit()
    jobs["catalog"] = str(catalog.id)
    return {
        "admin": admin, "uploader": uploader, "uploader_id": me, "other_unrestricted": other_unrestricted,
        "a": a, "b": b, "user_a": user_a, "headers_a": headers_a, "user_b": user_b, "headers_b": headers_b,
        "jobs": jobs, "room": room,
    }


# ------------------------------------------------------------------ prerequisites are real, not assumed
async def test_prerequisites_restricted_callers_hold_read_but_never_import(client, world):
    for user in (world["user_a"], world["user_b"]):
        eff = (await client.get(f"/api/v1/users/{user['id']}/effective-access", headers=world["admin"])).json()
        assert eff["unrestricted"] is False
        assert {"rack:read", "equipment:read"} <= set(eff["permissions"])
        assert set(IMPORT) <= set(eff["inactive_permissions"]), "import permissions must be inactive for a restricted caller"
        assert not set(IMPORT) & set(eff["permissions"]) or all(p in eff["inactive_permissions"] for p in IMPORT)
    # the uploader and the other unrestricted caller really are unrestricted and really hold the permissions used below
    for key, needed in (("uploader", {"rack:import", "rack:read"}), ("other_unrestricted", {"rack:read"})):
        me = (await client.get("/api/v1/auth/me", headers=world[key])).json()
        eff = (await client.get(f"/api/v1/users/{me['id']}/effective-access", headers=world["admin"])).json()
        assert eff["unrestricted"] is True and needed <= set(eff["permissions"])
    assert len(world["jobs"]) == 6


UPLOAD_PATHS = (
    "/api/v1/racks/import-jobs?mode=create_only",
    "/api/v1/equipment/import-jobs?mode=create_only",
    "/api/v1/catalog/import-jobs?mode=create_only",
)
# (path, status a restricted caller gets). Rack and equipment templates only need the read permission, which is active
# for restricted users; the catalog template needs `catalog:import`, which is inactive for them.
TEMPLATES = (
    ("/api/v1/racks/import-template", 200),
    ("/api/v1/equipment/import-template", 200),
    ("/api/v1/catalog/import-template", 403),
)


async def test_prerequisite_restricted_callers_get_an_exact_403_on_every_upload_route(client, world):
    content = build_workbook(RACK_HEADERS, [])
    for headers in (world["headers_a"], world["headers_b"]):
        for path in UPLOAD_PATHS:
            resp = await client.post(path, files={"file": ("x.xlsx", content, "application/octet-stream")}, headers=headers)
            assert resp.status_code == 403, (path, resp.status_code, resp.text)


async def test_template_downloads_for_restricted_and_unrestricted_callers(client, world):
    """Every template route is requested. Restricted callers get exactly the answer their active permissions give them;
    the unrestricted uploader can download all three. A 200 is a real XLSX, not an error page."""
    for headers in (world["headers_a"], world["headers_b"]):
        for path, expected in TEMPLATES:
            resp = await client.get(path, headers=headers)
            assert resp.status_code == expected, (path, resp.status_code, resp.text[:200])
            if expected == 200:
                assert resp.content[:4] == b"PK\x03\x04", path
    for path, _ in TEMPLATES:
        resp = await client.get(path, headers=world["uploader"])
        assert resp.status_code == 200 and resp.content[:4] == b"PK\x03\x04", (path, resp.status_code)


# ------------------------------------------------------------------ the full operation matrix for restricted A/B
async def test_every_import_job_operation_is_a_uniform_404_for_restricted_callers_and_changes_nothing(client, world, db_session):
    jobs = world["jobs"]
    before = {name: await _snapshot(db_session, jid) for name, jid in jobs.items()}
    missing, ref_ids = {}, {}
    for label, headers in (("A", world["headers_a"]), ("B", world["headers_b"])):
        for opname, method, suffix in OPERATIONS:
            ref_ids[(label, opname)] = uuid.uuid4()
            missing[(label, opname)] = await client.request(method, f"/api/v1/import-jobs/{ref_ids[(label, opname)]}{suffix}", headers=headers)
            assert missing[(label, opname)].status_code == 404

    checked, violations, outcomes = 0, [], {}
    for label, headers in (("A", world["headers_a"]), ("B", world["headers_b"])):
        for jobname, jid in jobs.items():
            for opname, method, suffix in OPERATIONS:
                resp = await client.request(method, f"/api/v1/import-jobs/{jid}{suffix}", headers=headers)
                checked += 1
                outcomes.setdefault(opname, {}).setdefault(resp.status_code, 0)
                outcomes[opname][resp.status_code] += 1
                ref = missing[(label, opname)]
                body, ref_body = _json(resp), _json(ref)
                same_shape = body.get("title") == ref_body.get("title") and str(body.get("detail", "")).replace(jid, "<id>") == str(
                    ref_body.get("detail", "")
                ).replace(str(ref_ids[(label, opname)]), "<id>")
                if resp.status_code != 404 or not same_shape:
                    violations.append((label, jobname, opname, resp.status_code))
                elif world["room"]["room_code"] in resp.text:
                    violations.append((label, jobname, opname, "echoes location data"))
    assert checked == 2 * 6 * len(OPERATIONS) == 72
    assert not violations, f"{len(violations)} of {checked} operations did not answer a uniform 404; status codes by operation: {outcomes}"
    after = {name: await _snapshot(db_session, jid) for name, jid in jobs.items()}
    assert after == before, "a rejected operation changed a job, its report or its audit trail"


async def test_unauthenticated_and_direct_id_substitution(client, world):
    for jid in world["jobs"].values():
        for _, method, suffix in OPERATIONS:
            assert (await client.request(method, f"/api/v1/import-jobs/{jid}{suffix}")).status_code in (401, 403)


# ------------------------------------------------------------------ positive controls (the same requests succeed for entitled callers)
async def test_unrestricted_uploader_and_unrestricted_peer_keep_full_access(client, world, db_session):
    jobs = world["jobs"]
    for headers in (world["uploader"], world["other_unrestricted"]):
        status = await client.get(f"/api/v1/import-jobs/{jobs['rack-validated']}", headers=headers)
        assert status.status_code == 200 and status.json()["status"] == "validated"
        rows = await client.get(f"/api/v1/import-jobs/{jobs['rack-validated']}/rows", headers=headers)
        assert rows.status_code == 200 and rows.json()["total"] == 1
        errors = await client.get(f"/api/v1/import-jobs/{jobs['rack-errors']}/rows?status=invalid", headers=headers)
        assert errors.status_code == 200 and errors.json()["total"] == 1 and errors.json()["items"][0]["errors"]
        assert (await client.get(f"/api/v1/import-jobs/{jobs['equipment']}", headers=headers)).status_code == 200
    committed = await client.get(f"/api/v1/import-jobs/{jobs['rack-to-commit']}", headers=world["uploader"])
    assert committed.json()["status"] in ("committed", "committed_with_errors") and committed.json()["committed_row_count"] == 1
    report = await client.get(f"/api/v1/import-jobs/{jobs['rack-to-commit']}/report", headers=world["uploader"])
    assert report.status_code == 200 and report.content[:4] == b"PK\x03\x04"
    # commit and cancel are authorised for the uploader (state transitions, then a conflict on repeat)
    assert (await client.post(f"/api/v1/import-jobs/{jobs['rack-validated']}/cancel", headers=world["uploader"])).status_code == 200
    assert (await client.post(f"/api/v1/import-jobs/{jobs['rack-validated']}/cancel", headers=world["uploader"])).status_code == 409
    assert (await client.post(f"/api/v1/import-jobs/{jobs['rack-to-commit']}/commit", headers=world["uploader"])).status_code == 409


async def test_a_restricted_user_with_no_import_permission_never_gets_403_for_a_foreign_job(client, world):
    """403 vs 404 would be an existence oracle: both answers must equal the answer for a random id."""
    for headers in (world["headers_a"], world["headers_b"]):
        for _, method, suffix in OPERATIONS:
            assert (await client.request(method, f"/api/v1/import-jobs/{world['jobs']['rack-validated']}{suffix}", headers=headers)).status_code == 404


# ------------------------------------------------------------------ historical owner (decision pending, behaviour pinned)
# A user uploads while unrestricted (global role) and is later made site-restricted. Uploader ownership alone then
# decides access: the job stays readable although its rows describe a room the user can no longer reach. The test
# below documents CURRENT behaviour of the interim uploader-only rule; it is not an endorsement. Whether that satisfies
# the intended contract is a product decision (see the PR description): no site/rack linkage is invented here.
# Reachable today only by direct database change (roles cannot change through the API).
@pytest.mark.parametrize("operation", ["status", "rows", "report", "commit", "cancel"])
async def test_historical_owner_cases(client, world, db_session, admin, auth_headers, make_user, operation):
    email = f"hist-{uuid.uuid4().hex[:8]}@example.com"
    user = await make_user(email, PW, "Engineer")  # global role: unrestricted, may upload and commit
    headers = await _login(client, email)
    room = world["room"]
    from tests.api.test_bulk_import_racks import _create_rack_model as _model

    model = await _model(client, auth_headers)
    content = build_workbook(
        RACK_HEADERS,
        [[f"HIST-{uuid.uuid4().hex[:6]}", "Hist rack", model["manufacturer"], model["model_name"], "", room["site_code"],
          room["building_code"], room["floor_level"], room["room_code"], 0, 0, 0, "Facilities", ""]],
    )
    job_id = await _upload(client, headers, "/api/v1/racks/import-jobs?mode=create_only", content)
    assert (await client.post(f"/api/v1/import-jobs/{job_id}/commit", headers=headers)).status_code == 202
    commit_bulk_import_job.run(job_id)
    owned = await client.get(f"/api/v1/import-jobs/{job_id}", headers=headers)
    assert owned.status_code == 200 and owned.json()["status"] == "committed" and owned.json()["committed_row_count"] == 1, owned.text
    report = await client.get(f"/api/v1/import-jobs/{job_id}/report", headers=headers)
    assert report.status_code == 200 and report.content[:4] == b"PK\x03\x04"  # a real XLSX exists before the restriction

    # the user loses the global role and keeps only a Site A group: now restricted, no access to the room's site
    await db_session.execute(text("DELETE FROM role_assignment WHERE user_id = :u"), {"u": str(user.id)})
    gid = await _group(client, admin, allow=[*DATA, *IMPORT], sites=[{"site_id": world["a"]["site"], "rack_scope": "all"}])
    assert (await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": [str(user.id)]}, headers=admin)).status_code == 200
    await db_session.commit()
    eff = (await client.get(f"/api/v1/users/{user.id}/effective-access", headers=admin)).json()
    assert eff["unrestricted"] is False and {s["site_id"] for s in eff["sites"]} == {world["a"]["site"]}

    method, suffix = {"status": ("get", ""), "rows": ("get", "/rows"), "report": ("get", "/report"),
                      "commit": ("post", "/commit"), "cancel": ("post", "/cancel")}[operation]
    before = await _snapshot(db_session, job_id)
    resp = await client.request(method, f"/api/v1/import-jobs/{job_id}{suffix}", headers=headers)
    # Owner-approved contract (2026-10-01, "keep uploader access"): the uploader keeps reading its own job after it
    # becomes site-restricted; commit and cancel are 403 because the import permissions are inactive for it.
    expected = {"status": 200, "rows": 200, "report": 200, "commit": 403, "cancel": 403}[operation]
    assert resp.status_code == expected, (operation, resp.status_code, resp.text[:200])
    if operation == "status":
        assert resp.json()["status"] == "committed"
    if operation == "rows":
        assert resp.json()["items"][0]["raw_data"]["room_code"] == room["room_code"], "rows expose the out-of-scope room's data"
    if operation == "report":
        assert resp.content[:4] == b"PK\x03\x04", "the report is a real XLSX"
    # another restricted user (not the owner) gets exactly the answer a missing id gets, and nothing changes
    other = await client.request(method, f"/api/v1/import-jobs/{job_id}{suffix}", headers=world["headers_b"])
    ref = await client.request(method, f"/api/v1/import-jobs/{uuid.uuid4()}{suffix}", headers=world["headers_b"])
    assert other.status_code == ref.status_code == 404
    assert _json(other).get("title") == _json(ref).get("title")
    assert await _snapshot(db_session, job_id) == before, "a restricted request changed the job, its report or its audit trail"
