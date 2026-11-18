"""GET /api/v1/alarms/overview (Issue #365): exact severity counts independent of the list filter and limit."""

import uuid
from datetime import timedelta

import pytest

from app.application import access_control
from app.domain.alarm.models import Alarm
from tests.api._ops_helpers import NOW, make_integration, make_rule
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

URL = "/api/v1/alarms/overview"


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def rule(db_session):
    integration = await make_integration(db_session)
    return integration, await make_rule(db_session, integration)


async def _alarms(db, rule, specs, *, asset_id=None):
    """specs: iterable of (severity, status, opened_at). Inserted in one commit."""
    integration, alarm_rule = rule
    rows = [
        Alarm(
            id=uuid.uuid4(), rule_id=alarm_rule.id, integration_id=integration.id, managed_asset_id=asset_id,
            subject_key=f"s-{uuid.uuid4().hex}", status=status, severity=severity, opened_at=opened_at, last_value=1,
            details={"metric": alarm_rule.metric},
        )
        for severity, status, opened_at in specs
    ]
    db.add_all(rows)
    await db.commit()
    return rows


async def test_empty_overview(client, admin):
    r = await client.get(URL, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"critical": 0, "warning": 0, "info": 0, "total": 0}
    assert body["items"] == [] and body["matching_total"] == 0 and body["truncated"] is False
    assert body["limit"] == 7 and body["scope"] == {"include_acknowledged": False, "severity": None}
    assert body["as_of"]


async def test_counts_exceed_the_truncated_list_and_ignore_severity_and_limit(client, admin, db_session, rule):
    specs = (
        [("critical", "ACTIVE", NOW - timedelta(minutes=i)) for i in range(60)]
        + [("warning", "ACTIVE", NOW - timedelta(minutes=i)) for i in range(45)]
        + [("info", "ACTIVE", NOW - timedelta(minutes=i)) for i in range(5)]
        + [("critical", "ACKNOWLEDGED", NOW)] * 3
        + [("warning", "CLEARED", NOW)] * 4
    )
    await _alarms(db_session, rule, specs)
    expected = {"critical": 60, "warning": 45, "info": 5, "total": 110}

    body = (await client.get(URL, headers=admin)).json()
    assert body["counts"] == expected
    assert len(body["items"]) == 7 and body["matching_total"] == 110 and body["truncated"] is True
    assert all(item["status"] == "ACTIVE" for item in body["items"])

    for severity, total in (("critical", 60), ("warning", 45), ("info", 5)):
        body = (await client.get(URL, params={"severity": severity, "limit": 50}, headers=admin)).json()
        assert body["counts"] == expected, "the severity filter must never change the counts"
        assert body["scope"]["severity"] == severity
        assert body["matching_total"] == total and len(body["items"]) == min(total, 50)
        assert body["truncated"] is (total > 50)
        # Filtered in SQL before LIMIT: every row matches, none is missing from the newest slice.
        assert {item["severity"] for item in body["items"]} == {severity}


async def test_include_acknowledged_widens_scope_but_never_includes_cleared(client, admin, db_session, rule):
    await _alarms(db_session, rule, [
        ("critical", "ACTIVE", NOW), ("critical", "ACKNOWLEDGED", NOW - timedelta(seconds=1)),
        ("warning", "ACKNOWLEDGED", NOW - timedelta(seconds=2)), ("info", "CLEARED", NOW - timedelta(seconds=3)),
        ("critical", "CLEARED", NOW - timedelta(seconds=4)),
    ])
    active = (await client.get(URL, headers=admin)).json()
    assert active["counts"] == {"critical": 1, "warning": 0, "info": 0, "total": 1}
    assert [i["status"] for i in active["items"]] == ["ACTIVE"]

    both = (await client.get(URL, params={"include_acknowledged": "true"}, headers=admin)).json()
    assert both["scope"]["include_acknowledged"] is True
    assert both["counts"] == {"critical": 2, "warning": 1, "info": 0, "total": 3}
    assert [i["status"] for i in both["items"]] == ["ACTIVE", "ACKNOWLEDGED", "ACKNOWLEDGED"]
    assert all(i["status"] != "CLEARED" for i in both["items"])


async def test_severity_is_reported_as_stored_not_derived_from_status(client, admin, db_session, rule):
    await _alarms(db_session, rule, [("info", "ACKNOWLEDGED", NOW), ("critical", "ACTIVE", NOW - timedelta(seconds=1))])
    body = (await client.get(URL, params={"include_acknowledged": "true"}, headers=admin)).json()
    assert [(i["severity"], i["status"]) for i in body["items"]] == [("info", "ACKNOWLEDGED"), ("critical", "ACTIVE")]


