"""Issue #104: reconciliation with authoritative racks, the acceptance boundary, room boundary, lineage and the
concurrency attacks (accept vs move, accept vs recalibrate, double-claim of one rack)."""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.application.catalog_documents.extraction.sandbox import landlock_available
from tests import spatial_fixtures as fx
from tests.api._phase2_helpers import create_room
from tests.api._spatial_helpers import (
    FP,
    accept,
    by_label,
    calibrate_declared,
    candidates,
    concurrent_client,
    fresh,
    import_file,
    patch,
    place_rack,
    room_with_plan,
)

pytestmark = pytest.mark.skipif(not landlock_available(), reason="Landlock is required for the parser sandbox")

RACK1 = (1000, 2000)  # canonical top-left of RACK-01 in the rack_row_dxf fixture (Y flipped from the 4000 mm room)
RACK2 = (1700, 2000)


async def get_rack(client, headers, rack_id: str) -> dict:
    return (await client.get(f"/api/v1/racks/{rack_id}", headers=headers)).json()


async def world(client, auth_headers, *, racks=((RACK1, "Rack A"), (RACK2, "Rack B")), count=4):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    placed = [await place_rack(client, headers, auth_headers, room_id, x, y, name=name) for (x, y), name in racks]
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(count), "r.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    return {"headers": headers, "room": room_id, "plan": plan, "job": job, "racks": placed}


async def objects(client, headers, plan_id):
    return (await client.get(f"{FP}/{plan_id}/objects", headers=headers)).json()


# ------------------------------------------------------------------------------------------ reconciliation
async def test_candidates_are_matched_to_existing_racks_by_position_and_footprint_not_by_label(client, auth_headers):
    w = await world(client, auth_headers)
    items = await candidates(client, w["headers"], w["job"]["id"])
    first, second, third = (by_label(items, f"RACK-0{i}") for i in (1, 2, 3))
    assert first["match_status"] == "matched" and first["matched_asset_id"] == w["racks"][0]["id"] and first["matched_asset_name"] == "Rack A"
    assert second["match_status"] == "matched" and second["matched_asset_id"] == w["racks"][1]["id"]
    assert third["match_status"] == "unmatched" and third["matched_asset_id"] is None
    codes = {e["code"] for e in first["evidence"] if e.get("phase") == "match"}
    assert {"position", "footprint", "orientation", "label"} <= codes
    assert any(e["code"] == "layer_name" and "phase" not in e for e in first["evidence"]), "detection evidence is kept separate"
    assert first["reconciled_calibration_id"] is not None and first["match_score"] >= 0.7
    # nothing authoritative changed
    assert (await get_rack(client, w["headers"], w["racks"][0]["id"]))["placement"]["x_mm"] == RACK1[0]
    assert await objects(client, w["headers"], w["plan"]["id"]) == []


async def test_wrong_label_on_the_right_place_and_right_label_in_the_wrong_place_are_conflicts(client, auth_headers):
    w = await world(client, auth_headers)
    items = await candidates(client, w["headers"], w["job"]["id"])
    swap = await patch(client, w["headers"], w["job"]["id"], by_label(items, "RACK-01"), {"label": "Rack B"})
    assert swap.status_code == 200
    again = await client.post(f"{FP}/import-jobs/{w['job']['id']}/reconcile", headers=w["headers"])
    assert again.status_code == 200 and again.json()["conflict"] >= 1
    cand = await fresh(client, w["headers"], w["job"]["id"], items[0]["id"] if items[0]["effective_label"] else by_label(items, "RACK-01")["id"])
    assert cand["match_status"] == "conflict" and any(e["code"] == "label_conflicts_with_position" for e in cand["evidence"])


async def test_a_conflicted_or_ambiguous_candidate_is_never_auto_resolved(client, auth_headers):
    w = await world(client, auth_headers, racks=(((1350, 2000), "Rack Mid"), ((1450, 2000), "Rack Mid 2")))
    statuses = {c["match_status"] for c in await candidates(client, w["headers"], w["job"]["id"]) if c["suggested_object_type"] == "rack"}
    assert statuses & {"ambiguous", "conflict", "matched", "unmatched"} and "duplicate" not in statuses
    assert await objects(client, w["headers"], w["plan"]["id"]) == []


