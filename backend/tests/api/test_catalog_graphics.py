"""Phase 10A PR-5: catalog revision graphics (front/rear images) and markers, end to end
via the real HTTP boundary — matching test_catalog_designer_lifecycle.py's convention.
Schema (catalog_graphic/catalog_graphic_marker) and ORM models were already merged in
PR-3/#16 (migration 0019_catalog_graphics); this file exercises the upload/marker-CRUD
routes and service logic PR-5 adds on top of them."""

import io
import uuid

from PIL import Image


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(width: int = 4, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(30, 200, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _viewer(auth_headers) -> dict:
    return await auth_headers("Viewer")


async def _make_manufacturer(client, headers, name: str | None = None) -> str:
    resp = await client.post(
        "/api/v1/catalog/manufacturers", json={"name": name or f"Graphics Co {uuid.uuid4().hex[:8]}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_model(client, headers, manufacturer_id: str, *, category: str = "rack") -> str:
    resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": category, "model_name": f"Model-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_draft(client, headers, model_id: str) -> dict:
    resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_port(client, headers, revision: dict, *, stable_key: str = "eth0") -> dict:
    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/network-ports",
        json={
            "stable_key": stable_key, "display_name": stable_key, "media_type": "copper",
            "supported_speeds_mbps": [1000], "connector_type": "rj45", "side": "front",
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload(client, headers, revision: dict, side: str, content: bytes, filename: str = "photo.png") -> dict:
    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{side}",
        files={"file": (filename, content, "application/octet-stream")},
        headers={**headers, "If-Match": str(revision["version"])},
    )
    return resp


# ------------------------------------------------------------------------ Upload


async def test_upload_png_graphic_succeeds_and_appears_in_revision_detail(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await _upload(client, headers, revision, "front", _png_bytes(width=8, height=6))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["side"] == "front"
    assert body["mime_type"] == "image/png"
    assert body["width_px"] == 8
    assert body["height_px"] == 6
    assert body["markers"] == []

    detail = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    graphics = detail.json()["graphics"]
    assert len(graphics) == 1
    assert graphics[0]["side"] == "front"


async def test_upload_jpeg_graphic_succeeds(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await _upload(client, headers, revision, "rear", _jpeg_bytes(), filename="photo.jpg")
    assert resp.status_code == 201, resp.text
    assert resp.json()["mime_type"] == "image/jpeg"


async def test_upload_rejects_content_that_is_not_png_or_jpeg(client, auth_headers):
    """Content-sniffed, never filename-based — a .png extension on non-image bytes is
    still rejected, matching app/api/v1/floor_plans.py's established discipline."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await _upload(client, headers, revision, "front", b"not an image, just plain text bytes")
    assert resp.status_code == 422, resp.text
    assert "not recognized" in resp.json()["detail"]


async def test_upload_rejects_payload_over_the_size_cap(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1)
    resp = await _upload(client, headers, revision, "front", oversized)
    assert resp.status_code == 422, resp.text
    assert "size limit" in resp.json()["detail"]


async def test_upload_rejects_invalid_side_path_segment(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await _upload(client, headers, revision, "top", _png_bytes())
    assert resp.status_code == 422, resp.text  # FastAPI Literal["front","rear"] path validation


async def test_upload_requires_catalog_administrator(client, auth_headers):
    headers = await _viewer(auth_headers)
    admin_headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, admin_headers)
    model_id = await _make_model(client, admin_headers, manufacturer_id)
    revision = await _make_draft(client, admin_headers, model_id)

    resp = await _upload(client, headers, revision, "front", _png_bytes())
    assert resp.status_code == 403, resp.text


async def test_upload_requires_if_match(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/front",
        files={"file": ("photo.png", _png_bytes(), "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 428, resp.text


async def test_re_upload_to_same_side_replaces_graphic_and_cascades_its_markers(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    first = await _upload(client, headers, revision, "front", _png_bytes(width=8, height=8))
    assert first.status_code == 201, first.text
    graphic_id = first.json()["id"]
    revision_version = first.json()["revision_version"]

    marker = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic_id}/markers",
        json={"marker_type": "other", "label": "note", "marker_x": 0.5, "marker_y": 0.5},
        headers={**headers, "If-Match": str(revision_version)},
    )
    assert marker.status_code == 201, marker.text
    revision_version = marker.json()["revision_version"]

    revision = {**revision, "version": revision_version}
    second = await _upload(client, headers, revision, "front", _png_bytes(width=16, height=16))
    assert second.status_code == 201, second.text
    new_graphic = second.json()
    assert new_graphic["id"] != graphic_id
    assert new_graphic["width_px"] == 16
    assert new_graphic["markers"] == []


# ------------------------------------------------------------------------ File / thumbnail streaming


async def test_get_graphic_file_and_thumbnail_stream_bytes(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    original = _png_bytes(width=32, height=32)
    upload = await _upload(client, headers, revision, "front", original)
    assert upload.status_code == 201, upload.text

    file_resp = await client.get(f"/api/v1/catalog/revisions/{revision['id']}/graphics/front/file", headers=headers)
    assert file_resp.status_code == 200, file_resp.text
    assert file_resp.headers["content-type"] == "image/png"
    assert file_resp.content == original

    thumb_resp = await client.get(f"/api/v1/catalog/revisions/{revision['id']}/graphics/front/thumbnail", headers=headers)
    assert thumb_resp.status_code == 200, thumb_resp.text
    assert thumb_resp.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(thumb_resp.content)) as thumb_img:
        assert max(thumb_img.size) <= 320


async def test_get_graphic_file_404s_when_side_never_uploaded(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    resp = await client.get(f"/api/v1/catalog/revisions/{revision['id']}/graphics/rear/file", headers=headers)
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------------ Markers


async def test_create_network_port_marker_linked_to_an_existing_port(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    port = await _make_port(client, headers, revision)
    revision = {**revision, "version": port["revision_version"]}
    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={
            "marker_type": "network_port", "network_port_template_id": port["id"], "marker_x": 0.25, "marker_y": 0.75,
        },
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["marker_type"] == "network_port"
    assert body["network_port_template_id"] == port["id"]
    assert body["marker_x"] == 0.25
    assert body["marker_y"] == 0.75


async def test_create_freestanding_marker_without_any_fk_link(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "other", "label": "Status LED", "marker_x": 0.1, "marker_y": 0.9},
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["network_port_template_id"] is None
    assert resp.json()["power_supply_template_id"] is None


async def test_create_marker_rejects_type_target_mismatch(client, auth_headers):
    """Mirrors marker_target_matches_type (migration 0019_catalog_graphics) at the API
    boundary — a network_port marker with no network_port_template_id is a clean 422,
    not a raw IntegrityError."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "network_port", "marker_x": 0.5, "marker_y": 0.5},
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    assert resp.status_code == 422, resp.text


async def test_create_marker_rejects_port_from_a_different_revision(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision_a = await _make_draft(client, headers, model_id)
    revision_b = await _make_draft(client, headers, model_id)
    port_on_a = await _make_port(client, headers, revision_a)

    upload = await _upload(client, headers, revision_b, "front", _png_bytes())
    graphic = upload.json()

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision_b['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "network_port", "network_port_template_id": port_on_a["id"], "marker_x": 0.5, "marker_y": 0.5},
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    assert resp.status_code == 422, resp.text
    assert "does not belong to this revision" in resp.json()["detail"]


async def test_update_marker_position_and_delete(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()

    create = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "other", "label": "note", "marker_x": 0.2, "marker_y": 0.2},
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    marker = create.json()

    patch = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers/{marker['id']}",
        json={"marker_x": 0.8, "marker_y": 0.3},
        headers={**headers, "If-Match": str(marker["revision_version"])},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["marker_x"] == 0.8
    assert patch.json()["marker_y"] == 0.3

    delete = await client.delete(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers/{marker['id']}",
        headers={**headers, "If-Match": str(patch.json()["revision_version"])},
    )
    assert delete.status_code == 204, delete.text

    detail = await client.get(f"/api/v1/catalog/revisions/{revision['id']}", headers=headers)
    assert detail.json()["graphics"][0]["markers"] == []


async def test_marker_create_stale_if_match_is_rejected(client, auth_headers):
    """Same If-Match-against-the-revision's-version contract every other child mutation
    in this router uses (no version column on catalog_graphic/catalog_graphic_marker
    themselves, by design)."""
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()
    stale_version = graphic["revision_version"]

    # Bump the revision's version via an unrelated scalar edit.
    bump = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}", json={"weight_value": 5},
        headers={**headers, "If-Match": str(stale_version)},
    )
    assert bump.status_code == 200, bump.text

    resp = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "other", "marker_x": 0.5, "marker_y": 0.5},
        headers={**headers, "If-Match": str(stale_version)},
    )
    assert resp.status_code == 409, resp.text


# ------------------------------------------------------------------------ Clone


async def test_clone_copies_graphics_and_repoints_markers_to_the_clones_own_ports(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)
    port = await _make_port(client, headers, revision, stable_key="eth0")
    revision = {**revision, "version": port["revision_version"]}

    fill = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={
            "dimension_unit": "mm", "width_value": 600, "height_value": 2000, "depth_value": 1000,
            "rack_unit_height": 42, "weight_unit": "kg", "weight_value": 100,
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert fill.status_code == 200, fill.text
    revision = fill.json()

    upload = await _upload(client, headers, revision, "front", _png_bytes(width=10, height=10))
    graphic = upload.json()
    marker = await client.post(
        f"/api/v1/catalog/revisions/{revision['id']}/graphics/{graphic['id']}/markers",
        json={"marker_type": "network_port", "network_port_template_id": port["id"], "marker_x": 0.4, "marker_y": 0.6},
        headers={**headers, "If-Match": str(graphic["revision_version"])},
    )
    assert marker.status_code == 201, marker.text

    publish = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish.status_code == 200, publish.text

    clone_resp = await client.post(
        f"/api/v1/catalog/models/{model_id}/revisions/clone?from_revision_id={revision['id']}", headers=headers
    )
    assert clone_resp.status_code == 201, clone_resp.text
    clone = clone_resp.json()

    clone_detail = await client.get(f"/api/v1/catalog/revisions/{clone['id']}", headers=headers)
    clone_graphics = clone_detail.json()["graphics"]
    assert len(clone_graphics) == 1
    assert clone_graphics[0]["id"] != graphic["id"]
    assert clone_graphics[0]["side"] == "front"
    assert len(clone_graphics[0]["markers"]) == 1
    cloned_marker = clone_graphics[0]["markers"][0]
    assert cloned_marker["network_port_template_id"] != port["id"]  # re-pointed at the clone's own port row

    clone_ports = (await client.get(f"/api/v1/catalog/revisions/{clone['id']}", headers=headers)).json()["network_ports"]
    assert cloned_marker["network_port_template_id"] == clone_ports[0]["id"]

    # The clone's image bytes are identical to the source's (content-addressed reuse,
    # not a re-upload) — verified by fetching both and comparing.
    source_file = await client.get(f"/api/v1/catalog/revisions/{revision['id']}/graphics/front/file", headers=headers)
    clone_file = await client.get(f"/api/v1/catalog/revisions/{clone['id']}/graphics/front/file", headers=headers)
    assert source_file.content == clone_file.content


# ------------------------------------------------------------------------ Publish validation warnings


async def test_missing_graphics_and_markers_are_non_blocking_publish_warnings(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_id = await _make_model(client, headers, manufacturer_id)
    revision = await _make_draft(client, headers, model_id)

    fill = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={
            "dimension_unit": "mm", "width_value": 600, "height_value": 2000, "depth_value": 1000,
            "rack_unit_height": 42, "weight_unit": "kg", "weight_value": 100,
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    revision = fill.json()

    no_graphics = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert no_graphics.status_code == 200, no_graphics.text
    summary = no_graphics.json()
    assert summary["valid"] is True  # PR-5 architectural directive: never blocking
    warning_codes = {(w["field"], w["code"]) for w in summary["warnings"]}
    assert ("graphics.front", "graphic_missing") in warning_codes
    assert ("graphics.rear", "graphic_missing") in warning_codes

    upload = await _upload(client, headers, revision, "front", _png_bytes())
    graphic = upload.json()
    revision = {**revision, "version": graphic["revision_version"]}

    partial = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/validate", headers=headers)
    assert partial.status_code == 200, partial.text
    summary = partial.json()
    assert summary["valid"] is True
    warning_codes = {(w["field"], w["code"]) for w in summary["warnings"]}
    assert ("graphics.rear", "graphic_missing") in warning_codes
    assert ("graphics.front", "graphic_has_no_markers") in warning_codes
    assert ("graphics.front", "graphic_missing") not in warning_codes

    # And publish itself still succeeds despite the warnings (never blocking).
    publish = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish.status_code == 200, publish.text
