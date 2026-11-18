"""GET /api/v1/dashboard/geography (Issue #365): scope-aware coverage with exact totals and a bounded site list."""

import uuid
from decimal import Decimal

import pytest

from app.api.v1 import dashboard
from app.domain.location.models import City, Country, Organization, Site
from tests.api.test_user_groups import _group, _group_user

URL = "/api/v1/dashboard/geography"


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _world(db, layout):
    """layout: {org_name: {country_name: {city_name: [(site_code, lat, lon), ...]}}}. Returns {name/code: id}."""
    ids: dict[str, uuid.UUID] = {}
    rows = []
    for org_name, countries in layout.items():
        org = Organization(id=uuid.uuid4(), name=org_name)
        rows.append(org)
        ids[org_name] = org.id
        for country_name, cities in countries.items():
            country = Country(id=uuid.uuid4(), organization_id=org.id, name=country_name, iso_code=country_name[:2].upper())
            rows.append(country)
            ids[country_name] = country.id
            for city_name, sites in cities.items():
                city = City(id=uuid.uuid4(), country_id=country.id, name=city_name)
                rows.append(city)
                ids[city_name] = city.id
                for code, lat, lon in sites:
                    site = Site(
                        id=uuid.uuid4(), city_id=city.id, code=code, name=f"Site {code}",
                        latitude=None if lat is None else Decimal(str(lat)),
                        longitude=None if lon is None else Decimal(str(lon)),
                    )
                    rows.append(site)
                    ids[code] = site.id
    # Parents before children: the session flushes in add order only per mapper, so flush level by level.
    for kind in (Organization, Country, City, Site):
        db.add_all([r for r in rows if isinstance(r, kind)])
        await db.flush()
    await db.commit()
    return ids


async def test_empty(client, admin):
    r = await client.get(URL, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"] == {"countries": 0, "cities": 0, "sites": 0, "sites_with_coordinates": 0, "sites_without_coordinates": 0}
    assert body["countries"] == body["cities"] == body["sites"] == [] and body["sites_truncated"] is False and body["as_of"]


async def test_distinct_counts_and_coordinate_validity(client, admin, db_session):
    ids = await _world(db_session, {
        "Org1": {
            "Egypt": {
                "Cairo": [("CAI-1", 30.04, 31.23), ("CAI-2", None, 31.2), ("CAI-3", 91, 10), ("CAI-4", 10, -180.5)],
                "Giza": [("GIZ-1", -90, 180)],
            },
            "Kenya": {"Nairobi": [("NBO-1", None, None)]},
        },
    })
    body = (await client.get(URL, headers=admin)).json()
    assert body["totals"] == {"countries": 2, "cities": 3, "sites": 6, "sites_with_coordinates": 2, "sites_without_coordinates": 4}
    assert body["countries"] == [
        {"id": str(ids["Egypt"]), "name": "Egypt", "iso_code": "EG", "city_count": 2, "site_count": 5},
        {"id": str(ids["Kenya"]), "name": "Kenya", "iso_code": "KE", "city_count": 1, "site_count": 1},
    ]
    assert [(c["name"], c["site_count"]) for c in body["cities"]] == [("Cairo", 4), ("Giza", 1), ("Nairobi", 1)]
    coords = {s["code"]: (s["latitude"], s["longitude"]) for s in body["sites"]}
    assert coords == {
        "CAI-1": (30.04, 31.23), "CAI-2": (None, None), "CAI-3": (None, None), "CAI-4": (None, None),
        "GIZ-1": (-90.0, 180.0), "NBO-1": (None, None),
    }
    # Ordered by country name, city name, site code.
    assert [s["code"] for s in body["sites"]] == ["CAI-1", "CAI-2", "CAI-3", "CAI-4", "GIZ-1", "NBO-1"]
    assert body["sites_truncated"] is False


async def test_restricted_user_sees_only_granted_sites_and_no_other_organization(client, admin, db_session):
    ids = await _world(db_session, {
        "OrgA": {"Egypt": {"Cairo": [("A-1", 30, 31), ("A-2", 30.1, 31.1)], "Alexandria": [("A-3", 31.2, 29.9)]}},
        "OrgB": {"France": {"Paris": [("B-1", 48.8, 2.3)]}},
    })
    gid = await _group(client, admin, allow=["location:read"], sites=[{"site_id": str(ids["A-1"]), "rack_scope": "all"}])
    _, restricted = await _group_user(client, admin, [gid])

    r = await client.get(URL, headers=restricted)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"] == {"countries": 1, "cities": 1, "sites": 1, "sites_with_coordinates": 1, "sites_without_coordinates": 0}
    assert body["countries"] == [{"id": str(ids["Egypt"]), "name": "Egypt", "iso_code": "EG", "city_count": 1, "site_count": 1}]
    assert body["cities"] == [{"id": str(ids["Cairo"]), "country_id": str(ids["Egypt"]), "name": "Cairo", "site_count": 1}]
    assert [s["id"] for s in body["sites"]] == [str(ids["A-1"])]
    text = r.text
    for hidden in ("A-2", "A-3", "B-1", "Alexandria", "Paris", "France", "OrgA", "OrgB"):
        assert hidden not in text

    full = (await client.get(URL, headers=admin)).json()
    assert full["totals"]["sites"] == 4 and full["totals"]["countries"] == 2 and full["totals"]["cities"] == 3


async def test_requires_location_read(client, admin, db_session):
    ids = await _world(db_session, {"Org": {"Egypt": {"Cairo": [("X-1", 1, 1)]}}})
    gid = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": str(ids["X-1"]), "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    assert (await client.get(URL, headers=headers)).status_code == 403
    assert (await client.get(URL)).status_code == 401


async def test_site_list_is_bounded_while_totals_stay_exact(client, admin, db_session):
    sites = [(f"S-{i:05d}", (i % 180) - 89, (i % 360) - 179) for i in range(dashboard.MAX_GEOGRAPHY_SITES + 5)]
    await _world(db_session, {"Org": {"Egypt": {"Cairo": sites[:1000], "Giza": sites[1000:]}}})
    body = (await client.get(URL, headers=admin)).json()
    assert body["totals"]["sites"] == dashboard.MAX_GEOGRAPHY_SITES + 5
    assert body["totals"]["sites_with_coordinates"] == dashboard.MAX_GEOGRAPHY_SITES + 5
    assert body["totals"]["cities"] == 2 and body["totals"]["countries"] == 1
    assert len(body["sites"]) == dashboard.MAX_GEOGRAPHY_SITES and body["sites_truncated"] is True
    assert [s["code"] for s in body["sites"]] == [code for code, _, _ in sites[: dashboard.MAX_GEOGRAPHY_SITES]]
    assert [c["site_count"] for c in body["cities"]] == [1000, dashboard.MAX_GEOGRAPHY_SITES + 5 - 1000]