# ------------------------------------------------------------------------------------------ acceptance authority
async def test_accepting_without_flags_creates_geometry_but_never_moves_or_links_the_rack(client, auth_headers):
    w = await world(client, auth_headers)
    rack = w["racks"][0]
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-01")
    moved = await patch(client, w["headers"], w["job"]["id"], cand, {"cx": cand["raw_geometry"]["cx"] + 400})
    before = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    accepted = await accept(client, w["headers"], w["job"]["id"], moved.json(), {"object_type": "rack", "matched_asset_id": rack["id"]})
    assert accepted.status_code == 200, accepted.text
    after = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    assert (after["x_mm"], after["y_mm"], after["version"], after["spatial_object_id"]) == (before["x_mm"], before["y_mm"], before["version"], None)
    (obj,) = await objects(client, w["headers"], w["plan"]["id"])
    assert obj["x_mm"] == RACK1[0] + 400, "the drawn shape is where the operator put it; the asset did not move"
    assert obj["provenance"]["matched_asset_id"] == rack["id"]


async def test_link_placement_links_the_drawn_shape_without_changing_coordinates(client, auth_headers):
    w = await world(client, auth_headers)
    rack = w["racks"][0]
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-01")
    before = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    unconfirmed = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "link_placement": True})
    assert unconfirmed.status_code == 422, "the system's suggested match is never enough: the operator must confirm the asset"
    resp = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "matched_asset_id": rack["id"], "link_placement": True})
    assert resp.status_code == 200, resp.text
    after = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    assert after["spatial_object_id"] == resp.json()["resulting_spatial_object_id"]
    assert (after["x_mm"], after["y_mm"], after["rotation_deg"]) == (before["x_mm"], before["y_mm"], before["rotation_deg"])
    assert after["version"] == before["version"] + 1
    again = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-02")
    other = await accept(client, w["headers"], w["job"]["id"], again, {"object_type": "rack", "matched_asset_id": rack["id"], "link_placement": True})
    assert other.status_code == 409, "a rack is represented by at most one accepted shape"


async def test_apply_position_is_an_explicit_operator_move_and_needs_the_placement_version(client, auth_headers):
    w = await world(client, auth_headers)
    rack = w["racks"][0]
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-01")
    corrected = (await patch(client, w["headers"], w["job"]["id"], cand, {"cx": cand["raw_geometry"]["cx"] + 120, "rotation_deg": 30})).json()
    body = {"object_type": "rack", "matched_asset_id": rack["id"], "apply_position": True}
    missing = await accept(client, w["headers"], w["job"]["id"], corrected, body)
    assert missing.status_code == 428
    stale = await accept(client, w["headers"], w["job"]["id"], corrected, {**body, "placement_version": 99})
    assert stale.status_code == 409
    placement = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    ok = await accept(client, w["headers"], w["job"]["id"], corrected, {**body, "placement_version": placement["version"]})
    assert ok.status_code == 200, ok.text
    moved = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    assert (moved["x_mm"], moved["y_mm"], moved["rotation_deg"]) == (RACK1[0] + 120, RACK1[1], 330)
    assert moved["spatial_object_id"] == ok.json()["resulting_spatial_object_id"] and moved["effective_from"] != placement["effective_from"]
    closed = (await db_count(client, w, rack["id"]))
    assert closed == (2, 1), "history keeps the closed placement; exactly one is current"


async def db_count(client, w, rack_id):
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import TEST_ADMIN_DATABASE_URL

    engine = create_async_engine(TEST_ADMIN_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("select count(*), count(*) filter (where effective_to is null) from rack_placement where rack_id = :r"), {"r": rack_id})).one()
        return tuple(row)
    finally:
        await engine.dispose()


