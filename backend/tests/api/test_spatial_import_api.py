"""Issue #104: DXF/VSDX import through the API -> classify -> correct -> calibrate -> accept, against PostgreSQL.
Hostile files go through the real sandboxed parser child."""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.application.catalog_documents.extraction.sandbox import landlock_available
from tests import spatial_fixtures as fx
from tests.api._spatial_helpers import (
    FP,
    accept,
    by_label,
    calibrate_declared,
    candidates,
    concurrent_client,
    fresh,
    import_file,
    new_floor_plan,
    patch,
    room_with_plan,
    upload,
)

sandbox_required = pytest.mark.skipif(not landlock_available(), reason="Landlock is required for the parser sandbox")
pytestmark = sandbox_required


# ------------------------------------------------------------------------------------------ upload + parse
async def test_dxf_upload_parse_diagnostics_and_candidates(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(4), "room.dxf", "dxf")
    assert job["status"] == "parsed" and job["detected_format"] == "dxf" and len(job["file_hash"]) == 64

    diag = (await client.get(f"{FP}/import-jobs/{job['id']}/diagnostics", headers=headers)).json()
    assert diag["source_format"] == "dxf" and diag["parser_name"] == "dxf_ascii_subset" and diag["parser_version"] == "1"
    assert diag["source_units"] == "mm" and diag["units_trusted"] is True and diag["y_axis"] == "up"
    assert diag["source_bbox"] == {"min_x": 0.0, "min_y": 0.0, "max_x": 6000.0, "max_y": 4000.0}
    assert diag["racks_detected"] == 4 and diag["candidate_count"] == diag["objects_classified"] == 5
    assert len(diag["sir_sha256"]) == 64 and diag["errors"] == []

    items = await candidates(client, headers, job["id"])
    racks = [c for c in items if c["suggested_object_type"] == "rack"]
    assert len(racks) == 4 and all(c["status"] == "pending" and c["canonical"] is None for c in racks)
    assert all(c["evidence"] and c["version"] == 1 and c["source_ref"].startswith("dxf:") for c in racks)
    assert {c["effective_label"] for c in racks} == {"RACK-01", "RACK-02", "RACK-03", "RACK-04"}

    geo = (await client.get(f"{FP}/import-jobs/{job['id']}/source-geometry", headers=headers)).json()
    assert geo["source_units"] == "mm" and geo["sir_sha256"] == diag["sir_sha256"] and geo["total_entities"] == 9
    assert (await client.get(f"{FP}/import-jobs/{uuid.uuid4()}/source-geometry", headers=headers)).status_code == 404


async def test_vsdx_upload_parse_and_candidates(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_vsdx(3), "room.vsdx", "vsdx")
    assert job["status"] == "parsed" and job["detected_format"] == "vsdx"
    diag = (await client.get(f"{FP}/import-jobs/{job['id']}/diagnostics", headers=headers)).json()
    assert diag["parser_name"] == "vsdx_zip_xml" and diag["source_units"] == "in" and diag["y_axis"] == "up"
    racks = [c for c in await candidates(client, headers, job["id"]) if c["suggested_object_type"] == "rack"]
    assert [c["effective_label"] for c in racks] == ["RACK-01", "RACK-02", "RACK-03"]


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("plan.dxf", "application/octet-stream", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        ("plan.vsdx", "application/octet-stream", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        ("plan.svg", "application/octet-stream", fx.rack_row_dxf(1)),
        ("plan.dat", "image/png", fx.rack_row_dxf(1)),
        ("plan.vsd", "application/octet-stream", fx.rack_row_vsdx(1)),
    ],
)
async def test_declared_and_detected_format_mismatch_is_rejected(client, auth_headers, filename, content_type, data):
    headers, _, plan = await room_with_plan(client, auth_headers)
    resp = await upload(client, headers, plan["id"], data, filename, content_type)
    assert resp.status_code == 422 and resp.json()["title"] == "File Type Mismatch", resp.text


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (b"%PDF-1.7\n" + b"x" * 100, "not approved"),
        (b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 50, "Binary DXF"),
        (b"AC1027" + b"\x00" * 100, "DWG"),
        (b"MZ\x90\x00" + b"\x00" * 100, "not recognized"),
    ],
)
async def test_unapproved_and_unknown_content_is_rejected_by_content_not_name(client, auth_headers, data, needle):
    headers, _, plan = await room_with_plan(client, auth_headers)
    resp = await upload(client, headers, plan["id"], data, "innocent.svg")
    assert resp.status_code == 422 and needle in resp.json()["detail"]


