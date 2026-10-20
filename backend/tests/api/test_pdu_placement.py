"""Issue #150 R5: PDU physical placement, end to end against real PostgreSQL.

Places/moves/retires a PDU through the new `POST|GET|DELETE /api/v1/power/pdus/{id}/placement`
endpoints. Reuses the existing locked close-then-open `equipment_placement` transaction
(`move_equipment`/`retire_equipment_placement`) -- the PDU's `equipment_id` FK already
points at `managed_asset.id`, so no PDU-table schema change is involved.

No mocks, no fake administrators: real `client`, real `make_user`/`auth_headers` RBAC,
real PostgreSQL exclusion constraints for rack-unit collision.
"""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import RackModel, RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site
from app.domain.physical.models import Rack
from app.domain.placement.models import RackPlacement
from app.domain.power.models import PDU

PASSWORD = "correct horse battery staple"


async def _seed_site_room(db_session: AsyncSession, *, room_code: str | None = None) -> uuid.UUID:
    org = Organization(name=f"Org-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    await db_session.flush()
    country = Country(organization_id=org.id, name="Testland")
    db_session.add(country)
    await db_session.flush()
    city = City(country_id=country.id, name="Testville")
    db_session.add(city)
    await db_session.flush()
    site = Site(city_id=city.id, code=f"S-{uuid.uuid4().hex[:6]}", name="Site")
    db_session.add(site)
    await db_session.flush()
    building = Building(site_id=site.id, code="A", name="Building A")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, name="Floor 1", level_number=1)
    db_session.add(floor)
    await db_session.flush()
    room = Room(floor_id=floor.id, code=room_code or f"R-{uuid.uuid4().hex[:6]}", name="Room")
    db_session.add(room)
    await db_session.flush()
    return room.id