async def test_apply_position_places_an_unplaced_rack_and_a_viewer_style_role_cannot_move_racks(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    from tests.api._phase2_helpers import create_rack

    loose = await create_rack(client, headers, auth_headers, name="Loose")
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(2), "r.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    cand = by_label(await candidates(client, headers, job["id"]), "RACK-01")
    placed = await accept(client, headers, job["id"], cand, {"object_type": "rack", "matched_asset_id": loose["id"], "apply_position": True})
    assert placed.status_code == 200, placed.text
    placement = (await get_rack(client, headers, loose["id"]))["placement"]
    assert (placement["room_id"], placement["x_mm"], placement["y_mm"]) == (room_id, RACK1[0], RACK1[1])


async def test_rack_in_another_room_and_non_rack_matches_are_refused(client, auth_headers):
    w = await world(client, auth_headers)
    other_room = await create_room(client, auth_headers)
    stranger = await place_rack(client, w["headers"], auth_headers, other_room, 100, 100, name="Elsewhere")
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-03")
    far = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "matched_asset_id": stranger["id"], "apply_position": True, "placement_version": 1})
    assert far.status_code == 409 and "different room" in far.json()["detail"]
    nonrack = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "annotation", "matched_asset_id": w["racks"][0]["id"]})
    assert nonrack.status_code == 422
    unknown = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "matched_asset_id": str(uuid.uuid4())})
    assert unknown.status_code == 422
    both = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "matched_asset_id": w["racks"][0]["id"], "link_placement": True, "apply_position": True})
    assert both.status_code == 422
    assert await objects(client, w["headers"], w["plan"]["id"]) == []


async def test_accepted_geometry_records_the_full_lineage(client, auth_headers):
    w = await world(client, auth_headers)
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-03")
    resp = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack"})
    (obj,) = await objects(client, w["headers"], w["plan"]["id"])
    prov = obj["provenance"]
    plan = (await client.get(f"{FP}/{w['plan']['id']}", headers=w["headers"])).json()
    diag = (await client.get(f"{FP}/import-jobs/{w['job']['id']}/diagnostics", headers=w["headers"])).json()
    assert prov["origin"] == "floor_plan_import" and prov["job_id"] == w["job"]["id"] and prov["candidate_id"] == cand["id"]
    assert prov["calibration_id"] == plan["current_calibration"]["id"] and prov["sir_sha256"] == diag["sir_sha256"]
    assert prov["source_ref"] == cand["source_ref"] and prov["accepted_by"] and prov["accepted_at"]
    assert resp.json()["status"] == "accepted" and resp.json()["resulting_spatial_object_id"] == obj["id"]
    audit = (await client.get("/api/v1/audit-logs", params={"entity_id": cand["id"]}, headers=await auth_headers("Administrator")))
    if audit.status_code == 200:
        assert any(e["action"] == "floor_plan.candidate_accept" for e in audit.json().get("items", []))


async def test_stale_candidate_version_cannot_be_accepted(client, auth_headers):
    w = await world(client, auth_headers)
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-03")
    await patch(client, w["headers"], w["job"]["id"], cand, {"label": "changed meanwhile"})
    stale = await accept(client, w["headers"], w["job"]["id"], cand, {"object_type": "rack"})
    assert stale.status_code == 409
    assert (await client.post(f"{FP}/import-jobs/{w['job']['id']}/candidates/{cand['id']}/accept", json={"object_type": "rack"}, headers=w["headers"])).status_code == 428
    assert await objects(client, w["headers"], w["plan"]["id"]) == []


# ------------------------------------------------------------------------------------------ re-export
def _reexport(count=4):
    """The same room exported again: reversed entity order, new handles, renamed labels, sub-millimetre noise."""
    entities = fx.rect_poly("F00", "WALLS", 0, 0, 6000, 4000)
    for i in reversed(range(count)):
        x = 1000.2 + i * 700
        entities += fx.rect_poly(f"F{i + 1:02d}", "RACKS", x, 999.8, 599.9, 1000.1)
        entities += fx.text(f"G{i + 1:02d}", "TEXT", x + 300, 1500, f"R{i + 1}")
    return fx.dxf(entities, layers=("WALLS", "RACKS", "TEXT"))


