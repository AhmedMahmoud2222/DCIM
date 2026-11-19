"""GET /api/v1/dashboard/geography (Issue #365): scope-aware coverage with exact totals and a bounded site list."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

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
    assert body["coordinates"] == "included"


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
    assert body["coordinates"] == "included"
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


def _enable_restricted_coordinates(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(geography_restricted_coordinates_enabled=True))


RESTRICTED_LAYOUT = {
    "OrgA": {"Egypt": {"Cairo": [("A-1", 30.0444, 31.2357), ("A-2", 30.1, 31.1)], "Alexandria": [("A-3", 31.2, 29.9)]}},
    "OrgB": {"France": {"Paris": [("B-1", 48.8, 2.3)]}},
}
HIDDEN_NAMES = ("A-2", "A-3", "B-1", "Alexandria", "Paris", "France", "OrgA", "OrgB")
# Coordinates of the sites the restricted user may not see (none is a substring of 30.0444 / 31.2357).
HIDDEN_COORDINATES = ("30.1", "31.1", "29.9", "48.8")


async def _restricted_to_a1(client, admin, db_session):
    ids = await _world(db_session, RESTRICTED_LAYOUT)
    gid = await _group(client, admin, allow=["location:read"], sites=[{"site_id": str(ids["A-1"]), "rack_scope": "all"}])
    _, restricted = await _group_user(client, admin, [gid])
    return ids, restricted


def _assert_only_a1_in_scope(r, ids):
    body = r.json()
    assert body["totals"]["countries"] == 1 and body["totals"]["cities"] == 1 and body["totals"]["sites"] == 1
    assert body["countries"] == [{"id": str(ids["Egypt"]), "name": "Egypt", "iso_code": "EG", "city_count": 1, "site_count": 1}]
    assert body["cities"] == [{"id": str(ids["Cairo"]), "country_id": str(ids["Egypt"]), "name": "Cairo", "site_count": 1}]
    assert [s["id"] for s in body["sites"]] == [str(ids["A-1"])]
    # as_of is removed so a timestamp can never collide with a coordinate substring.
    text = r.text.replace(body["as_of"], "")
    for hidden in HIDDEN_NAMES + HIDDEN_COORDINATES:
        assert hidden not in text


async def test_restricted_user_sees_only_granted_sites_and_no_other_organization(client, admin, db_session):
    ids, restricted = await _restricted_to_a1(client, admin, db_session)

    r = await client.get(URL, headers=restricted)
    assert r.status_code == 200, r.text
    _assert_only_a1_in_scope(r, ids)

    full = (await client.get(URL, headers=admin)).json()
    assert full["totals"]["sites"] == 4 and full["totals"]["countries"] == 2 and full["totals"]["cities"] == 3


async def test_unrestricted_admin_gets_coordinates(client, admin, db_session):
    await _world(db_session, RESTRICTED_LAYOUT)
    body = (await client.get(URL, headers=admin)).json()
    assert body["coordinates"] == "included"
    assert body["totals"]["sites_with_coordinates"] == 4 and body["totals"]["sites_without_coordinates"] == 0
    coords = {s["code"]: (s["latitude"], s["longitude"]) for s in body["sites"]}
    assert coords["A-1"] == (30.0444, 31.2357) and coords["B-1"] == (48.8, 2.3)


async def test_restricted_user_gets_coordinates_withheld_by_default(client, admin, db_session):
    ids, restricted = await _restricted_to_a1(client, admin, db_session)

    r = await client.get(URL, headers=restricted)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coordinates"] == "withheld"
    assert body["totals"] == {"countries": 1, "cities": 1, "sites": 1, "sites_with_coordinates": None, "sites_without_coordinates": None}
    assert [(s["latitude"], s["longitude"]) for s in body["sites"]] == [(None, None)]
    text = r.text.replace(body["as_of"], "")
    for digits in ("30.0444", "31.2357", "0444", "2357"):
        assert digits not in text
    _assert_only_a1_in_scope(r, ids)


async def test_restricted_user_gets_in_scope_coordinates_when_operator_enables_them(client, admin, db_session, monkeypatch):
    ids, restricted = await _restricted_to_a1(client, admin, db_session)
    _enable_restricted_coordinates(monkeypatch)

    r = await client.get(URL, headers=restricted)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coordinates"] == "included"
    assert body["totals"] == {"countries": 1, "cities": 1, "sites": 1, "sites_with_coordinates": 1, "sites_without_coordinates": 0}
    assert [(s["code"], s["latitude"], s["longitude"]) for s in body["sites"]] == [("A-1", 30.0444, 31.2357)]
    # Enabling coordinates never widens scope: hidden sites, cities, countries and their coordinates stay absent.
    _assert_only_a1_in_scope(r, ids)


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


async def _scoped_ids(client, headers, path):
    response = await client.get(f"/api/v1/{path}", headers=headers, params={"limit": 200})
    assert response.status_code == 200, (path, response.text)
    return {item["id"] for item in response.json()["items"]}


@pytest.mark.parametrize("coordinates_enabled", [False, True])
@pytest.mark.parametrize("grants", ["all", "selected_without_racks", "none"])
async def test_geography_reaches_exactly_the_entities_the_existing_scoped_location_routes_reach(
    client, admin, db_session, monkeypatch, grants, coordinates_enabled
):
    """Independent authorization check for the scope-aware route inventory (test_user_groups_authz): whatever
    scope a caller holds, geography must expose the same sites, cities and countries as GET /sites, /cities and
    /countries, which are already reviewed. It may add coordinates for those sites (for a restricted scope only when
    the operator enabled them), never another entity; the setting never changes which entities are returned."""
    if coordinates_enabled:
        _enable_restricted_coordinates(monkeypatch)
    ids = await _world(db_session, {
        "OrgA": {"Egypt": {"Cairo": [("A-1", 30, 31), ("A-2", 30.1, 31.1)], "Alexandria": [("A-3", 31.2, 29.9)]}},
        "OrgB": {"France": {"Paris": [("B-1", 48.8, 2.3)]}},
    })
    sites = {
        "all": [{"site_id": str(ids["A-1"]), "rack_scope": "all"}, {"site_id": str(ids["B-1"]), "rack_scope": "all"}],
        "selected_without_racks": [{"site_id": str(ids["A-2"]), "rack_scope": "selected", "rack_ids": []}],
        "none": [],
    }[grants]
    gid = await _group(client, admin, allow=["location:read"], sites=sites)
    _, headers = await _group_user(client, admin, [gid])

    geography = (await client.get(URL, headers=headers)).json()
    assert geography["coordinates"] == ("included" if coordinates_enabled else "withheld")
    if not coordinates_enabled:
        assert all(s["latitude"] is None and s["longitude"] is None for s in geography["sites"])
    assert {s["id"] for s in geography["sites"]} == await _scoped_ids(client, headers, "sites")
    assert {c["id"] for c in geography["cities"]} == await _scoped_ids(client, headers, "cities")
    assert {c["id"] for c in geography["countries"]} == await _scoped_ids(client, headers, "countries")
    assert geography["totals"]["sites"] == len(geography["sites"])
    assert geography["totals"]["cities"] == len(geography["cities"])
    assert geography["totals"]["countries"] == len(geography["countries"])
    # Per-city and per-country counts are over the caller's own sites only.
    by_city: dict[str, int] = {}
    for site in geography["sites"]:
        by_city[site["city_id"]] = by_city.get(site["city_id"], 0) + 1
    assert {c["id"]: c["site_count"] for c in geography["cities"]} == by_city
    if grants == "none":
        empty = 0 if coordinates_enabled else None
        assert geography["totals"] == {
            "countries": 0, "cities": 0, "sites": 0, "sites_with_coordinates": empty, "sites_without_coordinates": empty,
        }


async def test_geography_denied_when_another_group_denies_location_read(client, admin, db_session):
    ids = await _world(db_session, {"Org": {"Egypt": {"Cairo": [("D-1", 1, 1)]}}})
    granting = await _group(client, admin, allow=["location:read"], sites=[{"site_id": str(ids["D-1"]), "rack_scope": "all"}])
    denying = await _group(client, admin, deny=["location:read"])
    _, headers = await _group_user(client, admin, [granting, denying])
    assert (await client.get(URL, headers=headers)).status_code == 403
