# ruff: noqa: E501
"""Rollback: removes what the loader created in the selected demo sites, and only that.

One ownership-and-site predicate drives both the dry-run counts and the deletes:

* readings: dedup key `demo-sim:v1:` AND an integration routed to a selected site;
* mappings: `demo-sim:` source prefix AND the "Demo simulator" label AND a routed integration;
* sensors: `DEMO-ENV-` tag AND the `demo_loader` marker in `external_ids` AND a selected site;
* alarms: opened by a loader reading (`details.origin_provenance == demo_loader`, written by the alarm service when it
  opens the alarm), on a routed integration, and whose latest reading is still synthetic. A pre-existing alarm that a
  simulated reading later retained or cleared has no origin marker and is preserved, with its acknowledgement history.
  Its `last_value` and latest-reading link did change while the simulation ran; that is not reversible here.

Floor-plan calibrations are immutable by database trigger and stay (restore the backup to remove them); the audit log
is append-only and keeps its record of the run."""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.alarm_service import ORIGIN_KEY
from app.domain.alarm.models import Alarm
from app.domain.cooling.models import EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.placement.models import EquipmentPlacement
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from app.tools.demo_loader.constants import (
    DEDUP_PREFIX,
    MAPPING_LABEL,
    PROVENANCE,
    SENSOR_MARKER_KEY,
    SENSOR_TAG_PREFIX,
    SOURCE_PREFIX,
)
from app.tools.demo_loader.selection import Targets


@dataclass
class PurgeReport:
    alarms: int = 0
    readings: int = 0
    mappings: int = 0
    sensors: int = 0
    dry_run: bool = True

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Predicates:
    readings: ColumnElement[bool]
    alarms: ColumnElement[bool]
    mappings: ColumnElement[bool]
    sensors: ColumnElement[bool]


def predicates(targets: Targets) -> Predicates:
    integration_ids = [route.integration.id for route in targets.routes.values()]
    site_ids = [site.id for site in targets.sites]
    readings = TelemetryReading.dedup_key.startswith(DEDUP_PREFIX) & TelemetryReading.integration_id.in_(integration_ids)
    synthetic_in_scope = select(TelemetryReading.id).where(readings)
    alarms = (
        Alarm.integration_id.in_(integration_ids)
        & (Alarm.details[ORIGIN_KEY].astext == PROVENANCE)
        & (Alarm.telemetry_reading_id.is_(None) | Alarm.telemetry_reading_id.in_(synthetic_in_scope))
    )
    mappings = (
        IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX)
        & (IntegrationMetricMapping.label == MAPPING_LABEL)
        & IntegrationMetricMapping.integration_id.in_(integration_ids)
    )
    sensors = (
        ManagedAsset.asset_tag.startswith(SENSOR_TAG_PREFIX)
        & ManagedAsset.external_ids.has_key(SENSOR_MARKER_KEY)
        & EnvironmentalSensor.site_id.in_(site_ids)
    )
    return Predicates(readings=readings, alarms=alarms, mappings=mappings, sensors=sensors)


async def purge(db: AsyncSession, targets: Targets, *, dry_run: bool) -> PurgeReport:
    report = PurgeReport(dry_run=dry_run)
    pred = predicates(targets)
    sensor_ids = list(
        (await db.execute(select(EnvironmentalSensor.id).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id).where(pred.sensors))).scalars()
    )
    mapping_ids = list((await db.execute(select(IntegrationMetricMapping.id).where(pred.mappings))).scalars())
    report.alarms = (await db.execute(select(func.count()).select_from(Alarm).where(pred.alarms))).scalar_one()
    report.readings = (await db.execute(select(func.count()).select_from(TelemetryReading).where(pred.readings))).scalar_one()
    report.mappings = len(mapping_ids)
    report.sensors = len(sensor_ids)
    if dry_run:
        return report
    await db.execute(delete(Alarm).where(pred.alarms))  # before the readings they reference
    await db.execute(delete(TelemetryReading).where(pred.readings))
    if mapping_ids:
        await db.execute(delete(IntegrationMetricMapping).where(IntegrationMetricMapping.id.in_(mapping_ids)))
    if sensor_ids:
        await db.execute(delete(EquipmentPlacement).where(EquipmentPlacement.equipment_id.in_(sensor_ids)))
        await db.execute(delete(EnvironmentalSensor).where(EnvironmentalSensor.id.in_(sensor_ids)))
        await db.execute(delete(ManagedAsset).where(ManagedAsset.id.in_(sensor_ids)))
    await db.commit()
    return report