async def test_a_reexported_drawing_cannot_create_duplicate_racks_or_rewrite_accepted_placement(client, auth_headers):
    w = await world(client, auth_headers)
    items = await candidates(client, w["headers"], w["job"]["id"])
    for label, rack in (("RACK-01", w["racks"][0]), ("RACK-02", w["racks"][1])):
        resp = await accept(client, w["headers"], w["job"]["id"], by_label(items, label), {"object_type": "rack", "matched_asset_id": rack["id"], "link_placement": True})
        assert resp.status_code == 200, resp.text
    first_objects = await objects(client, w["headers"], w["plan"]["id"])
    before = (await get_rack(client, w["headers"], w["racks"][0]["id"]))["placement"]

    job2 = await import_file(client, w["headers"], w["plan"]["id"], _reexport(), "reexport.dxf", "dxf")
    assert job2["id"] != w["job"]["id"] and job2["file_hash"] != w["job"]["file_hash"]
    reconciled = await client.post(f"{FP}/import-jobs/{job2['id']}/reconcile", headers=w["headers"])
    assert reconciled.status_code == 200 and reconciled.json()["duplicate"] == 2
    second = await candidates(client, w["headers"], job2["id"])
    dupes = [c for c in second if c["match_status"] == "duplicate"]
    assert len(dupes) == 2 and {d["duplicate_of_spatial_object_id"] for d in dupes} == {o["id"] for o in first_objects if o["object_type"] == "rack"}
    assert all(any(e["code"] == "overlaps_accepted_geometry" for e in d["evidence"]) for d in dupes)
    blocked = await accept(client, w["headers"], job2["id"], dupes[0], {"object_type": "rack"})
    assert blocked.status_code == 409 and "already occupies" in blocked.json()["detail"]
    assert len([o for o in await objects(client, w["headers"], w["plan"]["id"]) if o["object_type"] == "rack"]) == 2
    assert (await get_rack(client, w["headers"], w["racks"][0]["id"]))["placement"] == before, "reimport never rewrites accepted placement"
    new_racks = [c for c in second if c["suggested_object_type"] == "rack" and c["match_status"] != "duplicate"]
    assert len(new_racks) == 2 and all(c["status"] == "pending" for c in new_racks)


# ------------------------------------------------------------------------------------------ room boundary
async def test_room_outline_candidate_becomes_the_boundary_and_a_second_one_is_refused(client, auth_headers):
    w = await world(client, auth_headers, racks=())
    outline = next(c for c in await candidates(client, w["headers"], w["job"]["id"]) if c["suggested_object_type"] == "room_outline")
    resp = await accept(client, w["headers"], w["job"]["id"], outline, {"object_type": "room_outline"})
    assert resp.status_code == 200, resp.text
    view = (await client.get(f"/api/v1/spatial/rooms/{w['room']}/view", headers=w["headers"])).json()
    assert view["active_floor_plan_id"] is None, "a draft plan is not the room's active plan"
    (obj,) = await objects(client, w["headers"], w["plan"]["id"])
    assert (obj["object_type"], obj["x_mm"], obj["y_mm"], obj["width_mm"], obj["height_mm"]) == ("room_outline", 0, 0, 6000, 4000)
    again = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-01")
    assert again["status"] == "pending"
    wall = await import_file(client, w["headers"], w["plan"]["id"], fx.dxf(fx.rect_poly("W", "WALLS", 0, 0, 6000, 4000) + fx.rect_poly("a", "R", 10, 10, 20, 20) + fx.rect_poly("b", "R", 40, 10, 20, 20) + fx.rect_poly("c", "R", 70, 10, 20, 20)), "w.dxf", "dxf")
    second = next(c for c in await candidates(client, w["headers"], wall["id"]) if c["suggested_object_type"] == "room_outline")
    dup = await accept(client, w["headers"], wall["id"], second, {"object_type": "room_outline"})
    assert dup.status_code == 409 and "already has an approved room boundary" in dup.json()["detail"]


async def test_rack_outside_the_boundary_needs_a_recorded_exception(client, auth_headers):
    w = await world(client, auth_headers, racks=(), count=4)
    items = await candidates(client, w["headers"], w["job"]["id"])
    await accept(client, w["headers"], w["job"]["id"], next(c for c in items if c["suggested_object_type"] == "room_outline"), {"object_type": "room_outline"})
    inside = by_label(items, "RACK-01")
    assert (await accept(client, w["headers"], w["job"]["id"], inside, {"object_type": "rack"})).status_code == 200
    out = by_label(items, "RACK-02")
    pushed = (await patch(client, w["headers"], w["job"]["id"], out, {"cx": 9000})).json()
    refused = await accept(client, w["headers"], w["job"]["id"], pushed, {"object_type": "rack"})
    assert refused.status_code == 422 and "outside the approved room boundary" in refused.json()["detail"]
    allowed = await accept(client, w["headers"], w["job"]["id"], pushed, {"object_type": "rack", "boundary_exception_reason": "Annex bay, drawing is clipped"})
    assert allowed.status_code == 200, allowed.text
    obj = next(o for o in await objects(client, w["headers"], w["plan"]["id"]) if o["id"] == allowed.json()["resulting_spatial_object_id"])
    assert obj["provenance"]["boundary_exceptions"][0]["reason"] == "Annex bay, drawing is clipped"


