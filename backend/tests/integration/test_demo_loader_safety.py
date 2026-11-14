"""Safety regressions for the demo loader (owner review of PR #351, 4 P1 + 7 P2).

Each test names the failure it prevents. The fixture is a small stand-in world: three DEMO-SITE sites (2 racks, 4 devices
each), their integrations, and an HRC site that must never change."""

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.alarm_service import acknowledge_alarm
from app.application.telemetry_service import ingest_reading
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.cooling.models import EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from app.tools.demo_loader import cli
from app.tools.demo_loader.constants import DEDUP_PREFIX
from app.tools.demo_loader.provision import sensor_tag
from app.tools.demo_loader.purge import purge
from app.tools.demo_loader.selection import LoaderError, discover, outside_fingerprint, validate_site_prefix
from app.tools.demo_loader.verify import VerifyOptions, verify
from tests.demo_world import build_world
from tests.integration.test_demo_loader import args

DEVICE_HOURS = float(os.environ.get("DEMO_TEST_DEVICE_HOURS", "1"))


@pytest.fixture
async def world(db_session, make_user):
    await make_user("safety-admin@example.com", "Passw0rd!Passw0rd", "Administrator")
    return await build_world(db_session, racks_per_site=2, devices_per_rack=2)


async def loader_readings(db, **where):
    stmt = select(func.count()).select_from(TelemetryReading).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX))
    for column, value in where.items():
        stmt = stmt.where(getattr(TelemetryReading, column) == value)
    return (await db.execute(stmt)).scalar_one()


# ----------------------------------------------------------------------------------------------- P1: routing


async def test_a_foreign_integration_override_is_refused_before_any_write(db_session, world):
    """--integration DEMO-SITE-01=<HRC's integration> must not route demo series through HRC."""
    fingerprint = await outside_fingerprint(db_session)
    with pytest.raises(LoaderError, match="belongs to site HRC"):
        await cli.run_apply(db_session, args(steps=["existing"], integration=[("DEMO-SITE-01", "integration-HRC")], no_verify=True))
    assert await loader_readings(db_session) == 0
    assert await outside_fingerprint(db_session) == fingerprint
    assert await db_session.scalar(select(func.count()).select_from(TelemetryReading).where(TelemetryReading.integration_id == world.hrc.integration_id)) == 3


async def test_a_name_match_does_not_override_a_conflicting_site_id(db_session, world):
    """An integration whose name says DEMO-SITE-01 but whose site_id is HRC must not be chosen for DEMO-SITE-01."""
    await db_session.execute(text("UPDATE integration SET site_id = :hrc WHERE id = :i"), {"hrc": world.hrc.site_id, "i": world.sites[0].integration_id})
    await db_session.commit()
    with pytest.raises(LoaderError, match="Cannot choose one enabled integration for DEMO-SITE-01"):
        await cli.run_apply(db_session, args(steps=["existing"], no_verify=True))
    assert await loader_readings(db_session) == 0


async def test_one_integration_cannot_serve_two_selected_sites(db_session, world):
    await db_session.execute(text("UPDATE integration SET site_id = NULL WHERE id = :i"), {"i": world.sites[0].integration_id})
    await db_session.commit()
    both = [("DEMO-SITE-01", "integration-DEMO-SITE-01"), ("DEMO-SITE-02", "integration-DEMO-SITE-01")]
    with pytest.raises(LoaderError, match="would serve both"):
        await cli.run_apply(db_session, args(steps=["existing"], integration=both, no_verify=True))
    assert await loader_readings(db_session) == 0


async def test_ambiguous_site_less_integrations_fail_closed(db_session, world):
    await db_session.execute(text("UPDATE integration SET site_id = NULL WHERE id = :i"), {"i": world.sites[0].integration_id})
    db_session.add(Integration(name="spare-DEMO-SITE-01-copy", integration_type="snmp", target_host="192.0.2.99", config={}, poll_interval_seconds=300))
    await db_session.commit()
    with pytest.raises(LoaderError, match="2 candidates"):
        await cli.run_apply(db_session, args(steps=["existing"], no_verify=True))
    assert await loader_readings(db_session) == 0


