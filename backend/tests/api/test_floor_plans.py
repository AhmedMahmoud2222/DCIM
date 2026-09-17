"""FloorPlan CRUD, upload/import pipeline, and candidate review queue (ARCHITECTURE_REVIEW.md
§8/§9/§10/§10a/§11; Phase 2 prompt's adversarial import matrix). Celery tasks are invoked
synchronously via `.run()` (the same pattern tests/integration/test_outbox.py uses for
`dispatch_pending_outbox_events`) rather than `.delay()`, since no worker consumes the
broker queue in this test environment — the upload endpoint's own `.delay()` call is still
exercised (it must not raise), the task body is then run directly against the real test
database to observe its effect."""

import base64
import uuid

from app.infrastructure.tasks.floorplan_import import run_floor_plan_import_job, validate_and_run_raster_import_job
from tests.api._phase2_helpers import create_room

BENIGN_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
    <rect x="10" y="20" width="60" height="100" />
    <text x="5" y="5">Server Room</text>
</svg>"""

MALICIOUS_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg">
    <script>alert(1)</script>
    <rect x="0" y="0" width="10" height="10" onclick="evil()" />
    <a href="https://evil.example.com/exfiltrate"><rect x="1" y="1" width="5" height="5" /></a>
</svg>"""

XXE_SVG = (
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    b"<svg>&xxe;</svg>"
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _run_import(job_id: str, content: bytes) -> None:
    run_floor_plan_import_job.run(job_id, base64.b64encode(content).decode("ascii"))


def _sized_svg(total_bytes: int) -> bytes:
    prefix, suffix = b'<svg xmlns="http://www.w3.org/2000/svg">', b"</svg>"
    padding = max(0, total_bytes - len(prefix) - len(suffix) - 9)
    return prefix + b"<!--" + b"A" * padding + b"-->" + suffix


async def test_create_floor_plan_and_first_revision_number(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    resp = await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["revision_number"] == 1
    assert resp.json()["status"] == "draft"


async def test_second_floor_plan_for_same_room_gets_next_revision(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    first = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    second = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    assert first["revision_number"] == 1
    assert second["revision_number"] == 2


async def test_activating_a_floor_plan_supersedes_the_previously_active_one(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    first = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    second = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()

    activate_first = await client.post(
        f"/api/v1/floor-plans/{first['id']}/activate", headers={**headers, "If-Match": "1"}
    )
    assert activate_first.status_code == 200
    assert activate_first.json()["status"] == "active"

    activate_second = await client.post(
        f"/api/v1/floor-plans/{second['id']}/activate", headers={**headers, "If-Match": "1"}
    )
    assert activate_second.status_code == 200

    refreshed_first = await client.get(f"/api/v1/floor-plans/{first['id']}", headers=headers)
    assert refreshed_first.json()["status"] == "superseded"


async def test_upload_content_sniffed_svg_is_accepted_regardless_of_filename(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()

    resp = await client.post(
        f"/api/v1/floor-plans/{floor_plan['id']}/upload",
        files={"file": ("floorplan.dat", BENIGN_SVG, "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


async def test_upload_unrecognized_content_is_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()

    resp = await client.post(
        f"/api/v1/floor-plans/{floor_plan['id']}/upload",
        files={"file": ("floorplan.svg", b"not a real image or svg file", "image/svg+xml")},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_viewer_cannot_upload(client, auth_headers):
    manager_headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=manager_headers)).json()

    viewer_headers = await auth_headers("Viewer")
    resp = await client.post(
        f"/api/v1/floor-plans/{floor_plan['id']}/upload",
        files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


async def test_benign_svg_import_produces_diagnostics_and_a_rack_candidate(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()

    _run_import(job["id"], BENIGN_SVG)

    diagnostics = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/diagnostics", headers=headers)
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["objects_discovered"] == 2  # the rect and the text element
    assert body["racks_detected"] == 1

    candidates = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)
    types = [c["suggested_object_type"] for c in candidates.json()["items"]]
    assert "rack" in types


async def test_malicious_svg_is_sanitized_not_rejected_outright(client, auth_headers):
    """§10a: dangerous content is stripped (script tags, event handlers, external
    references), producing warnings — the file is still processed, since a single
    stripped element must not fail an otherwise-legitimate floor plan import."""
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", MALICIOUS_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()

    _run_import(job["id"], MALICIOUS_SVG)

    diagnostics = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/diagnostics", headers=headers)
    body = diagnostics.json()
    assert body["unsupported_object_count"] >= 1  # the stripped <script>
    warnings_text = " ".join(body["warnings"])
    assert "script" in warnings_text
    assert "event-handler" in warnings_text or "onclick" in warnings_text
    assert "external reference" in warnings_text

    job_status = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}", headers=headers)
    assert job_status.json()["status"] == "parsed"  # not failed — sanitized, not rejected


async def test_xxe_attempt_is_rejected_outright(client, auth_headers):
    """§10a's XXE defense: a DOCTYPE (regardless of whether the entity would actually
    resolve in this sandbox) must cause outright rejection, not a best-effort parse."""
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", XXE_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()

    _run_import(job["id"], XXE_SVG)

    job_status = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}", headers=headers)
    assert job_status.json()["status"] == "failed"
    assert job_status.json()["rejection_reason"] is not None


async def test_upload_beyond_the_endpoints_blanket_size_cap_is_rejected_before_any_parsing(client, auth_headers):
    """The upload endpoint's own pre-check (max of the SVG and raster caps) rejects a
    file too large for either pipeline before it is ever base64-encoded or dispatched."""
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    oversized = _sized_svg(21 * 1024 * 1024)

    resp = await client.post(
        f"/api/v1/floor-plans/{floor_plan['id']}/upload",
        files={"file": ("floorplan.svg", oversized, "image/svg+xml")},
        headers=headers,
    )
    assert resp.status_code == 413


async def test_svg_over_the_svg_specific_cap_is_accepted_then_fails_async_with_a_reason(client, auth_headers):
    """Below the endpoint's blanket 20MB cap but above sanitize_svg's own 5MB SVG-specific
    cap: accepted at upload time (§36's async design), rejected with a diagnosable reason
    once the import task actually runs — never a silently-successful oversized import."""
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    oversized = _sized_svg(6 * 1024 * 1024)
    assert 5 * 1024 * 1024 < len(oversized) < 20 * 1024 * 1024

    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", oversized, "image/svg+xml")},
            headers=headers,
        )
    ).json()
    assert job["status"] == "queued"

    _run_import(job["id"], oversized)

    job_status = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}", headers=headers)
    assert job_status.json()["status"] == "failed"
    assert "size limit" in job_status.json()["rejection_reason"]


async def test_accepting_a_candidate_creates_an_authoritative_spatial_object(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()
    _run_import(job["id"], BENIGN_SVG)

    candidates = (await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)).json()
    rack_candidate = next(c for c in candidates["items"] if c["suggested_object_type"] == "rack")

    accept = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{rack_candidate['id']}/accept",
        json={"object_type": "rack", "label": "Row A Rack 1"},
        headers=headers,
    )
    assert accept.status_code == 200
    assert accept.json()["status"] == "accepted"
    assert accept.json()["resulting_spatial_object_id"] is not None

    objects = await client.get(f"/api/v1/floor-plans/{floor_plan['id']}/objects", headers=headers)
    assert any(o["source"] == "imported" for o in objects.json())


async def test_accepting_a_candidate_with_invalid_object_type_is_a_clean_422(client, auth_headers):
    """Adversarial: object_type is client-controlled input — an arbitrary string must be
    rejected at the API boundary (matching SpatialObject's object_type_allowed CHECK
    constraint), not reach the INSERT and surface as a generic 409."""
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()
    _run_import(job["id"], BENIGN_SVG)
    candidates = (await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)).json()
    candidate_id = candidates["items"][0]["id"]

    resp = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{candidate_id}/accept",
        json={"object_type": "'; DROP TABLE spatial_object; --"},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_engineer_cannot_accept_or_reject_candidates(client, auth_headers):
    """floor_plan:manage (not floor_plan:import) gates the accept/reject decision —
    Engineer can upload but must not be able to unilaterally promote imported geometry."""
    manager_headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=manager_headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=manager_headers,
        )
    ).json()
    _run_import(job["id"], BENIGN_SVG)
    candidates = (await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=manager_headers)).json()
    candidate_id = candidates["items"][0]["id"]

    engineer_headers = await auth_headers("Engineer")
    resp = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{candidate_id}/accept",
        json={"object_type": "imported_shape"},
        headers=engineer_headers,
    )
    assert resp.status_code == 403