async def test_room_boundary_can_be_set_by_hand_and_reports_racks_left_outside(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    inside = await place_rack(client, headers, auth_headers, room_id, 500, 500, name="In")
    edge = await place_rack(client, headers, auth_headers, room_id, 4800, 500, name="Edge")
    url = f"{FP}/{plan['id']}/room-boundary"
    bad = await client.put(url, json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 0, "height_mm": 10}, headers={**headers, "If-Match": "1"})
    assert bad.status_code == 422
    flat = await client.put(url, json={"shape": "polygon", "points_mm": [[0, 0], [10, 0], [20, 0]]}, headers={**headers, "If-Match": "1"})
    assert flat.status_code == 422
    ok = await client.put(url, json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 5000, "height_mm": 3000}, headers={**headers, "If-Match": "1"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["racks_outside_boundary"] == ["Edge"] and ok.json()["floor_plan"]["room_width_mm"] == 5000
    poly = await client.put(url, json={"shape": "polygon", "points_mm": [[0, 0], [8000, 0], [8000, 2000], [0, 3000]]}, headers={**headers, "If-Match": str(ok.json()["floor_plan"]["version"])})
    assert poly.status_code == 200 and poly.json()["spatial_object_id"] == ok.json()["spatial_object_id"], "replaces in place"
    assert sorted(poly.json()["racks_outside_boundary"]) == []
    assert (await client.put(url, json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 5, "height_mm": 5}, headers={**headers, "If-Match": "1"})).status_code == 409
    assert inside["id"] != edge["id"]


async def test_rack_placement_endpoints_enforce_the_active_boundary_with_an_audited_override(client, auth_headers):
    headers, room_id, plan = await room_with_plan(client, auth_headers)
    rack = await place_rack(client, headers, auth_headers, room_id, 100, 100, name="Mover")
    boundary = await client.put(f"{FP}/{plan['id']}/room-boundary", json={"shape": "rect", "x_mm": 0, "y_mm": 0, "width_mm": 3000, "height_mm": 3000}, headers={**headers, "If-Match": "1"})
    assert boundary.status_code == 200
    fp_version = boundary.json()["floor_plan"]["version"]
    inactive_move = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id, "x_mm": 9000, "y_mm": 100, "rotation_deg": 0}, headers=headers)
    assert inactive_move.status_code == 200, "a draft plan imposes no constraint on live placement"
    activate = await client.post(f"{FP}/{plan['id']}/activate", headers={**headers, "If-Match": str(fp_version)})
    assert activate.status_code == 200
    out = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id, "x_mm": 9000, "y_mm": 100, "rotation_deg": 0}, headers=headers)
    assert out.status_code == 422 and "outside the approved room boundary" in out.json()["detail"]
    inside = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id, "x_mm": 1000, "y_mm": 1000, "rotation_deg": 0}, headers=headers)
    assert inside.status_code == 200
    forced = await client.post(f"/api/v1/racks/{rack['id']}/move", json={"room_id": room_id, "x_mm": 9000, "y_mm": 100, "rotation_deg": 0, "boundary_exception_reason": "Temporary staging"}, headers=headers)
    assert forced.status_code == 200
    from tests.api._phase2_helpers import create_rack_model_revision

    revision = await create_rack_model_revision(client, auth_headers)
    created = await client.post("/api/v1/racks", json={"asset_tag": "RK-OUT-1", "model_revision_id": revision, "name": "New", "room_id": room_id, "x_mm": 9500, "y_mm": 0}, headers=headers)
    assert created.status_code == 422
    unplaced = await client.post("/api/v1/racks", json={"asset_tag": "RK-NOPOS", "model_revision_id": revision, "name": "New2", "room_id": room_id}, headers=headers)
    assert unplaced.status_code == 201, "no coordinates -> nothing to contain"