async def test_a_mapping_on_another_sites_asset_stops_the_run_before_any_write(db_session, world):
    """A shared or mis-assigned integration must not let demo simulation write readings for another site's asset."""
    db_session.add(IntegrationMetricMapping(integration_id=world.sites[0].integration_id, managed_asset_id=world.hrc.device_ids[0],
                                            source_identifier="stray.power", canonical_metric="power_kw", unit="kW", scale=1))
    await db_session.commit()
    with pytest.raises(LoaderError, match="not selected"):
        await cli.run_apply(db_session, args(steps=["existing"], no_verify=True))
    assert await loader_readings(db_session) == 0


# ----------------------------------------------------------------------------------------------- P2: site boundary


@pytest.mark.parametrize("prefix", ["HRC", "", "DEMO", "DEMO-SITE", "DEMO-SITE-%", "DEMO-SITE-0_", "%", "DEMO-SITE-0*", "DEMO-SITE-0\\"])
def test_a_site_prefix_outside_the_demo_boundary_is_refused(prefix):
    with pytest.raises(LoaderError, match="not allowed"):
        validate_site_prefix(prefix)
    with pytest.raises(LoaderError):
        cli.guard(args(site_prefix=prefix))


@pytest.mark.parametrize("prefix", ["DEMO-SITE-", "DEMO-SITE-01", "DEMO-SITE-02"])
def test_demo_prefixes_are_accepted(prefix):
    assert validate_site_prefix(prefix) == prefix


async def test_hrc_and_empty_selectors_cannot_reach_other_sites(db_session, world):
    for prefix in ("HRC", ""):
        with pytest.raises(LoaderError):
            await discover(db_session, prefix=prefix)
        with pytest.raises(LoaderError):
            await cli.run(args(command="apply", site_prefix=prefix, steps=["existing"], no_verify=True))
    assert await loader_readings(db_session) == 0


# ----------------------------------------------------------------------------------------------- P1: purge scope and alarms


async def test_purge_of_one_site_leaves_every_other_site_intact(db_session, world):
    as_of = datetime.now(UTC)
    await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, no_verify=True))

    async def snapshot():
        return {
            s.code: (
                await loader_readings(db_session, integration_id=s.integration_id),
                await db_session.scalar(select(func.count()).select_from(IntegrationMetricMapping).where(
                    IntegrationMetricMapping.integration_id == s.integration_id, IntegrationMetricMapping.label == "Demo simulator")),
                await db_session.scalar(select(func.count()).select_from(EnvironmentalSensor).where(EnvironmentalSensor.site_id == s.site_id)),
            )
            for s in world.sites
        }

    before = await snapshot()
    one = await discover(db_session, prefix="DEMO-SITE-01")
    dry = await purge(db_session, one, dry_run=True)
    assert (dry.readings, dry.mappings, dry.sensors) == (before["DEMO-SITE-01"][0], before["DEMO-SITE-01"][1], before["DEMO-SITE-01"][2])
    assert await snapshot() == before  # a dry run changes nothing
    done = await purge(db_session, one, dry_run=False)
    assert (done.readings, done.mappings, done.sensors) == (dry.readings, dry.mappings, dry.sensors)
    after = await snapshot()
    assert after["DEMO-SITE-01"] == (0, 0, 0)
    assert after["DEMO-SITE-02"] == before["DEMO-SITE-02"] and after["DEMO-SITE-03"] == before["DEMO-SITE-03"]
    assert await loader_readings(db_session, integration_id=world.hrc.integration_id) == 0


