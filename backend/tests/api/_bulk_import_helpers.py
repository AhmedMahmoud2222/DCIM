"""Shared XLSX-building helper for the bulk-import test suite — a minimal workbook with
a header row plus whatever data rows the caller supplies, mirroring the shape
`app.application.bulk_import.templates` produces (but built directly here rather than
via the real template builder, so a test controls exactly which headers/values a
malformed or adversarial upload contains)."""

import io
import uuid

from openpyxl import Workbook


async def create_room_with_codes(client, auth_headers) -> dict:
    """Like tests/api/_phase2_helpers.py::create_room, but also returns the
    site_code/building_code/floor_level/room_code the bulk-import row columns need —
    create_room only ever returns the room's id, which is no use for building a
    spreadsheet row that must resolve the same location path by its human-readable
    codes, not by id."""
    headers = await auth_headers("DCIM Manager")
    org = (await client.post("/api/v1/organizations", json={"name": f"Org-{uuid.uuid4().hex[:8]}"}, headers=headers)).json()
    country = (
        await client.post(
            "/api/v1/countries", json={"organization_id": org["id"], "name": "Testland", "iso_code": "TL"}, headers=headers
        )
    ).json()
    city = (await client.post("/api/v1/cities", json={"country_id": country["id"], "name": "Testville"}, headers=headers)).json()
    site_code = f"S-{uuid.uuid4().hex[:6]}"
    site = (
        await client.post(
            "/api/v1/sites", json={"city_id": city["id"], "code": site_code, "name": "Test Site"}, headers=headers
        )
    ).json()
    building_code = "A"
    building = (
        await client.post(
            "/api/v1/buildings", json={"site_id": site["id"], "code": building_code, "name": "Building A"}, headers=headers
        )
    ).json()
    floor_level = 1
    floor = (
        await client.post(
            "/api/v1/floors", json={"building_id": building["id"], "name": "Floor 1", "level_number": floor_level},
            headers=headers,
        )
    ).json()
    room_code = f"R-{uuid.uuid4().hex[:6]}"
    room = (
        await client.post(
            "/api/v1/rooms", json={"floor_id": floor["id"], "code": room_code, "name": "Test Room"}, headers=headers
        )
    ).json()
    return {
        "room_id": room["id"], "site_code": site_code, "building_code": building_code, "floor_level": floor_level,
        "room_code": room_code,
    }


def build_workbook(headers: list[str], rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def row_dict_to_list(headers: list[str], row: dict) -> list:
    return [row.get(h, "") for h in headers]
