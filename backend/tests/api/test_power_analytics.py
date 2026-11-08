"""Issue #102 areas B, C, D, E, H against PostgreSQL: rollups, snapshots, forecast, reports, authorization."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.application import power_history, power_reports
from app.domain.integration.models import Collector, Integration
from app.domain.power.analytics_models import PowerReportJob, PowerUtilizationSnapshot
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading, telemetry_series_key
from tests.api._phase2_helpers import create_equipment
from tests.api._phase3_helpers import connection_body, create_room_and_site, create_ups

P = "/api/v1/power"
A = f"{P}/analytics"


@pytest.fixture
async def add_reading(db_session):
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    collector = Collector(
        id=uuid.uuid4(), name=f"pa-{uuid.uuid4().hex}", collector_type="central", status="active",
        secret_ciphertext="test", secret_rotated_at=now,
    )
    integration = Integration(
        id=uuid.uuid4(), name=f"pa-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.1", config={},
        poll_interval_seconds=60,
    )
    mapping = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=integration.id, source_identifier="p", canonical_metric="power_kw",
        unit="kW", scale=1,
    )
    db_session.add_all((collector, integration, mapping))
    await db_session.flush()

    async def add(asset_id, value, occurred_at, metric="power_kw", unit="kW"):
        db_session.add(
            TelemetryReading(
                id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id, mapping_id=mapping.id,
                managed_asset_id=asset_id, external_identifier="x",
                series_key=telemetry_series_key(integration.id, asset_id, "x", metric, unit),
                dedup_key=uuid.uuid4().hex, metric=metric, unit=unit, value=value, occurred_at=occurred_at,
                received_at=occurred_at, attributes={},
            )
        )
        await db_session.flush()

    return add


class Plant:
    """Two UPSs (A, B) with breakers feeding one dual-corded server."""


async def build_plant(client, auth_headers, *, cap_kw=20.0):
    h = await auth_headers("DCIM Manager")
    room_id, site_id = await create_room_and_site(client, auth_headers)
    plant = Plant()
    plant.headers, plant.room_id, plant.site_id = h, room_id, site_id
    for side in ("a", "b"):
        ups = await create_ups(client, h, room_id)
        setattr(plant, f"ups_{side}", ups["id"])
        cap = await client.put(
            f"{P}/nodes/{ups['id']}/capacity", json={"rated_capacity_kw": cap_kw}, headers=h
        )
        assert cap.status_code in (200, 201), cap.text
        dev = await client.post(
            f"{P}/protection-devices",
            json={
                "housing_asset_id": ups["managed_asset_id"], "site_id": site_id, "label": f"BRK-{side.upper()}",
                "rating_a": 100, "voltage_v": 230, "poles": 1, "phase_config": "single",
            },
            headers=h,
        )
        assert dev.status_code == 201, dev.text
        setattr(plant, f"brk_{side}", dev.json())
    equipment = await create_equipment(client, h, auth_headers)
    plant.equipment_id = equipment["id"]
    for side in ("A", "B"):
        feed = await client.post(
            f"{P}/equipment-feeds", json={"equipment_asset_id": plant.equipment_id, "label": f"Feed {side}"}, headers=h
        )
        assert feed.status_code == 201, feed.text
        s = side.lower()
        setattr(plant, f"feed_{s}", feed.json()["id"])
        for src, dst in ((getattr(plant, f"ups_{s}"), getattr(plant, f"brk_{s}")["id"]), (getattr(plant, f"brk_{s}")["id"], feed.json()["id"])):
            c = await client.post(f"{P}/connections", json=connection_body(src, dst, feed_label=side), headers=h)
            assert c.status_code == 201, c.text
    return plant


def by_id(items):
    return {i["id"]: i for i in items}


@pytest.mark.asyncio
async def test_rollup_counts_dual_fed_server_once_and_tracks_failure(client, auth_headers, add_reading):
    plant = await build_plant(client, auth_headers)
    now = datetime.now(UTC)
    await add_reading(uuid.UUID(plant.equipment_id), 10.0, now - timedelta(seconds=20))
    r = (await client.get(f"{A}/sites/{plant.site_id}/rollup", headers=plant.headers)).json()
    assert r["metric"] == "power_kw" and r["unit"] == "kW"
    nodes = by_id(r["nodes"])
    assert by_id(r["equipment"])[plant.equipment_id]["scenario"] == "normal_dual_feed"
    assert nodes[plant.ups_a]["load_kw"] == pytest.approx(5.0) and nodes[plant.ups_b]["load_kw"] == pytest.approx(5.0)
    assert r["site"]["load_kw"] == pytest.approx(10.0)  # naive A+B would be 20
    assert r["site"]["capacity_kw"] == pytest.approx(40.0) and r["site"]["headroom_kw"] == pytest.approx(30.0)
    assert nodes[plant.brk_a["id"]]["capacity_kw"] == pytest.approx(23.0)  # 100 A x 230 V
    assert nodes[plant.ups_a]["quality"] == "measured"

    trip = await client.post(
        f"{P}/protection-devices/{plant.brk_a['id']}/state", json={"state": "tripped"},
        headers={**plant.headers, "If-Match": "1"},
    )
    assert trip.status_code == 200
    r = (await client.get(f"{A}/sites/{plant.site_id}/rollup", headers=plant.headers)).json()
    nodes = by_id(r["nodes"])
    assert by_id(r["equipment"])[plant.equipment_id]["scenario"] == "one_feed_failed"
    assert nodes[plant.ups_a]["load_kw"] == 0 and nodes[plant.ups_b]["load_kw"] == pytest.approx(10.0)
    assert r["site"]["load_kw"] == pytest.approx(10.0)
    assert nodes[plant.brk_a["id"]]["interrupted"] is True


@pytest.mark.asyncio
async def test_rollup_without_telemetry_is_estimated_or_missing_not_zero_confident(client, auth_headers):
    plant = await build_plant(client, auth_headers)
    r = (await client.get(f"{A}/sites/{plant.site_id}/rollup", headers=plant.headers)).json()
    eq = by_id(r["equipment"])[plant.equipment_id]
    assert eq["quality"] == "missing" and eq["demand_kw"] is None
    assert by_id(r["nodes"])[plant.ups_a]["quality"] == "missing"


@pytest.mark.asyncio
async def test_unknown_site_and_scope_are_404(client, auth_headers):
    h = await auth_headers("DCIM Manager")
    assert (await client.get(f"{A}/sites/{uuid.uuid4()}/rollup", headers=h)).status_code == 404
    assert (await client.get(f"{A}/history", params={"scope_type": "site", "scope_id": str(uuid.uuid4())}, headers=h)).status_code == 404
    assert (await client.get(f"{A}/forecast", params={"scope_type": "bogus", "scope_id": str(uuid.uuid4())}, headers=h)).status_code == 422


@pytest.mark.asyncio
async def test_snapshots_are_reproducible_idempotent_and_leave_raw_telemetry_alone(client, auth_headers, add_reading, db_session):
    plant = await build_plant(client, auth_headers)
    eq = uuid.UUID(plant.equipment_id)
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    bucket = datetime(2026, 10, 8, 10, tzinfo=UTC)
    for minute, value in ((5, 8.0), (25, 10.0), (45, 12.0)):
        await add_reading(eq, value, bucket + timedelta(minutes=minute))
    await add_reading(eq, 99.0, bucket + timedelta(hours=1, minutes=5))  # next bucket must not leak in
    await db_session.commit()
    raw_before = (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one()

    inserted = await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), bucket, now)
    await db_session.commit()
    assert inserted > 0
    site_row = (
        await db_session.execute(
            select(PowerUtilizationSnapshot).where(
                PowerUtilizationSnapshot.scope_type == "site", PowerUtilizationSnapshot.scope_id == uuid.UUID(plant.site_id)
            )
        )
    ).scalar_one()
    assert float(site_row.load_kw) == pytest.approx(10.0)  # mean of 8, 10, 12; counted once
    assert site_row.metric == "power_kw" and site_row.unit == "kW" and site_row.load_basis == "measured"
    assert site_row.sample_count == 3 and site_row.expected_samples == 60 and float(site_row.coverage_ratio) == pytest.approx(0.05)
    assert site_row.window_start == bucket and site_row.window_end == bucket + timedelta(hours=1)

    snapshot = (await db_session.execute(text("select md5(string_agg(t::text, '|' order by id::text)) from power_utilization_snapshot t"))).scalar_one()
    again = await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), bucket, now + timedelta(hours=2))
    await db_session.commit()
    assert again == 0
    assert (await db_session.execute(text("select md5(string_agg(t::text, '|' order by id::text)) from power_utilization_snapshot t"))).scalar_one() == snapshot
    assert (await db_session.execute(select(func.count()).select_from(TelemetryReading))).scalar_one() == raw_before

    with pytest.raises(ValueError):
        await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), datetime(2026, 10, 8, 12, tzinfo=UTC), now)
    with pytest.raises(ValueError):
        await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), bucket + timedelta(minutes=7), now)

    hist = await client.get(
        f"{A}/history",
        params={"scope_type": "site", "scope_id": plant.site_id, "start": (bucket - timedelta(hours=1)).isoformat(), "end": (bucket + timedelta(hours=2)).isoformat()},
        headers=plant.headers,
    )
    assert hist.status_code == 200 and hist.json()["total"] == 1
    item = hist.json()["items"][0]
    assert item["unit"] == "kW" and item["load_kw"] == pytest.approx(10.0)
    too_wide = await client.get(
        f"{A}/history",
        params={"scope_type": "site", "scope_id": plant.site_id, "start": "2026-01-01T00:00:00+00:00", "end": "2026-10-01T00:00:00+00:00"},
        headers=plant.headers,
    )
    assert too_wide.status_code == 422


@pytest.mark.asyncio
async def test_snapshot_scope_survives_node_retirement(client, auth_headers, add_reading, db_session):
    plant = await build_plant(client, auth_headers)
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    bucket = datetime(2026, 10, 8, 10, tzinfo=UTC)
    await add_reading(uuid.UUID(plant.equipment_id), 6.0, bucket + timedelta(minutes=5))
    await db_session.commit()
    await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), bucket, now)
    await db_session.commit()
    assert (await client.post(f"{P}/nodes/{plant.brk_a['id']}/retire", headers=plant.headers)).status_code == 200
    hist = await client.get(
        f"{A}/history",
        params={"scope_type": "power_node", "scope_id": plant.brk_a["id"], "start": (bucket - timedelta(hours=1)).isoformat(), "end": (bucket + timedelta(hours=2)).isoformat()},
        headers=plant.headers,
    )
    assert hist.status_code == 200 and hist.json()["total"] == 1


@pytest.mark.asyncio
async def test_forecast_endpoint_missing_then_good(client, auth_headers, db_session):
    plant = await build_plant(client, auth_headers)
    q = {"scope_type": "site", "scope_id": plant.site_id}
    empty = (await client.get(f"{A}/forecast", params=q, headers=plant.headers)).json()
    assert empty["status"] == "missing" and empty["no_forecast_reason"] and empty["exhaustion_date"] is None
    now = datetime.now(UTC)
    top = now.replace(minute=0, second=0, microsecond=0)
    for i in range(24 * 20):
        start = top - timedelta(hours=24 * 20 - i)
        load = 10.0 + 0.5 * (i / 24)
        db_session.add(
            PowerUtilizationSnapshot(
                id=uuid.uuid4(), granularity="hour", bucket_start=start, bucket_end=start + timedelta(hours=1),
                scope_type="site", scope_id=uuid.UUID(plant.site_id), site_id=uuid.UUID(plant.site_id), metric="power_kw",
                unit="kW", load_kw=load, load_basis="measured", effective_capacity_kw=40, headroom_kw=40 - load,
                utilization_pct=load / 40 * 100, sample_count=60, expected_samples=60, coverage_ratio=1, quality="measured",
                window_start=start, window_end=start + timedelta(hours=1), method_version="1", computed_at=now,
            )
        )
    await db_session.commit()
    good = (await client.get(f"{A}/forecast", params=q, headers=plant.headers)).json()
    assert good["status"] == "good" and good["method"] == "linear_ols_v1"
    assert good["slope_kw_per_day"] == pytest.approx(0.5, abs=0.05) and 40 < good["days_to_exhaustion"] < 65
    assert good["sample_count"] == 20 * 24 * 60 and good["confidence"] == "high"


@pytest.fixture
def no_broker(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.power_analytics.generate_power_report.apply_async", lambda **kw: sent.append(kw))
    return sent


@pytest.mark.asyncio
async def test_report_lifecycle_json_csv_idempotent_and_owner_only(client, auth_headers, db_session, no_broker, add_reading):
    plant = await build_plant(client, auth_headers)
    await add_reading(uuid.UUID(plant.equipment_id), 10.0, datetime(2026, 10, 8, 10, 5, tzinfo=UTC))
    await db_session.commit()
    await power_history.write_site_bucket(db_session, uuid.UUID(plant.site_id), datetime(2026, 10, 8, 10, tzinfo=UTC), datetime(2026, 10, 8, 12, tzinfo=UTC))
    await db_session.commit()
    await client.post(
        f"{P}/protection-devices/{plant.brk_a['id']}/state", json={"state": "tripped"}, headers={**plant.headers, "If-Match": "1"}
    )

    created = await client.post(f"{A}/reports", json={"site_id": plant.site_id, "format": "csv"}, headers=plant.headers)
    assert created.status_code == 202 and created.json()["status"] == "queued"
    assert no_broker and no_broker[0]["queue"] == "reports"
    job_id = uuid.UUID(created.json()["id"])
    not_ready = await client.get(f"{A}/reports/{job_id}/download", headers=plant.headers)
    assert not_ready.status_code == 409

    now = datetime(2026, 10, 8, 12, 5, tzinfo=UTC)
    assert await power_reports.run_report_job(db_session, job_id, now) == "completed"
    assert await power_reports.run_report_job(db_session, job_id, now) == "skipped"  # duplicate delivery
    job = (await client.get(f"{A}/reports/{job_id}", headers=plant.headers)).json()
    assert job["status"] == "completed" and job["attempts"] == 1 and job["row_count"] > 0

    as_json = await client.get(f"{A}/reports/{job_id}/download", params={"format": "json"}, headers=plant.headers)
    body = as_json.json()
    site = body["sites"][0]
    assert body["metric"] == "power_kw" and body["unit"] == "kW"
    codes = {e["code"] for e in site["exceptions"]}
    assert "PROTECTION_TRIPPED" in codes and "REDUNDANCY_ONE_FEED_FAILED" in codes
    assert {"utilization", "exceptions", "redundancy", "protection", "headroom", "forecast", "data_quality"} <= set(site)
    assert site["forecast"]["status"] == "sparse" or site["forecast"]["status"] == "missing" or site["forecast"]["no_forecast_reason"]
    as_csv = await client.get(f"{A}/reports/{job_id}/download", headers=plant.headers)
    assert as_csv.headers["content-type"].startswith("text/csv") and "attachment" in as_csv.headers["content-disposition"]
    assert as_csv.text.splitlines()[0].startswith("site_id,section,scope_id")
    assert "PROTECTION_TRIPPED" in as_csv.text and "tripped" in as_csv.text

    # another user cannot see, fetch, download or list the job
    other = await auth_headers("DCIM Manager")
    assert (await client.get(f"{A}/reports/{job_id}", headers=other)).status_code == 404
    assert (await client.get(f"{A}/reports/{job_id}/download", headers=other)).status_code == 404
    assert (await client.get(f"{A}/reports/{uuid.uuid4()}", headers=other)).status_code == 404
    assert (await client.get(f"{A}/reports", headers=other)).json()["total"] == 0
    assert (await client.get(f"{A}/reports", headers=plant.headers)).json()["total"] == 1


@pytest.mark.asyncio
async def test_report_failure_codes_are_fixed_and_never_leak(client, auth_headers, db_session, no_broker, monkeypatch):
    plant = await build_plant(client, auth_headers)

    async def boom(*a, **k):
        raise RuntimeError("secret-internal-detail postgres://user:pw@host")

    monkeypatch.setattr(power_reports, "build_report", boom)
    job = await client.post(f"{A}/reports", json={"site_id": plant.site_id}, headers=plant.headers)
    job_id = uuid.UUID(job.json()["id"])
    assert await power_reports.run_report_job(db_session, job_id, datetime.now(UTC)) == "failed"
    got = (await client.get(f"{A}/reports/{job_id}", headers=plant.headers)).json()
    assert got["status"] == "failed" and got["failure_code"] == "GENERATION_FAILED"
    assert "secret" not in str(got)
    dl = await client.get(f"{A}/reports/{job_id}/download", headers=plant.headers)
    assert dl.status_code == 409 and "secret" not in dl.text


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_attempts_are_bounded(client, auth_headers, db_session, no_broker):
    plant = await build_plant(client, auth_headers)
    job = (await client.post(f"{A}/reports", json={"site_id": plant.site_id}, headers=plant.headers)).json()
    job_id = uuid.UUID(job["id"])
    now = datetime.now(UTC)
    claimed = await power_reports.claim_job(db_session, job_id, now)
    assert claimed is not None and claimed.status == "running"
    assert await power_reports.claim_job(db_session, job_id, now + timedelta(seconds=10)) is None  # live lease
    again = await power_reports.claim_job(db_session, job_id, now + timedelta(seconds=power_reports.LEASE_SECONDS + 1))
    assert again is not None and again.attempts == 2
    row = await db_session.get(PowerReportJob, job_id, populate_existing=True)
    row.attempts = power_reports.MAX_ATTEMPTS
    row.lease_expires_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert await power_reports.run_report_job(db_session, job_id, now) == "failed"
    final = await db_session.get(PowerReportJob, job_id, populate_existing=True)
    assert final.failure_code == "ATTEMPTS_EXHAUSTED"


@pytest.mark.asyncio
async def test_report_input_validation_and_dispatch_failure(client, auth_headers, db_session, monkeypatch):
    plant = await build_plant(client, auth_headers)
    assert (await client.post(f"{A}/reports", json={"format": "xml"}, headers=plant.headers)).status_code == 422
    assert (await client.post(f"{A}/reports", json={"site_id": str(uuid.uuid4())}, headers=plant.headers)).status_code == 404

    def down(**kw):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.api.v1.power_analytics.generate_power_report.apply_async", down)
    r = await client.post(f"{A}/reports", json={"site_id": plant.site_id}, headers=plant.headers)
    assert r.status_code == 502 and "broker" not in r.text
    row = (await db_session.execute(select(PowerReportJob))).scalars().one()
    assert row.status == "failed" and row.failure_code == "GENERATION_FAILED"


def test_csv_neutralises_formula_cells():
    result = {
        "sites": [
            {
                "site_id": "s", "site_name": "=HYPERLINK(\"x\")", "utilization": [], "exceptions": [], "redundancy": [],
                "protection": [{"device_id": "d", "label": "@cmd", "state": "closed", "status": "in_service"}],
                "headroom": [], "data_quality": [],
                "forecast": {"status": "missing", "current_load_kw": None, "capacity_kw": None, "headroom_kw": None,
                              "confidence": "none", "no_forecast_reason": "n", "exhaustion_date": None},
            }
        ]
    }
    out = power_reports.render_csv(result)
    assert "'=HYPERLINK" in out and "'@cmd" in out


# ------------------------------------------------------------------------- authorization (area H)


ENDPOINTS_READ = [
    ("GET", "/sites/{site}/rollup"), ("GET", "/history?scope_type=site&scope_id={site}"),
    ("GET", "/forecast?scope_type=site&scope_id={site}"), ("GET", "/reports"),
]


@pytest.mark.asyncio
async def test_anonymous_and_unprivileged_roles_are_denied(client, auth_headers, no_broker):
    plant = await build_plant(client, auth_headers)
    for method, path in ENDPOINTS_READ:
        url = A + path.format(site=plant.site_id)
        assert (await client.request(method, url)).status_code == 401
    viewer = await auth_headers("Viewer")  # power:read only
    for method, path in ENDPOINTS_READ:
        assert (await client.request(method, A + path.format(site=plant.site_id), headers=viewer)).status_code == 200
    assert (await client.post(f"{A}/reports", json={}, headers=viewer)).status_code == 403
    assert (await client.post(f"{A}/reports", json={})).status_code == 401
    assert (await client.post(f"{P}/protection-devices/{plant.brk_a['id']}/state", json={"state": "open"}, headers={**viewer, "If-Match": "1"})).status_code == 403
    assert (await client.patch(f"{P}/protection-devices/{plant.brk_a['id']}", json={"label": "x"}, headers={**viewer, "If-Match": "1"})).status_code == 403
    assert (await client.post(f"{P}/connections", json=connection_body(plant.ups_a, plant.brk_a["id"], feed_label="B"), headers=viewer)).status_code == 403
    assert (await client.post(f"{P}/nodes/{plant.brk_a['id']}/retire", headers=viewer)).status_code == 403
    state = (await client.get(f"{P}/protection-devices/{plant.brk_a['id']}", headers=plant.headers)).json()
    assert state["state"] == "closed" and state["version"] == 1 and state["label"] == "BRK-A"


@pytest.mark.asyncio
async def test_site_restricted_user_is_denied_everywhere_and_learns_nothing(client, auth_headers, no_broker):
    admin = await auth_headers("Administrator")
    plant = await build_plant(client, auth_headers)
    g = await client.post("/api/v1/groups", json={"name": f"G-{uuid.uuid4().hex[:8]}"}, headers=admin)
    gid = g.json()["id"]
    perms = await client.put(
        f"/api/v1/groups/{gid}/permissions",
        json={"allow": ["power:read", "power:manage", "capacity:read"], "deny": []}, headers=admin,
    )
    assert perms.status_code == 200, perms.text
    access = await client.put(
        f"/api/v1/groups/{gid}/site-access", json={"sites": [{"site_id": plant.site_id, "rack_scope": "all"}]}, headers=admin
    )
    assert access.status_code == 200, access.text
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    pw = "correct horse battery staple"
    u = await client.post("/api/v1/users", json={"email": email, "full_name": "U", "password": pw, "group_ids": [gid]}, headers=admin)
    assert u.status_code == 201, u.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    hdr = {"Authorization": f"Bearer {login.json()['access_token']}"}
    missing = str(uuid.uuid4())
    for site in (plant.site_id, missing):  # existing and non-existing ids must look identical
        for method, path in ENDPOINTS_READ:
            assert (await client.request(method, A + path.format(site=site), headers=hdr)).status_code == 403, path
    for ident in (plant.brk_a["id"], missing):
        assert (await client.get(f"{P}/protection-devices/{ident}", headers=hdr)).status_code == 403
        assert (await client.post(f"{P}/protection-devices/{ident}/state", json={"state": "open"}, headers={**hdr, "If-Match": "1"})).status_code == 403
        assert (await client.get(f"{A}/reports/{ident}", headers=hdr)).status_code == 403
        assert (await client.get(f"{A}/reports/{ident}/download", headers=hdr)).status_code == 403
    assert (await client.post(f"{A}/reports", json={"site_id": plant.site_id}, headers=hdr)).status_code == 403
    assert (await client.get(f"{P}/protection-devices", headers=hdr)).status_code == 403
    assert (await client.post(f"{P}/connections", json=connection_body(plant.ups_a, plant.brk_b["id"], feed_label="B"), headers=hdr)).status_code == 403