async def test_purge_keeps_a_real_alarm_that_simulated_readings_retained(db_session, world):
    """A pre-existing alarm that a simulated reading later retained must survive rollback, acknowledgement included."""
    site = world.sites[0]
    db_session.add(AlarmRule(integration_id=site.integration_id, metric="temperature_c", rule_type="threshold_high", threshold=Decimal("20"),
                             unit="degC", name="inlet high", severity="critical"))
    await db_session.commit()
    now = datetime.now(UTC)
    mapping = (await db_session.execute(select(IntegrationMetricMapping).where(
        IntegrationMetricMapping.integration_id == site.integration_id, IntegrationMetricMapping.source_identifier == "probe-1.temp"))).scalar_one()
    await ingest_reading(db_session, collector_id=site.collector_id, integration_id=site.integration_id, dedup_key="real-hot-reading",
                         external_identifier="ext-probe-1.temp", source_identifier="probe-1.temp", occurred_at=now - timedelta(minutes=9), value=73,
                         mapping_revision_id=mapping.current_revision_id)
    real = (await db_session.execute(select(Alarm).where(Alarm.integration_id == site.integration_id))).scalar_one()
    assert "origin_provenance" not in real.details  # a real device opened it
    await acknowledge_alarm(db_session, alarm=real, actor_user_id=None)
    await db_session.commit()
    real_id = real.id

    await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=now, no_verify=True, site_prefix="DEMO-SITE-01"))
    alarms = (await db_session.execute(select(Alarm).where(Alarm.integration_id == site.integration_id))).scalars().all()
    loader_made = [a for a in alarms if a.details.get("origin_provenance") == "demo_loader"]
    assert loader_made, "simulated sensor readings above the threshold should have opened loader-origin alarms"
    await db_session.refresh(real)
    assert real.status == "ACKNOWLEDGED" and real.details.get("origin_provenance") is None
    assert real.telemetry_reading_id is not None  # the simulation moved it to a simulated reading

    targets = await discover(db_session, prefix="DEMO-SITE-01")
    dry = await purge(db_session, targets, dry_run=True)
    assert dry.alarms == len(loader_made)
    await purge(db_session, targets, dry_run=False)
    survivors = {a.id: a for a in (await db_session.execute(select(Alarm))).scalars()}
    assert real_id in survivors and survivors[real_id].status == "ACKNOWLEDGED" and survivors[real_id].acknowledged_at is not None
    assert not [a for a in survivors.values() if a.details.get("origin_provenance") == "demo_loader"]
    assert survivors[real_id].telemetry_reading_id is None  # its simulated latest reading was removed; the alarm itself was not


# ----------------------------------------------------------------------------------------------- P2: failure handling


def make_flaky(series_module, real, *, collector_name, fail_on_call, after_sql):
    calls = {"n": 0}

    async def flaky(db, *, collector, body):
        if collector.name == collector_name:
            calls["n"] += 1
            if calls["n"] == fail_on_call:
                if after_sql:  # SQL has started inside the batch's transaction when it fails
                    await db.execute(text("SELECT 1"))
                raise RuntimeError("simulated failure inside a batch")
        return await real(db, collector=collector, body=body)

    return flaky