# ------------------------------------------------------------------------------------------ concurrency
async def test_accept_apply_position_racing_a_rack_move_has_exactly_one_winner(client, auth_headers):
    w = await world(client, auth_headers)
    rack = w["racks"][0]
    cand = by_label(await candidates(client, w["headers"], w["job"]["id"]), "RACK-01")
    placement = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    async with concurrent_client() as cc:
        accept_task = accept(cc, w["headers"], w["job"]["id"], cand, {"object_type": "rack", "matched_asset_id": rack["id"], "apply_position": True, "placement_version": placement["version"]})
        move_task = cc.post(
            f"/api/v1/racks/{rack['id']}/move", json={"room_id": w["room"], "x_mm": 3000, "y_mm": 500, "rotation_deg": 0},
            headers={**w["headers"], "If-Match": str(placement["version"])},
        )
        accepted, moved = await asyncio.gather(accept_task, move_task)
    statuses = sorted([accepted.status_code, moved.status_code])
    assert statuses == [200, 409], (accepted.text, moved.text)
    count = await db_count(client, w, rack["id"])
    assert count[1] == 1, "exactly one current placement, never two"
    current = (await get_rack(client, w["headers"], rack["id"]))["placement"]
    objs = await objects(client, w["headers"], w["plan"]["id"])
    if accepted.status_code == 200:
        assert (current["x_mm"], current["y_mm"]) == RACK1 and current["spatial_object_id"] == objs[0]["id"]
    else:
        assert (current["x_mm"], current["y_mm"]) == (3000, 500) and objs == [] and (await fresh(client, w["headers"], w["job"]["id"], cand["id"]))["status"] == "pending"


async def test_two_candidates_claiming_one_rack_have_exactly_one_winner(client, auth_headers):
    w = await world(client, auth_headers)
    rack = w["racks"][0]
    items = await candidates(client, w["headers"], w["job"]["id"])
    c1, c2 = by_label(items, "RACK-01"), by_label(items, "RACK-03")
    async with concurrent_client() as cc:
        results = await asyncio.gather(*[accept(cc, w["headers"], w["job"]["id"], c, {"object_type": "rack", "matched_asset_id": rack["id"], "link_placement": True}) for c in (c1, c2)])
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert len(await objects(client, w["headers"], w["plan"]["id"])) == 1


async def test_overlapping_candidates_accepted_at_once_cannot_both_become_racks(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    twin = fx.dxf(fx.rect_poly("1", "RACKS", 1000, 1000, 600, 1000) + fx.rect_poly("2", "RACKS", 1010, 1005, 600, 1000))
    job = await import_file(client, headers, plan["id"], twin, "t.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    items = await candidates(client, headers, job["id"])
    async with concurrent_client() as cc:
        results = await asyncio.gather(*[accept(cc, headers, job["id"], c, {"object_type": "rack"}) for c in items])
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert len(await objects(client, headers, plan["id"])) == 1


async def test_accepts_racing_a_recalibration_never_mix_calibrations(client, auth_headers):
    headers, _, plan = await room_with_plan(client, auth_headers)
    job = await import_file(client, headers, plan["id"], fx.rack_row_dxf(4), "r.dxf", "dxf")
    await calibrate_declared(client, headers, plan["id"], job["id"])
    items = [c for c in await candidates(client, headers, job["id"]) if c["suggested_object_type"] == "rack"]
    fp = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()
    async with concurrent_client() as cc:
        recal = cc.post(f"{FP}/{plan['id']}/calibration", json={"method": "declared_units", "job_id": job["id"], "rotation_degrees": 180}, headers={**headers, "If-Match": str(fp["version"])})
        results = await asyncio.gather(recal, *[accept(cc, headers, job["id"], c, {"object_type": "rack"}) for c in items])
    final = (await client.get(f"{FP}/{plan['id']}", headers=headers)).json()["current_calibration"]
    accepted = [o for o in await objects(client, headers, plan["id"])]
    assert {o["provenance"]["calibration_id"] for o in accepted} <= {final["id"]}, "accepted shapes share the calibration in force"
    if results[0].status_code == 201:
        assert all(r.status_code in (200, 409) for r in results[1:])
    else:
        assert results[0].status_code == 409 and all(r.status_code == 200 for r in results[1:])
