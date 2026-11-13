# ruff: noqa: E501
"""Rollback: removes what the loader created, and only that.

Recognised by markers the loader writes itself: readings by their `demo-sim:v1:` dedup key, mappings by the
`demo-sim:` source prefix and the "Demo simulator" label, sensors by the `DEMO-ENV-` asset tag together with the
`demo_loader` marker in `external_ids`. Pre-existing mappings, equipment, racks, plans and everything at the HRC site
are never selected. Floor-plan calibrations are immutable by database trigger and stay (restore the backup to remove
them); the audit log is append-only and keeps its record of the run."""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.alarm.models import Alarm
from app.domain.cooling.models import EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.placement.models import EquipmentPlacement
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from app.tools.demo_loader.constants import DEDUP_PREFIX, MAPPING_LABEL, SENSOR_MARKER_KEY, SENSOR_TAG_PREFIX, SOURCE_PREFIX
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


async def purge(db: AsyncSession, targets: Targets, *, dry_run: bool) -> PurgeReport:
    report = PurgeReport(dry_run=dry_run)
    site_ids = [s.id for s in targets.sites]
    sensor_ids = list(
        (
            await db.execute(
                select(EnvironmentalSensor.id).join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id).where(
                    ManagedAsset.asset_tag.startswith(SENSOR_TAG_PREFIX), ManagedAsset.external_ids.has_key(SENSOR_MARKER_KEY),
                    EnvironmentalSensor.site_id.in_(site_ids),
                )
            )
        ).scalars()
    )
    mapping_ids = list(
        (
            await db.execute(
                select(IntegrationMetricMapping.id).where(
                    IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX), IntegrationMetricMapping.label == MAPPING_LABEL,
                    IntegrationMetricMapping.integration_id.in_([r.integration.id for r in targets.routes.values()]),
                )
            )
        ).scalars()
    )
    synthetic = select(TelemetryReading.id).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX))
    alarm_filter: ColumnElement[bool] = Alarm.telemetry_reading_id.in_(synthetic)
    if sensor_ids:
        alarm_filter = or_(alarm_filter, Alarm.managed_asset_id.in_(sensor_ids))
    report.alarms = (await db.execute(select(func.count()).select_from(Alarm).where(alarm_filter))).scalar_one()
    report.readings = (await db.execute(select(func.count()).select_from(TelemetryReading).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))).scalar_one()
    report.mappings = len(mapping_ids)
    report.sensors = len(sensor_ids)
    if dry_run:
        return report
    await db.execute(delete(Alarm).where(alarm_filter))
    await db.execute(delete(TelemetryReading).where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX)))
    if mapping_ids:
        await db.execute(delete(IntegrationMetricMapping).where(IntegrationMetricMapping.id.in_(mapping_ids)))
    if sensor_ids:
        await db.execute(delete(EquipmentPlacement).where(EquipmentPlacement.equipment_id.in_(sensor_ids)))
        await db.execute(delete(EnvironmentalSensor).where(EnvironmentalSensor.id.in_(sensor_ids)))
        await db.execute(delete(ManagedAsset).where(ManagedAsset.id.in_(sensor_ids)))
    await db.commit()
    return report