@pytest.mark.parametrize("parallel", [True, False], ids=["session-per-collector", "sequential"])
async def test_a_failure_inside_an_open_transaction_is_reported_and_healthy_collectors_finish(db_session, db_engine, world, monkeypatch, parallel):
    from app.tools.demo_loader import series as series_module

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False) if parallel else None
    as_of = datetime.now(UTC)
    real = series_module.ingest_collector_telemetry
    monkeypatch.setattr(series_module, "ingest_collector_telemetry",
                        make_flaky(series_module, real, collector_name="collector-DEMO-SITE-02", fail_on_call=1, after_sql=True))
    broken = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, session_factory=factory, no_verify=True))
    assert broken["ok"] is False
    failures = {**broken["existing"]["failed_collectors"], **broken["sensors"]["telemetry"]["failed_collectors"]}
    assert set(failures) == {"collector-DEMO-SITE-02"}
    expected_per_site = (3 + 4) * 289  # 3 existing + 2 racks x 2 metrics
    assert await loader_readings(db_session, integration_id=world.sites[0].integration_id) == expected_per_site
    assert await loader_readings(db_session, integration_id=world.sites[2].integration_id) == expected_per_site
    assert await loader_readings(db_session, integration_id=world.sites[1].integration_id) == 0
    monkeypatch.setattr(series_module, "ingest_collector_telemetry", real)
    resumed = await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=as_of, session_factory=factory, no_verify=True))
    assert resumed["ok"], resumed
    total = await loader_readings(db_session)
    distinct = (await db_session.execute(select(func.count(func.distinct(TelemetryReading.dedup_key))).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    assert total == distinct == 3 * expected_per_site


# ----------------------------------------------------------------------------------------------- P2: sensor ownership


async def test_an_unmarked_sensor_with_the_wanted_tag_is_never_adopted(db_session, world):
    rack_tag = f"{world.sites[1].code}-RACK-001"
    asset = ManagedAsset(asset_type="sensor", asset_tag=sensor_tag(rack_tag), lifecycle_status="planned")
    db_session.add(asset)
    await db_session.flush()
    db_session.add(EnvironmentalSensor(id=asset.id, site_id=world.sites[1].site_id, name="someone else's sensor", sensor_kind="temperature"))
    await db_session.commit()
    with pytest.raises(LoaderError, match="not this loader's sensor"):
        await cli.run_apply(db_session, args(steps=["sensors"], no_verify=True))
    assert await db_session.scalar(select(func.count()).select_from(EnvironmentalSensor)) == 1  # nothing was created anywhere
    await db_session.refresh(asset)
    assert asset.lifecycle_status == "planned"  # and the foreign sensor was not commissioned


def test_long_rack_tags_that_share_their_start_get_different_sensor_tags():
    base = "R" * 60
    first, second = sensor_tag(base + "-AAAA"), sensor_tag(base + "-BBBB")
    assert first != second and len(first) <= 64 and len(second) <= 64
    assert sensor_tag("RACK-001") == "DEMO-ENV-RACK-001"  # short tags are unchanged


async def test_a_sensor_marked_for_a_different_rack_is_not_adopted(db_session, world):
    first_rack = f"{world.sites[0].code}-RACK-001"
    await cli.run_apply(db_session, args(steps=["sensors"], site_prefix="DEMO-SITE-01", no_verify=True))
    asset = (await db_session.execute(select(ManagedAsset).where(ManagedAsset.asset_tag == sensor_tag(first_rack)))).scalar_one()
    asset.external_ids = {**asset.external_ids, "demo_loader": {"version": "x", "rack_id": "00000000-0000-0000-0000-000000000000"}}
    await db_session.commit()
    with pytest.raises(LoaderError, match="not this loader's sensor"):
        await cli.run_apply(db_session, args(steps=["sensors"], site_prefix="DEMO-SITE-01", no_verify=True))


# ----------------------------------------------------------------------------------------------- P2: verify is strong enough


async def seeded(db_session, world, **overrides):
    as_of = datetime.now(UTC)
    await cli.run_apply(db_session, args(steps=["existing", "sensors", "devices"], as_of=as_of, no_verify=True,
                                         device_history_hours=DEVICE_HOURS, **overrides))
    return as_of


async def run_verify(db_session, as_of, **kw):
    targets = await discover(db_session, prefix=kw.pop("prefix", "DEMO-SITE-"))
    return await verify(db_session, targets, VerifyOptions(
        as_of=as_of, hours=24.0, device_hours=kw.pop("device_hours", DEVICE_HOURS), parts=frozenset({"existing", "sensors", "devices"}), **kw))


def failed(report):
    return [c.name for c in report.checks if not c.ok]


async def test_verify_passes_on_a_complete_seed_and_fails_on_each_kind_of_gap(db_session, world):
    as_of = await seeded(db_session, world)
    clean = await run_verify(db_session, as_of, require_sensors=False)
    assert not failed(clean), [c for c in clean.checks if not c.ok]

    # a missing slot in one series
    victim = (await db_session.execute(select(TelemetryReading).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)).limit(1))).scalar_one()
    await db_session.delete(victim)
    await db_session.commit()
    assert "every expected series has every slot of its window" in failed(await run_verify(db_session, as_of, require_sensors=False))

    # an entire device metric series gone
    mapping = (await db_session.execute(select(IntegrationMetricMapping).where(IntegrationMetricMapping.source_identifier.like("demo-sim:%:load_percent")).limit(1))).scalar_one()
    await db_session.execute(text("DELETE FROM telemetry_reading WHERE mapping_id = :m"), {"m": mapping.id})
    await db_session.commit()
    report = await run_verify(db_session, as_of, require_sensors=False)
    assert "every expected series has every slot of its window" in failed(report)

    # a mapping gone entirely
    await db_session.execute(text("DELETE FROM telemetry_reading WHERE mapping_id = :m"), {"m": mapping.id})
    await db_session.execute(text("DELETE FROM integration_metric_mapping WHERE id = :m"), {"m": mapping.id})
    await db_session.commit()
    assert "every expected mapping exists" in failed(await run_verify(db_session, as_of, require_sensors=False))