async def test_accepting_an_already_accepted_candidate_is_a_conflict(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()
    _run_import(job["id"], BENIGN_SVG)
    candidates = (await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)).json()
    candidate_id = candidates["items"][0]["id"]

    first = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{candidate_id}/accept",
        json={"object_type": "imported_shape"}, headers=headers,
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{candidate_id}/accept",
        json={"object_type": "imported_shape"}, headers=headers,
    )
    assert second.status_code == 409


async def test_rejecting_a_candidate_never_creates_a_spatial_object(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.svg", BENIGN_SVG, "image/svg+xml")},
            headers=headers,
        )
    ).json()
    _run_import(job["id"], BENIGN_SVG)
    candidates = (await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)).json()
    candidate_id = candidates["items"][0]["id"]

    reject = await client.post(
        f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates/{candidate_id}/reject", headers=headers
    )
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"
    assert reject.json()["resulting_spatial_object_id"] is None

    objects = await client.get(f"/api/v1/floor-plans/{floor_plan['id']}/objects", headers=headers)
    assert objects.json() == []


async def test_raster_import_is_calibration_only_with_no_shape_candidates(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    room_id = await create_room(client, auth_headers)
    floor_plan = (await client.post("/api/v1/floor-plans", json={"room_id": room_id}, headers=headers)).json()
    job = (
        await client.post(
            f"/api/v1/floor-plans/{floor_plan['id']}/upload",
            files={"file": ("floorplan.png", PNG_HEADER, "image/png")},
            headers=headers,
        )
    ).json()

    validate_and_run_raster_import_job.run(job["id"], base64.b64encode(PNG_HEADER).decode("ascii"), "png")

    diagnostics = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/diagnostics", headers=headers)
    assert diagnostics.json()["objects_discovered"] == 0
    candidates = await client.get(f"/api/v1/floor-plans/import-jobs/{job['id']}/candidates", headers=headers)
    assert candidates.json()["items"] == []


async def test_get_nonexistent_floor_plan_is_404(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    resp = await client.get(f"/api/v1/floor-plans/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
