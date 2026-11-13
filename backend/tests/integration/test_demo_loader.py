"""End-to-end proof of the local demo loader against a synthetic stand-in for the demo database."""

import argparse
import os
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.domain.cooling.models import EnvironmentalSensor
from app.domain.physical.models import Equipment, Rack
from app.domain.spatial.models import FloorPlan
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from app.tools.demo_loader import cli
from app.tools.demo_loader.constants import DEDUP_PREFIX
from app.tools.demo_loader.selection import discover, outside_fingerprint
from app.tools.demo_loader.verify import verify
from tests.demo_world import PLAN_HEIGHT_MM, PLAN_WIDTH_MM, build_world


def args(**overrides) -> argparse.Namespace:
    base = {
        "command": "apply", "site_prefix": "DEMO-SITE-", "integration": [], "steps": list(cli.STEPS), "history_hours": 24.0,
        "device_history_hours": float(os.environ.get("DEMO_TEST_DEVICE_HOURS", "1")), "actor_email": None, "no_sandbox": True, "no_verify": False, "expect_racks": None,
        "expect_devices": None, "confirm_local_demo": True,
        "as_of": None, "session_factory": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
async def world(db_session, make_user):
    await make_user("loader-admin@example.com", "Passw0rd!Passw0rd", "Administrator")
    return await build_world(db_session, racks_per_site=4, devices_per_rack=5)


async def counts(db):
    return {
        "racks": (await db.execute(select(func.count()).select_from(Rack))).scalar_one(),
        "equipment": (await db.execute(select(func.count()).select_from(Equipment))).scalar_one(),
        "readings": (await db.execute(select(func.count()).select_from(TelemetryReading))).scalar_one(),
        "mappings": (await db.execute(select(func.count()).select_from(IntegrationMetricMapping))).scalar_one(),
        "sensors": (await db.execute(select(func.count()).select_from(EnvironmentalSensor))).scalar_one(),
    }


async def test_apply_builds_everything_and_a_second_run_changes_nothing(db_session, world):
    before = await counts(db_session)
    # Pin the clock: a long first run must not make the second run's "newest slot" a new slot (that is correct
    # behaviour, but not what this test is about; see test_refresh_backfills_only_missing_slots_and_is_repeatable).
    as_of = datetime.now(UTC)
    first = await cli.run_apply(db_session, args(expect_racks=12, expect_devices=60, as_of=as_of))
    assert first["ok"], first
    assert first["outside_demo_sites"]["unchanged"] is True

    racks, devices = 12, 60
    assert first["sensors"]["created"] == racks and first["sensors"]["placed"] == racks and first["sensors"]["problems"] == []
    assert first["devices"]["mappings_created"] == devices * 2
    assert [p["state"] for p in first["floorplans"]] == ["calibrated"] * 3
    assert all(p["method"] == "declared_units" and p["scale_mm_per_unit"] == 1.0 for p in first["floorplans"])
    assert all(p["match"].get("matched") == 4 for p in first["floorplans"])  # the four existing racks of each room matched

    after_first = await counts(db_session)
    assert after_first["racks"] == before["racks"] and after_first["equipment"] == before["equipment"]
    assert after_first["sensors"] == racks

    second = await cli.run_apply(db_session, args(as_of=as_of))
    assert second["ok"], second
    assert second["sensors"]["created"] == 0 and second["sensors"]["placed"] == 0 and second["sensors"]["commissioned"] == 0
    assert second["devices"]["mappings_created"] == 0
    assert [p["state"] for p in second["floorplans"]] == ["already_calibrated"] * 3
    for key in ("existing", "devices"):
        telemetry = second[key] if key == "existing" else second[key]["telemetry"]
        assert telemetry["accepted"] == 0 and telemetry["duplicate"] == 0 and telemetry["skipped_existing"] > 0
    assert await counts(db_session) == after_first


async def test_one_session_per_collector_gives_the_same_rows_as_sequential_ingest(db_session, db_engine, world):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    as_of = datetime.now(UTC)
    parallel = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, session_factory=factory, no_verify=True))
    assert parallel["ok"], parallel
    assert not parallel["existing"]["rejected"] and not parallel["sensors"]["telemetry"]["rejected"]
    series = 9 + 24
    rows = (await db_session.execute(select(func.count()).select_from(TelemetryReading)
                                     .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert rows == series * 289
    distinct = (await db_session.execute(select(func.count(func.distinct(TelemetryReading.dedup_key)))
                                         .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert distinct == rows
    sequential = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, no_verify=True))
    assert sequential["existing"]["accepted"] == 0 and sequential["sensors"]["telemetry"]["accepted"] == 0
    assert sequential["existing"]["skipped_existing"] == 9 * 289


async def test_history_is_pinned_synthetic_and_converted_through_mapping_units(db_session, world):
    await cli.run_apply(db_session, args(steps=["existing"]))
    mapping = (await db_session.execute(
        select(IntegrationMetricMapping).where(IntegrationMetricMapping.source_identifier == "probe-1.temp",
                                               IntegrationMetricMapping.integration_id == world.sites[0].integration_id))).scalar_one()
    rows = (await db_session.execute(select(TelemetryReading).where(TelemetryReading.mapping_id == mapping.id)
                                     .order_by(TelemetryReading.occurred_at))).scalars().all()
    simulated = [r for r in rows if r.dedup_key.startswith(DEDUP_PREFIX)]
    assert len(simulated) >= 280  # 24 h at 300 s
    assert all(r.contract_evidence == "pinned" and r.mapping_revision_id == mapping.current_revision_id for r in simulated)
    assert all(r.attributes["synthetic"] is True and r.attributes["provenance"] == "demo_loader" for r in simulated)
    assert all(r.unit == "degC" and r.raw_unit == "degF" for r in simulated)  # stored canonical, sent in the source unit
    assert all(r.external_identifier == "ext-probe-1.temp" for r in simulated)  # continues the real series
    values = [float(r.value) for r in simulated]
    assert max(values) - min(values) > 0.5  # varies over the day
    assert 15 < min(values) and max(values) < 35  # anchored near the real 72.5 degF reading
    slots = [int(r.occurred_at.timestamp()) for r in simulated]
    assert all(s % 300 == 0 for s in slots) and len(set(slots)) == len(slots)


async def test_sensors_floor_plans_and_heat_maps(db_session, world):
    await cli.run_apply(db_session, args())
    targets = await discover(db_session)
    report = await verify(db_session, targets, expected_racks=12, expected_devices=60)
    failed = [c for c in report.checks if not c.ok]
    assert not failed, failed
    for plan in report.data["plans"]:
        for metric in ("temperature_c", "humidity_percent"):
            assert plan[metric]["cells"] > 0 and plan[metric]["contributing"] == 4
    # Racks keep their position: the calibration changed nothing about placement.
    plans = (await db_session.execute(select(FloorPlan).where(FloorPlan.id.in_([s.plan_id for s in world.sites])))).scalars().all()
    assert all(p.current_calibration_id and p.room_width_mm == PLAN_WIDTH_MM and p.room_height_mm == PLAN_HEIGHT_MM for p in plans)


async def test_hrc_site_is_never_touched(db_session, world):
    fingerprint = await outside_fingerprint(db_session)
    await cli.run_apply(db_session, args())
    assert (await outside_fingerprint(db_session)) == fingerprint
    hrc_mappings = (await db_session.execute(select(func.count()).select_from(TelemetryReading)
                                             .where(TelemetryReading.integration_id == world.hrc.integration_id))).scalar_one()
    assert hrc_mappings == 3  # only the three seed readings
    hrc_plan = await db_session.get(FloorPlan, world.hrc.plan_id)
    assert hrc_plan.current_calibration_id is None


async def test_refresh_backfills_only_missing_slots_and_is_repeatable(db_session, world):
    await cli.run_apply(db_session, args(steps=["existing", "sensors"]))
    ns = argparse.Namespace(site_prefix="DEMO-SITE-", integration=[], watch=False, catch_up_hours=24.0)
    first = await cli.run_refresh(db_session, ns)  # may add a slot if the apply crossed a 5-minute boundary
    assert first["ok"] and not first["rejected"]
    settled = await cli.run_refresh(db_session, ns)
    assert settled["ok"] and settled["accepted"] == 0 and settled["duplicate"] == 0
    # Remove the newest hour (12 slots) of every simulated series: refresh must restore exactly those slots.
    removed = (await db_session.execute(text(
        "DELETE FROM telemetry_reading WHERE dedup_key LIKE 'demo-sim:v1:%' AND occurred_at > now() - interval '1 hour'"))).rowcount
    await db_session.commit()
    assert removed > 0
    restored = await cli.run_refresh(db_session, ns)
    assert restored["ok"] and restored["accepted"] == removed and not restored["rejected"]
    assert (await cli.run_refresh(db_session, ns))["accepted"] == 0


async def test_purge_removes_only_what_the_loader_created(db_session, world):
    baseline = await counts(db_session)
    await cli.run_apply(db_session, args())
    targets = await discover(db_session)
    from app.tools.demo_loader.purge import purge

    dry = await purge(db_session, targets, dry_run=True)
    assert dry.sensors == 12 and dry.mappings == 12 * 2 + 60 * 2 and dry.readings > 0
    assert await db_session.scalar(select(func.count()).select_from(EnvironmentalSensor)) == 12  # dry run changed nothing
    done = await purge(db_session, targets, dry_run=False)
    assert done.sensors == 12
    after = await counts(db_session)
    seed_readings = 9 + 3  # nine demo seed readings plus the three at HRC
    assert after["readings"] == seed_readings
    assert after == {**baseline, "readings": seed_readings}
    again = await purge(db_session, targets, dry_run=False)
    assert (again.sensors, again.mappings, again.readings) == (0, 0, 0)


async def test_production_and_unconfirmed_runs_are_refused(monkeypatch):
    from app.tools.demo_loader.selection import LoaderError

    with pytest.raises(LoaderError, match="--confirm-local-demo"):
        cli.guard(args(confirm_local_demo=False))
    settings = cli.get_settings()
    monkeypatch.setattr(type(settings), "environment", "production", raising=False)
    monkeypatch.setattr(settings, "__dict__", {**settings.__dict__, "environment": "production"})
    with pytest.raises(LoaderError, match="production"):
        cli.guard(args())
    assert Counter  # keep import used for readability of failures
    assert datetime.now(UTC) - timedelta(seconds=1) < datetime.now(UTC)


def test_as_of_must_be_timezone_aware_iso_8601_and_not_in_the_future():

    assert cli.parse_as_of("2026-11-13T12:00:00Z") == datetime(2026, 11, 13, 12, tzinfo=UTC)
    assert cli.parse_as_of("2026-11-13T14:00:00+02:00") == datetime(2026, 11, 13, 12, tzinfo=UTC)
    assert cli.parse_as_of("2026-11-13T12:00:00+00:00").tzinfo in (UTC, UTC)
    for bad in ("2026-11-13T12:00:00", "2026-11-13", "yesterday", "2999-01-01T00:00:00Z"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_as_of(bad)
    parsed = cli.build_parser().parse_args(["apply", "--confirm-local-demo", "--as-of", "2026-11-13T12:00:00Z"])
    assert parsed.as_of == datetime(2026, 11, 13, 12, tzinfo=UTC)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["apply", "--confirm-local-demo", "--as-of", "2026-11-13T12:00:00"])
    with pytest.raises(SystemExit):  # refresh has no --as-of: continuous refresh always uses the live clock
        cli.build_parser().parse_args(["refresh", "--confirm-local-demo", "--as-of", "2026-11-13T12:00:00Z"])


async def test_refresh_uses_the_live_clock(db_session, world, monkeypatch):
    seen = {}
    real = cli.simulate

    async def spy(db, series, **kwargs):
        seen.update(kwargs)
        return await real(db, series, **kwargs)

    monkeypatch.setattr(cli, "simulate", spy)
    await cli.run_refresh(db_session, argparse.Namespace(site_prefix="DEMO-SITE-", integration=[], watch=False, catch_up_hours=1.0,
                                                         as_of=datetime(2020, 1, 1, tzinfo=UTC), session_factory=None))
    assert seen["now"] is None  # even a stray as_of attribute cannot freeze a refresh
    newest = (await db_session.execute(select(func.max(TelemetryReading.occurred_at))
                                       .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert datetime.now(UTC) - newest < timedelta(minutes=6)


async def _fingerprint(db):
    rows = (await db.execute(
        select(IntegrationMetricMapping.source_identifier, TelemetryReading.occurred_at, TelemetryReading.value, TelemetryReading.unit,
               TelemetryReading.raw_value_text, TelemetryReading.contract_evidence)
        .join(IntegrationMetricMapping, IntegrationMetricMapping.id == TelemetryReading.mapping_id)
        .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).all()
    return sorted((str(r[0]), r[1], str(r[2]), r[3], r[4], r[5]) for r in rows)


async def test_independently_seeded_parallel_and_sequential_runs_store_identical_data(db_session, db_engine, world):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.tools.demo_loader.purge import purge

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    as_of = datetime.now(UTC)
    steps = ["existing", "sensors", "devices"]
    await cli.run_apply(db_session, args(steps=steps, as_of=as_of, session_factory=factory, no_verify=True, device_history_hours=2.0))
    parallel = await _fingerprint(db_session)
    assert len(parallel) == (9 + 24) * 289 + 120 * 25  # devices: 2 h of 5-minute slots, both ends included
    # Roll everything the loader created back, then seed again from scratch, one collector at a time.
    await purge(db_session, await discover(db_session), dry_run=False)
    assert await _fingerprint(db_session) == []
    await cli.run_apply(db_session, args(steps=steps, as_of=as_of, session_factory=None, no_verify=True, device_history_hours=2.0))
    assert await _fingerprint(db_session) == parallel


async def test_a_failing_collector_is_reported_and_a_rerun_resumes_without_duplicates(db_session, db_engine, world, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.tools.demo_loader import series as series_module

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    as_of = datetime.now(UTC)
    real = series_module.ingest_collector_telemetry
    calls = {"n": 0}

    async def flaky(db, *, collector, body):
        if collector.name == "collector-DEMO-SITE-02":
            calls["n"] += 1
            if calls["n"] == 2:  # the first batch commits, the second fails
                raise RuntimeError("simulated database outage")
        return await real(db, collector=collector, body=body)

    monkeypatch.setattr(series_module, "ingest_collector_telemetry", flaky)
    broken = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, session_factory=factory, no_verify=True))
    assert broken["ok"] is False
    failures = {**broken["existing"]["failed_collectors"], **broken["sensors"]["telemetry"]["failed_collectors"]}
    assert set(failures) == {"collector-DEMO-SITE-02"} and "simulated database outage" in failures["collector-DEMO-SITE-02"]
    per_collector = dict((await db_session.execute(
        select(TelemetryReading.collector_id, func.count()).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX))
        .group_by(TelemetryReading.collector_id))).all())
    healthy = [c for c in world.sites if c.code != "DEMO-SITE-02"]
    assert all(per_collector[c.collector_id] > 0 for c in healthy)
    assert per_collector[world.sites[1].collector_id] < per_collector[healthy[0].collector_id]  # the failed one is incomplete
    monkeypatch.setattr(series_module, "ingest_collector_telemetry", real)
    resumed = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, session_factory=factory, no_verify=True))
    assert resumed["ok"], resumed
    total = (await db_session.execute(select(func.count()).select_from(TelemetryReading)
                                      .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    distinct = (await db_session.execute(select(func.count(func.distinct(TelemetryReading.dedup_key)))
                                         .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert total == distinct == (9 + 24) * 289


async def test_an_unassigned_integration_is_rejected_per_record_and_reported(db_session, world):
    await db_session.execute(text("UPDATE collector_assignment SET effective_to = now() WHERE integration_id = :i"),
                             {"i": world.sites[0].integration_id})
    await db_session.commit()
    from app.tools.demo_loader.selection import LoaderError

    # Without a current assignment the loader cannot choose a collector: it stops before ingesting anything.
    with pytest.raises(LoaderError, match="no current collector assignment"):
        await cli.run_apply(db_session, args(steps=["existing"], no_verify=True))
    assert await db_session.scalar(select(func.count()).select_from(TelemetryReading).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX))) == 0


async def test_one_process_per_site_can_run_concurrently_without_touching_the_other_sites(db_session, db_engine, world):
    """The PowerShell runbook starts one loader process per site (`--site-prefix DEMO-SITE-0n`). Each must change only
    its own site, report the HRC site and the other demo sites as unchanged by the protected prefix, and together they
    must produce exactly what one run over all sites produces."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    as_of = datetime.now(UTC)
    fingerprint = await outside_fingerprint(db_session)

    async def one_site(code: str):
        async with factory() as session:
            return await cli.run_apply(session, args(site_prefix=code, steps=["existing", "sensors"], as_of=as_of, no_verify=True))

    results = await asyncio.gather(*(one_site(s.code) for s in world.sites))
    assert all(r["ok"] and r["outside_demo_sites"]["unchanged"] is True for r in results)
    assert [r["targets"]["sites"] for r in results] == [[s.code] for s in world.sites]
    rows = (await db_session.execute(select(func.count()).select_from(TelemetryReading)
                                     .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert rows == (9 + 24) * 289
    assert await outside_fingerprint(db_session) == fingerprint
    assert await db_session.scalar(select(func.count()).select_from(EnvironmentalSensor)) == 12