async def test_identical_opened_at_is_ordered_by_id_descending(client, admin, db_session, rule):
    rows = await _alarms(db_session, rule, [("warning", "ACTIVE", NOW)] * 12)
    expected = sorted((str(a.id) for a in rows), reverse=True)
    first = (await client.get(URL, params={"limit": 5}, headers=admin)).json()
    again = (await client.get(URL, params={"limit": 5}, headers=admin)).json()
    assert [i["id"] for i in first["items"]] == expected[:5] == [i["id"] for i in again["items"]]
    assert first["matching_total"] == 12 and first["truncated"] is True


async def test_newest_first_and_limit_bounds(client, admin, db_session, rule):
    await _alarms(db_session, rule, [("critical", "ACTIVE", NOW - timedelta(hours=i)) for i in range(3)])
    body = (await client.get(URL, params={"limit": 3}, headers=admin)).json()
    opened = [i["opened_at"] for i in body["items"]]
    assert opened == sorted(opened, reverse=True) and body["truncated"] is False and body["limit"] == 3
    for bad in (0, 51, "x"):
        assert (await client.get(URL, params={"limit": bad}, headers=admin)).status_code == 422


@pytest.mark.parametrize("severity", ["CRITICAL", "major", "ACTIVE", ""])
async def test_unknown_severity_is_422(client, admin, severity):
    assert (await client.get(URL, params={"severity": severity}, headers=admin)).status_code == 422


async def test_requires_alarm_read(client, admin):
    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=["location:read"], sites=[{"site_id": site["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    assert (await client.get(URL, headers=headers)).status_code == 403
    assert (await client.get(URL)).status_code == 401


async def test_restricted_scope_hides_out_of_scope_and_unassigned_alarms(
    client, admin, auth_headers, db_session, rule, monkeypatch
):
    """alarm:read is not scope-aware today (restricted callers get 403). Declare it scope-aware, as a future change
    might, and prove the defence-in-depth clause bounds counts, matching_total AND items to visible assets."""
    site_a = await _make_site(client, admin)
    site_b = await _make_site(client, admin)
    rack_a = uuid.UUID(await _make_rack(client, admin, auth_headers, site_a["room"]))
    rack_b = uuid.UUID(await _make_rack(client, admin, auth_headers, site_b["room"]))
    visible = await _alarms(db_session, rule, [("critical", "ACTIVE", NOW), ("info", "ACTIVE", NOW)], asset_id=rack_a)
    await _alarms(db_session, rule, [("critical", "ACTIVE", NOW), ("warning", "ACTIVE", NOW)], asset_id=rack_b)
    await _alarms(db_session, rule, [("critical", "ACTIVE", NOW), ("warning", "ACTIVE", NOW)], asset_id=None)

    monkeypatch.setattr(access_control, "SCOPE_AWARE_PERMISSIONS", access_control.SCOPE_AWARE_PERMISSIONS | {"alarm:read"})
    gid = await _group(client, admin, allow=["alarm:read"], sites=[{"site_id": site_a["site"], "rack_scope": "all"}])
    _, restricted = await _group_user(client, admin, [gid])

    body = (await client.get(URL, headers=restricted)).json()
    assert body["counts"] == {"critical": 1, "warning": 0, "info": 1, "total": 2}
    assert body["matching_total"] == 2 and body["truncated"] is False
    assert {i["id"] for i in body["items"]} == {str(a.id) for a in visible}
    filtered = (await client.get(URL, params={"severity": "warning"}, headers=restricted)).json()
    assert filtered["items"] == [] and filtered["matching_total"] == 0 and filtered["counts"]["total"] == 2

    everything = (await client.get(URL, headers=admin)).json()
    assert everything["counts"] == {"critical": 3, "warning": 2, "info": 1, "total": 6}


async def test_existing_alarm_list_and_history_are_unchanged(client, admin, db_session, rule):
    await _alarms(db_session, rule, [("warning", "CLEARED", NOW), ("critical", "ACTIVE", NOW - timedelta(seconds=1))])
    assert len((await client.get("/api/v1/alarms", headers=admin)).json()) == 2
    assert len((await client.get("/api/v1/alarms/history", headers=admin)).json()["items"]) == 2