async def test_unknown_extension_with_dxf_content_is_accepted_by_content(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    resp = await upload(client, headers, plan["id"], fx.rack_row_dxf(1), "export.dat")
    assert resp.status_code == 202 and resp.json()["detected_format"] == "dxf"


async def test_identical_upload_is_deduplicated_and_a_failed_job_can_be_retried(client, auth_headers, db_session):
    headers, _, plan = await room_with_plan(client, auth_headers)
    data = fx.rack_row_dxf(2)
    first = await import_file(client, headers, plan["id"], data, "a.dxf", "dxf")
    again = await upload(client, headers, plan["id"], data, "renamed.dxf")
    assert again.status_code == 202 and again.json()["id"] == first["id"] and again.json()["deduplicated"] is True
    jobs = (await client.get(f"{FP}/{plan['id']}/import-jobs", headers=headers)).json()
    assert jobs["total"] == 1

    other = await new_floor_plan(client, headers, (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()["room_id"])
    assert (await upload(client, headers, other["id"], data, "a.dxf")).json()["id"] != first["id"], "dedup is per floor plan"

    bad = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n"  # no EOF: truncated
    failed = await import_file(client, headers, plan["id"], bad, "bad.dxf", "dxf")
    assert failed["status"] == "failed"
    retry = await upload(client, headers, plan["id"], bad, "bad.dxf")
    assert retry.json()["id"] != failed["id"] and retry.json()["deduplicated"] is False


async def test_concurrent_identical_uploads_create_exactly_one_job(client, auth_headers, db_session):
    headers, _, plan = await room_with_plan(client, auth_headers)
    data = fx.rack_row_dxf(2)
    async with concurrent_client() as cc:
        responses = await asyncio.gather(*[upload(cc, headers, plan["id"], data, "a.dxf") for _ in range(6)])
    assert [r.status_code for r in responses] == [202] * 6
    assert len({r.json()["id"] for r in responses}) == 1
    count = (await db_session.execute(text("select count(*) from floor_plan_import_job where floor_plan_id = :f"), {"f": plan["id"]})).scalar_one()
    assert count == 1


async def test_task_redelivery_does_not_duplicate_candidates(client, auth_headers):
    from tests.api._spatial_helpers import run_job

    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(3), "a.dxf", "dxf")
    before = len(await candidates(client, headers, job["id"]))
    run_job(job["id"], fx.rack_row_dxf(3), "dxf")
    assert len(await candidates(client, headers, job["id"])) == before


HOSTILE_DXF = {
    "truncated": b"0\nSECTION\n2\nENTITIES\n0\nLINE\n",
    "recursive_blocks": fx.dxf(fx.insert("9", "0", "A", 0, 0), blocks=fx.block("A", fx.insert("a", "0", "B", 0, 0)) + fx.block("B", fx.insert("b", "0", "A", 0, 0))),
    "huge_coordinates": fx.dxf("0\nLINE\n5\nL\n8\nR\n10\n1e30\n20\n0\n11\n1\n21\n1\n"),
    "giant_text": fx.dxf(fx.text("1", "T", 0, 0, "A" * 6000)),
    "invalid_units": fx.dxf("", insunits=99),
    "garbage": b"0\nSECTION\n2\nENTITIES\nnope\nnope\n",
}
HOSTILE_VSDX = {
    "path_traversal": fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"../../evil.xml": b"x"}),
    "xxe": fx.vsdx(override={"visio/pages/page1.xml": b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>&a;</Shapes></PageContents>'}),
    "external_relationship": fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"_rels/x.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="a" Type="t" Target="https://evil.example" TargetMode="External"/></Relationships>'}),
    "zip_bomb": fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/media/b.bin": b"\x00" * (40 * 1024 * 1024)}),
    "macro": fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), extra={"visio/vbaProject.bin": b"x"}),
    "missing_parts": fx.vsdx(fx.vsdx_shape(1, 1, 1, 1, 1), drop=["visio/document.xml"]),
    "not_a_zip": b"PK\x03\x04" + b"\x00" * 200,
}