async def _make_rack(db_session: AsyncSession, *, room_id: uuid.UUID, height_u: int = 42) -> uuid.UUID:
    asset = ManagedAsset(asset_type="rack", asset_tag=f"RACK-{uuid.uuid4().hex[:8]}")
    db_session.add(asset)
    await db_session.flush()
    model = RackModel(manufacturer="Acme", model_name=f"RM-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()
    revision = RackModelRevision(rack_model_id=model.id, height_u=height_u, width_mm=600, depth_mm=1000)
    db_session.add(revision)
    await db_session.flush()
    rack = Rack(id=asset.id, model_revision_id=revision.id, name="Rack")
    db_session.add(rack)
    await db_session.flush()
    db_session.add(
        RackPlacement(
            rack_id=asset.id, room_id=room_id, effective_from=datetime.now(UTC),
        )
    )
    await db_session.flush()
    await db_session.commit()
    return asset.id


async def _seed_pdu_asset(db_session: AsyncSession, *, asset_tag: str | None = None) -> uuid.UUID:
    asset = ManagedAsset(
        asset_type="pdu", asset_tag=asset_tag or f"PDU-{uuid.uuid4().hex[:8]}", lifecycle_status="planned"
    )
    db_session.add(asset)
    await db_session.flush()
    pdu = PDU(id=asset.id, name=asset.asset_tag, protocol="none", outlet_count=0, version=1)
    db_session.add(pdu)
    await db_session.flush()
    await db_session.commit()
    return asset.id


async def _creator(db_session: AsyncSession, make_user) -> uuid.UUID:
    user = await make_user(f"pdu-placer-{uuid.uuid4().hex[:8]}@example.com", PASSWORD, "Administrator")
    return user.id


def _url(pdu_id: uuid.UUID) -> str:
    return f"/api/v1/power/pdus/{pdu_id}/placement"


def _placement(*, rack_id, u_start, u_end, side, room_id, position=None, placement_type="rack_mounted") -> dict:
    payload = {
        "room_id": str(room_id),
        "placement_type": placement_type,
        "rack_id": str(rack_id) if rack_id is not None else None,
        "u_start": u_start,
        "u_end": u_end,
        "side": side,
        "position": position,
    }
    return {k: v for k, v in payload.items() if v is not None}


# ------------------------------------------------------------------------ Happy path


async def test_place_pdu_rack_mounted(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=room_id),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["placement_type"] == "rack_mounted"
    assert body["rack_id"] == str(rack_id)
    assert body["u_start"] == 1 and body["u_end"] == 2
    assert body["side"] == "front"
    assert body["position"] is None

    # Read surface reflects the placement.
    get_resp = await client.get(_url(pdu_id), headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["u_start"] == 1


async def test_place_pdu_vertical_zero_u(client: AsyncClient, db_session, make_user, auth_headers):
    """Vertical 0U: placement_type='other', rack_id + side, no U range, no U-capacity
    check; position recorded as mounting_method and exposed as `position`."""
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(
            rack_id=rack_id, u_start=None, u_end=None, side="rear", room_id=room_id,
            position="left-rear", placement_type="other",
        ),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["placement_type"] == "other"
    assert body["rack_id"] == str(rack_id)
    assert body["u_start"] is None and body["u_end"] is None
    assert body["side"] == "rear"
    assert body["position"] == "left-rear"


async def test_place_pdu_exceeds_rack_capacity_is_422(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=40, u_end=45, side="front", room_id=room_id),
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_place_pdu_rack_not_in_room_is_422(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    other_room_id = await _seed_site_room(db_session, room_code="OTHER")
    rack_id = await _make_rack(db_session, room_id=other_room_id, height_u=42)
    headers = await auth_headers("Administrator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=room_id),
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_place_pdu_nonexistent_room_is_404(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    rack_id = await _make_rack(db_session, room_id=await _seed_site_room(db_session), height_u=42)
    headers = await auth_headers("Administrator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=uuid.uuid4()),
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------------ Move and retire


async def test_move_pdu_closes_previous_placement(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    first = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=room_id),
        headers=headers,
    )
    assert first.status_code == 201, first.text
    first_version = first.json()["version"]

    second = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=3, u_end=4, side="rear", room_id=room_id),
        headers={**headers, "If-Match": str(first_version)},
    )
    assert second.status_code == 201, second.text
    assert second.json()["u_start"] == 3 and second.json()["u_end"] == 4

    # The placement read surface shows only the current placement.
    get_resp = await client.get(_url(pdu_id), headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["u_start"] == 3


async def test_retire_pdu_placement(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    placed = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=room_id),
        headers=headers,
    )
    assert placed.status_code == 201, placed.text

    deleted = await client.delete(_url(pdu_id), headers=headers)
    assert deleted.status_code == 204, deleted.text

    get_resp = await client.get(_url(pdu_id), headers=headers)
    assert get_resp.status_code == 404, get_resp.text

    # Idempotent: unplacing an already-unplaced PDU is a success.
    again = await client.delete(_url(pdu_id), headers=headers)
    assert again.status_code == 204, again.text


# ------------------------------------------------------------------------ Collision and concurrency


async def test_pdu_rack_unit_collision_is_rejected(client: AsyncClient, db_session, make_user, auth_headers):
    """Two PDUs may not occupy the same rack units on the same side -- the GiST
    exclusion constraint turns the overlap into a controlled 409, never a 500."""
    pdu_a = await _seed_pdu_asset(db_session, asset_tag=f"PDU-A-{uuid.uuid4().hex[:8]}")
    pdu_b = await _seed_pdu_asset(db_session, asset_tag=f"PDU-B-{uuid.uuid4().hex[:8]}")
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    first = await client.post(
        _url(pdu_a),
        json=_placement(rack_id=rack_id, u_start=5, u_end=7, side="front", room_id=room_id),
        headers=headers,
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        _url(pdu_b),
        json=_placement(rack_id=rack_id, u_start=6, u_end=8, side="front", room_id=room_id),
        headers=headers,
    )
    assert second.status_code == 409, second.text


async def test_concurrent_pdu_placement_one_wins_other_conflicts(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """Two simultaneous placements of two different PDUs into the same rack units:
    serialized by the per-asset lock + exclusion constraint; exactly one succeeds."""
    import asyncio

    pdu_a = await _seed_pdu_asset(db_session, asset_tag=f"PDU-C-{uuid.uuid4().hex[:8]}")
    pdu_b = await _seed_pdu_asset(db_session, asset_tag=f"PDU-D-{uuid.uuid4().hex[:8]}")
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Administrator")

    async def place(target_id):
        return await client.post(
            _url(target_id),
            json=_placement(rack_id=rack_id, u_start=9, u_end=10, side="front", room_id=room_id),
            headers=headers,
        )

    results = await asyncio.gather(place(pdu_a), place(pdu_b))
    statuses = sorted([r.status_code for r in results])
    assert statuses == [201, 409], statuses


# ------------------------------------------------------------------------ RBAC


async def test_operator_cannot_place_pdu(client: AsyncClient, db_session, make_user, auth_headers):
    pdu_id = await _seed_pdu_asset(db_session)
    room_id = await _seed_site_room(db_session)
    rack_id = await _make_rack(db_session, room_id=room_id, height_u=42)
    headers = await auth_headers("Operator")

    resp = await client.post(
        _url(pdu_id),
        json=_placement(rack_id=rack_id, u_start=1, u_end=2, side="front", room_id=room_id),
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