async def test_verify_fails_when_the_history_is_shorter_than_requested(db_session, world):
    as_of = await seeded(db_session, world)
    report = await run_verify(db_session, as_of, device_hours=DEVICE_HOURS + 5, require_sensors=False)  # asks for 5 hours more than was seeded
    assert "every expected series has every slot of its window" in failed(report)


async def test_verify_of_a_site_that_was_never_seeded_fails_even_if_other_sites_have_data(db_session, world):
    as_of = datetime.now(UTC)
    await cli.run_apply(db_session, args(steps=["existing", "sensors", "devices"], as_of=as_of, no_verify=True, site_prefix="DEMO-SITE-01",
                                         device_history_hours=DEVICE_HOURS))
    assert not failed(await run_verify(db_session, as_of, prefix="DEMO-SITE-01", require_sensors=False))
    other = await run_verify(db_session, as_of, prefix="DEMO-SITE-02", require_sensors=False)
    assert "every expected mapping exists" in failed(other) and "every expected series has every slot of its window" in failed(other)


async def test_verify_requires_both_sensor_metrics_to_be_fresh(db_session, world):
    as_of = await seeded(db_session, world)
    stale_after = datetime.now(UTC) - timedelta(hours=1)
    humidity = (await db_session.execute(select(IntegrationMetricMapping).where(IntegrationMetricMapping.source_identifier.like("demo-sim:%:humidity_percent")).limit(1))).scalar_one()
    await db_session.execute(text("DELETE FROM telemetry_reading WHERE mapping_id = :m AND occurred_at > :t"), {"m": humidity.id, "t": stale_after})
    await db_session.commit()
    report = await run_verify(db_session, as_of)
    assert "both metrics of every sensor are fresh (<= 3 poll intervals, live clock)" in failed(report)


# ----------------------------------------------------------------------------------------------- P2: writers and rollback


async def test_purge_refuses_while_a_refresh_writer_is_running_and_works_after_it_stops(db_session, world):
    await cli.run_apply(db_session, args(steps=["existing", "sensors"], as_of=datetime.now(UTC), no_verify=True))
    watcher = asyncio.create_task(cli.run(args(command="refresh", watch=True, catch_up_hours=1.0, sequential=True, confirm_local_demo=True)))
    try:
        for _ in range(100):  # wait until the watcher holds the writer lock
            held = await db_session.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"))
            if held:
                break
            await asyncio.sleep(0.1)
        assert held, "the refresh watcher never took the writer lock"
        with pytest.raises(LoaderError, match="Cannot start"):
            await cli.run(args(command="purge", dry_run=False, confirm_local_demo=True))
        assert await loader_readings(db_session) > 0  # nothing was removed
        dry, code = await cli.run(args(command="purge", dry_run=True, confirm_local_demo=True))  # a dry run is read-only and allowed
        assert code == 0 and dry["readings"] > 0
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
    done, code = await cli.run(args(command="purge", dry_run=False, confirm_local_demo=True))
    assert code == 0 and done["readings"] > 0 and await loader_readings(db_session) == 0


async def test_a_second_writer_cannot_start_while_a_purge_holds_the_lock(db_session, world):
    async with cli.writer_lock(exclusive=True):
        with pytest.raises(LoaderError, match="a purge is running"):
            async with cli.writer_lock(exclusive=False):
                pass
    async with cli.writer_lock(exclusive=False):
        async with cli.writer_lock(exclusive=False):  # per-site processes may overlap
            pass


def test_apply_is_not_given_a_protect_prefix_option():
    """HRC protection is fixed; there is no option that could weaken it."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["apply", "--confirm-local-demo", "--protect-prefix", "HRC"])


def namespace_defaults() -> argparse.Namespace:  # keeps the import used for readers of failures
    return args()