@pytest.mark.parametrize(("name", "data"), [*[(k, v) for k, v in HOSTILE_DXF.items()], ])
async def test_hostile_dxf_yields_a_failed_job_with_diagnostics_and_no_authoritative_write(client, auth_headers, db_session, name, data):
    await _assert_hostile_is_contained(client, auth_headers, db_session, data, f"{name}.dxf", "dxf")


@pytest.mark.parametrize(("name", "data"), [*[(k, v) for k, v in HOSTILE_VSDX.items()], ])
async def test_hostile_vsdx_yields_a_failed_job_with_diagnostics_and_no_authoritative_write(client, auth_headers, db_session, name, data):
    await _assert_hostile_is_contained(client, auth_headers, db_session, data, f"{name}.vsdx", "vsdx")


async def _assert_hostile_is_contained(client, auth_headers, db_session, data, filename, fmt):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], data, filename, fmt)
    assert job["status"] == "failed" and job["rejection_reason"]
    diag = (await client.get(f"{FP}/import-jobs/{job['id']}/diagnostics", headers=headers)).json()
    assert diag["errors"] and diag["failure_code"] and diag["candidate_count"] == 0
    assert await candidates(client, headers, job["id"]) == []
    assert (await client.get(f"{FP}/{plan['id']}/objects", headers=headers)).json() == []
    sir = (await db_session.execute(text("select count(*) from floor_plan_import_sir where job_id = :j"), {"j": job["id"]})).scalar_one()
    assert sir == 0, "a rejected file leaves no SIR"
    dedup = (await db_session.execute(text("select dedup_key from floor_plan_import_job where id = :j"), {"j": job["id"]})).scalar_one()
    assert dedup is None


async def test_parser_timeout_fails_the_job_cleanly(client, auth_headers, monkeypatch):
    from app.application.spatial_import.limits import ParserLimits
    from app.application.spatial_import.runner import parse_in_sandbox
    from app.infrastructure.tasks import floorplan_import as task_module

    monkeypatch.setattr(task_module, "parse_in_sandbox", lambda c, f, **k: parse_in_sandbox(c, f, limits=ParserLimits(wall_seconds=0.01), **k))
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "slow.dxf", "dxf")
    assert job["status"] == "failed" and "too long" in job["rejection_reason"]
    assert (await client.get(f"{FP}/import-jobs/{job['id']}/diagnostics", headers=headers)).json()["failure_code"] == "timeout"


async def test_unexpected_parser_failure_still_reaches_a_terminal_state(client, auth_headers, monkeypatch):
    from app.infrastructure.tasks import floorplan_import as task_module
    from tests.api._spatial_helpers import run_job

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(task_module, "parse_in_sandbox", boom)
    headers, _, plan = await room_with_plan(client, auth_headers)
    resp = await upload(client, headers, plan["id"], fx.rack_row_dxf(2), "x.dxf")
    job_id = resp.json()["id"]
    with pytest.raises(RuntimeError):
        run_job(job_id, fx.rack_row_dxf(2), "dxf")
    job = (await client.get(f"{FP}/import-jobs/{job_id}", headers=headers)).json()
    assert job["status"] == "failed" and "secret" not in (job["rejection_reason"] or "")


# ------------------------------------------------------------------------------------------ calibration
async def test_nothing_is_accepted_before_calibration_and_calibration_requires_if_match(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    cand = (await candidates(client, headers, job["id"]))[0]
    resp = await accept(client, headers, job["id"], cand, {"object_type": "imported_shape"})
    assert resp.status_code == 409 and "Calibrate" in resp.json()["detail"]

    body = {"method": "declared_units", "job_id": job["id"]}
    assert (await client.post(f"{FP}/{plan['id']}/calibration", json=body, headers=headers)).status_code == 428
    stale = await client.post(f"{FP}/{plan['id']}/calibration", json=body, headers={**headers, "If-Match": "7"})
    assert stale.status_code == 409


async def test_declared_units_calibration_records_lineage_and_error_bound(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    result = await calibrate_declared(client, headers, plan["id"], job["id"])
    cal = result["calibration"]
    assert cal["method"] == "declared_units" and cal["mm_per_unit"] == 1.0 and cal["sequence"] == 1 and cal["confidence"] == "high"
    assert cal["error_bound_mm"] == 0.5 and cal["y_axis"] == "up" and cal["origin_x"] == 0 and cal["origin_y"] == 4000
    assert cal["job_id"] == job["id"] and cal["supersedes_id"] is None
    assert result["floor_plan"]["current_calibration"]["id"] == cal["id"] and result["floor_plan"]["version"] == plan["version"] + 1
    assert (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()["current_calibration"]["id"] == cal["id"]
    history = (await client.get(f"{FP}/{plan['id']}/calibrations", headers=headers)).json()
    assert [c["sequence"] for c in history] == [1]
    items = await candidates(client, headers, job["id"])
    rack = by_label(items, "RACK-01")
    assert rack["canonical"] == {"geometry_type": "rect", "x_mm": 1000, "y_mm": 2000, "width_mm": 600, "height_mm": 1000, "rotation_deg": 0, "geometry_data": None}


async def test_two_point_calibration_for_a_pixel_drawing_exposes_bounds(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="500"><rect x="100" y="100" width="60" height="100"/><rect x="900" y="100" width="10" height="10"/></svg>'
    job = await import_file(client, headers, plan["id"], svg, "p.svg", "svg")
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    resp = await client.post(
        f"{FP}/{plan['id']}/calibration",
        json={"method": "two_point", "job_id": job["id"], "p1": [100, 100], "p2": [900, 100], "distance_mm": 8000, "tolerance_mm": 4, "pick_tolerance_src": 2},
        headers={**headers, "If-Match": str(fp["version"])},
    )
    assert resp.status_code == 201, resp.text
    cal = resp.json()["calibration"]
    assert cal["mm_per_unit"] == pytest.approx(10.0) and cal["source_units"] == "px"
    assert cal["relative_error"] == pytest.approx(4 / 8000 + 2 * 2 / 800) and cal["error_bound_mm"] > 0.5 and cal["confidence"] == "medium"
    assert cal["reference"]["distance_mm"] == 8000 and cal["reference"]["p1"] == [100, 100]
    assert resp.json()["floor_plan"]["calibration_scale_mm_per_px"] == pytest.approx(10.0)


@pytest.mark.parametrize(
    "body",
    [
        {"method": "two_point", "p1": [5, 5], "p2": [5, 5], "distance_mm": 100},
        {"method": "two_point", "p1": [0, 0], "p2": [10, 0]},  # missing distance
        {"method": "room_dimension", "src_width": 100, "real_width_mm": 8000, "src_height": 50, "real_height_mm": 9000},
        {"method": "manual_scale", "mm_per_unit": 0},
        {"method": "declared_units", "origin": [1e12, 0]},
        {"method": "bogus"},
    ],
)
async def test_invalid_calibrations_are_rejected_cleanly(client, auth_headers, body):
    headers, _, plan = await room_with_plan(client, auth_headers)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="100" height="50"/></svg>'
    job = await import_file(client, headers, plan["id"], svg, "p.svg", "svg")
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    resp = await client.post(f"{FP}/{plan['id']}/calibration", json={"job_id": job["id"], **body}, headers={**headers, "If-Match": str(fp["version"])})
    assert resp.status_code == 422, resp.text
    assert (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()["current_calibration"] is None


async def test_declared_units_on_a_pixel_drawing_is_refused(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="100" height="50"/></svg>'
    job = await import_file(client, headers, plan["id"], svg, "p.svg", "svg")
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    resp = await client.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": job["id"]}, headers={**headers, "If-Match": str(fp["version"])})
    assert resp.status_code == 422 and "no usable length unit" in resp.json()["detail"]


async def test_calibration_job_must_belong_to_the_floor_plan_and_be_parsed(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    other = await new_floor_plan(client, headers, room_id)
    other_job = await import_file(client, headers, other["id"], fx.rack_row_dxf(1), "o.dxf", "dxf")
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    h = {**headers, "If-Match": str(fp["version"])}
    assert (await client.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": other_job["id"]}, headers=h)).status_code == 404
    queued = (await upload(client, headers, plan["id"], fx.rack_row_dxf(3), "q.dxf")).json()
    assert (await client.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": queued["id"]}, headers=h)).status_code == 409


async def test_recalibration_is_allowed_until_the_first_accept_then_locked(client, auth_headers, db_session):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    first = await calibrate_declared(client, headers, plan["id"], job["id"])
    second = await calibrate_declared(client, headers, plan["id"], job["id"], rotation_degrees=90)
    assert second["calibration"]["sequence"] == 2 and second["calibration"]["supersedes_id"] == first["calibration"]["id"]
    assert second["calibration"]["rotation_quadrants"] == 1
    assert [c["sequence"] for c in (await client.get(f"{FP}/{plan['id']}/calibrations", headers=headers)).json()] == [2, 1]

    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    assert (await accept(client, headers, job["id"], cand, {"object_type": "rack"})).status_code == 200
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    locked = await client.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": job["id"]}, headers={**headers, "If-Match": str(fp["version"])})
    assert locked.status_code == 409 and "recalibrate on a new floor plan revision" in locked.json()["detail"]


async def test_calibration_rows_are_immutable_in_the_database(client, auth_headers, db_session):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    cal = (await calibrate_declared(client, headers, plan["id"], job["id"]))["calibration"]
    for sql in (
        "update floor_plan_calibration set mm_per_unit = 2 where id = :i",
        "update floor_plan_calibration set error_bound_mm = 0 where id = :i",
        "delete from floor_plan_calibration where id = :i",
        "update floor_plan_import_sir set sir_sha256 = 'x' where job_id = :j",
    ):
        with pytest.raises(Exception, match="immutable"):
            await db_session.execute(text(sql), {"i": cal["id"], "j": job["id"]})
        await db_session.rollback()
    with pytest.raises(Exception, match="scale_positive|violates"):
        await db_session.execute(text("insert into floor_plan_calibration (floor_plan_id, sequence, method, source_units, mm_per_unit, origin_x, origin_y, y_axis, confidence) values (:f, 99, 'manual_scale', 'mm', 0, 0, 0, 'up', 'low')"), {"f": plan["id"]})
    await db_session.rollback()


# ------------------------------------------------------------------------------------------ corrections
async def test_corrections_are_staged_on_the_candidate_and_never_touch_authoritative_state(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(3), "a.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    raw = cand["raw_geometry"]

    resp = await patch(client, headers, job["id"], cand, {"cx": raw["cx"] + 100, "width": 650, "rotation_deg": 15, "label": "R-A1", "object_type": "equipment"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == cand["version"] + 1 and body["can_undo"] is True
    assert body["raw_geometry"] == raw, "the parser's geometry is never overwritten"
    assert body["effective_geometry"]["cx"] == raw["cx"] + 100 and body["effective_geometry"]["width"] == 650
    assert body["effective_label"] == "R-A1" and body["effective_object_type"] == "equipment" and body["suggested_label"] == "RACK-01"
    assert body["canonical"]["width_mm"] == 650 and body["canonical"]["rotation_deg"] == 345  # y-up CCW 15 -> clockwise 345
    assert (await client.get(f"{FP}/{plan['id']}/objects", headers=headers)).json() == []

    stale = await patch(client, headers, job["id"], cand, {"label": "again"})  # old version
    assert stale.status_code == 409
    undone = await client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/undo", headers={**headers, "If-Match": str(body["version"])})
    assert undone.status_code == 200 and undone.json()["correction"] is None and undone.json()["effective_label"] == "RACK-01"
    assert undone.json()["can_undo"] is False
    nothing = await client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/undo", headers={**headers, "If-Match": str(undone.json()["version"])})
    assert nothing.status_code == 409


async def test_corrected_geometry_is_what_gets_accepted_and_persisted(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-02")
    raw = cand["raw_geometry"]
    patched = (await patch(client, headers, job["id"], cand, {"cx": raw["cx"] + 250, "cy": raw["cy"] - 100, "label": "Row A / 02"})).json()
    accepted = await accept(client, headers, job["id"], patched, {"object_type": "rack"})
    assert accepted.status_code == 200, accepted.text
    obj = next(o for o in (await client.get(f"{FP}/{plan['id']}/objects", headers=headers)).json() if o["id"] == accepted.json()["resulting_spatial_object_id"])
    # RACK-02 sat at x=1700..2300, y=1000..2000 (Y up) -> corrected +250 in X and -100 in Y(up) = +100 mm down
    assert (obj["x_mm"], obj["y_mm"], obj["width_mm"], obj["height_mm"]) == (1950, 2100, 600, 1000)
    assert obj["label"] == "Row A / 02" and obj["object_type"] == "rack" and obj["source"] == "imported"


@pytest.mark.parametrize(
    "body",
    [
        {"object_type": "'; DROP TABLE x;--"},
        {"width": 0},
        {"width": -5},
        {"cx": 1e30},
        {"rotation_deg": 9999},
        {"label": "x" * 300},
        {"points": [[0, 0], [1, 1]]},  # points on a rectangle
        {"matched_asset_id": str(uuid.uuid4())},
    ],
)
async def test_invalid_corrections_are_rejected(client, auth_headers, body):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    resp = await patch(client, headers, job["id"], cand, body)
    assert resp.status_code == 422, resp.text
    assert (await fresh(client, headers, job["id"], cand["id"]))["correction"] is None


async def test_text_candidates_cannot_be_resized(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.dxf(fx.text("1", "NOTES", 5, 5, "UPS ROOM")), "t.dxf", "dxf")
    cand = (await candidates(client, headers, job["id"]))[0]
    assert (await patch(client, headers, job["id"], cand, {"width": 5})).status_code == 422
    assert (await patch(client, headers, job["id"], cand, {"cx": 6})).status_code == 200


async def test_concurrent_edits_with_the_same_version_have_exactly_one_winner(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    async with concurrent_client() as cc:
        results = await asyncio.gather(*[patch(cc, headers, job["id"], cand, {"label": f"op-{i}"}) for i in range(8)])
    assert sorted(r.status_code for r in results) == [200] + [409] * 7


async def test_rejecting_a_candidate_never_creates_geometry_and_is_terminal(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    rejected = await client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/reject", headers={**headers, "If-Match": str(cand["version"])})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert (await patch(client, headers, job["id"], rejected.json(), {"label": "x"})).status_code == 409
    assert (await client.get(f"{FP}/{plan['id']}/objects", headers=headers)).json() == []
    assert (await client.post(f"{FP}/import-jobs/{job['id']}/candidates/{cand['id']}/reject", headers=headers)).status_code == 428


async def test_viewer_can_read_but_not_calibrate_correct_or_accept(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "a.dxf", "dxf")
    viewer = await auth_headers("Viewer")
    cand = (await candidates(client, viewer, job["id"]))[0]
    assert (await client.get(f"{FP}/import-jobs/{job['id']}/diagnostics", headers=viewer)).status_code == 200
    assert (await client.get(f"{FP}/import-jobs/{job['id']}/source-geometry", headers=viewer)).status_code == 200
    assert (await client.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": job["id"]}, headers={**viewer, "If-Match": "1"})).status_code == 403
    assert (await patch(client, viewer, job["id"], cand, {"label": "x"})).status_code == 403
    assert (await accept(client, viewer, job["id"], cand, {"object_type": "rack"})).status_code == 403
    assert (await client.post(f"{FP}/import-jobs/{job['id']}/reconcile", headers=viewer)).status_code == 403
    assert (await client.put(f"{FP}/{plan['id']}/room-boundary", json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 100, "height_mm": 100}, headers={**viewer, "If-Match": "1"})).status_code == 403
    engineer = await auth_headers("Engineer")
    assert (await upload(client, engineer, plan["id"], fx.rack_row_dxf(3), "e.dxf")).status_code == 202
    assert (await accept(client, engineer, job["id"], cand, {"object_type": "rack"})).status_code == 403
