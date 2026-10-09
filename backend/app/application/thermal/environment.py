# ruff: noqa: E501
"""Single current-value service for spatial thermal views (Issue #105).

Every heat map, airflow view, exception list and the legacy `environment_overlay` read telemetry through
`classify_reading`, so two views can never disagree about whether a value is fresh.

Freshness policy (one place):
  fresh    age <= max(poll_interval, 1) * FRESH_POLL_MULTIPLE
  stale    older than that
  missing  the sensor is placed and expected, but has no reading at or before the snapshot cutoff
  invalid  the stored row cannot be trusted as a current value: unit differs from the metric's canonical unit
           (never reinterpreted), or the timestamp lies in the future beyond FUTURE_TOLERANCE_SECONDS

The snapshot cutoff (`as_of`) bounds which readings had been *received* by that instant, so a map is tied to one
instant and a reading stamped in the future by a faulty device is surfaced as invalid rather than silently ignored. Authorization
is applied while selecting sensors: a sensor the caller cannot see is never loaded, counted or interpolated.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access_control import AccessScope, equipment_visible_clause
from app.core.errors import NotFoundError
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.cooling.models import SENSOR_KIND_METRICS, EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Integration
from app.domain.location.models import Building, Floor, Room
from app.domain.physical.models import Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import METRIC_REGISTRY, convert_to_presentation

FRESH_POLL_MULTIPLE = 3
FUTURE_TOLERANCE_SECONDS = 120
MAX_SENSORS_PER_ROOM = 500

MEASURED_FRESH = "measured_fresh"
MEASURED_STALE = "measured_stale"
MISSING = "missing"
INVALID = "invalid"

# Sensors reporting these lifecycle states are expected to produce data. Planned / reserved sensors are not yet
# commissioned and decommissioned / removed ones are retired, so neither counts as "missing".
EXPECTED_LIFECYCLE_STATUSES = ("installed", "active", "maintenance")
DEFAULT_MAP_ROLES = ("ambient", "rack_inlet", "other")


def classify_reading(occurred_at: datetime, now: datetime, poll_interval_seconds: int) -> tuple[str, int]:
    """Return (state, age_seconds). The only freshness rule in the codebase for environmental values."""
    age = (now - occurred_at).total_seconds()
    if age < -FUTURE_TOLERANCE_SECONDS:
        return INVALID, 0
    age = max(0.0, age)
    fresh = age <= max(poll_interval_seconds, 1) * FRESH_POLL_MULTIPLE
    return (MEASURED_FRESH if fresh else MEASURED_STALE), int(age)


async def assert_room_visible(db: AsyncSession, room_id: uuid.UUID, scope: AccessScope) -> uuid.UUID:
    """The room's site id, or NotFound when the room does not exist or its site is outside `scope`. Every service entry
    point calls this first, so a hidden room is indistinguishable from a missing one even when a caller skips the
    router-level check."""
    site_id = (
        await db.execute(
            select(Building.site_id).join(Floor, Floor.building_id == Building.id).join(Room, Room.floor_id == Floor.id).where(Room.id == room_id)
        )
    ).scalar_one_or_none()
    if site_id is None or not scope.allows_site(site_id):
        raise NotFoundError(f"Room {room_id} not found.")
    return site_id


@dataclass
class SensorPoint:
    sensor_id: uuid.UUID
    asset_tag: str
    name: str
    sensor_kind: str
    measurement_role: str
    lifecycle_status: str
    rack_id: uuid.UUID | None
    placement_type: str
    x_mm: float | None
    y_mm: float | None
    position_source: str | None  # placement | rack_footprint
    position_exact: bool
    flow_direction_deg: int | None = None
    metric: str | None = None
    state: str = MISSING
    value: float | None = None
    unit: str | None = None
    presentation_value: float | None = None
    presentation_unit: str | None = None
    occurred_at: datetime | None = None
    age_seconds: int | None = None
    poll_interval_seconds: int | None = None
    integration_id: uuid.UUID | None = None
    integration_name: str | None = None
    invalid_reason: str | None = None
    active_alarm_count: int = 0

    @property
    def located(self) -> bool:
        return self.x_mm is not None and self.y_mm is not None

    @property
    def usable_for_field(self) -> bool:
        return self.state == MEASURED_FRESH and self.located and self.value is not None


@dataclass
class SensorSnapshot:
    room_id: uuid.UUID
    metric: str
    as_of: datetime
    points: list[SensorPoint] = field(default_factory=list)
    truncated: bool = False

    def count(self, state: str) -> int:
        return sum(1 for p in self.points if p.state == state)


def _rack_centre(x: int, y: int, rotation: int | None, width: int | None, depth: int | None) -> tuple[float, float, bool]:
    """Centre of a rack footprint from its placement origin. Without dimensions the origin itself is used and the
    caller reports the position as approximate."""
    if width is None or depth is None:
        return float(x), float(y), False
    w, d = (depth, width) if (rotation or 0) % 180 == 90 else (width, depth)
    return x + w / 2.0, y + d / 2.0, True


async def load_room_sensor_snapshot(
    db: AsyncSession,
    *,
    room_id: uuid.UUID,
    metric: str,
    scope: AccessScope,
    now: datetime | None = None,
    as_of: datetime | None = None,
    roles: tuple[str, ...] = DEFAULT_MAP_ROLES,
) -> SensorSnapshot:
    """Every expected sensor in `room_id` that measures `metric` and that `scope` may see, with its latest reading at
    or before `as_of`. Freshness is judged against `as_of` (the snapshot instant), so a historical snapshot is
    not reported stale merely because it is old; `as_of` defaults to and is capped at `now`."""
    now = now or datetime.now(UTC)
    as_of = min(as_of or now, now)
    await assert_room_visible(db, room_id, scope)
    definition = METRIC_REGISTRY[metric]
    kinds = [kind for kind, metrics in SENSOR_KIND_METRICS.items() if metric in metrics]

    rack_room = (
        select(RackPlacement.rack_id.label("rack_id"), RackPlacement.x_mm, RackPlacement.y_mm, RackPlacement.rotation_deg)
        .where(RackPlacement.room_id == room_id, RackPlacement.effective_to.is_(None))
        .subquery()
    )
    stmt = (
        select(
            EnvironmentalSensor, ManagedAsset, EquipmentPlacement, rack_room.c.x_mm, rack_room.c.y_mm, rack_room.c.rotation_deg,
            Rack.model_revision_id,
        )
        .join(ManagedAsset, ManagedAsset.id == EnvironmentalSensor.id)
        .join(EquipmentPlacement, EquipmentPlacement.equipment_id == EnvironmentalSensor.id)
        .outerjoin(rack_room, rack_room.c.rack_id == EquipmentPlacement.rack_id)
        .outerjoin(Rack, Rack.id == EquipmentPlacement.rack_id)
        .where(
            EquipmentPlacement.effective_to.is_(None),
            EnvironmentalSensor.sensor_kind.in_(kinds),
            EnvironmentalSensor.measurement_role.in_(roles),
            ManagedAsset.lifecycle_status.in_(EXPECTED_LIFECYCLE_STATUSES),
            or_(
                # floor / wall / ceiling placements carry their own room
                (EquipmentPlacement.placement_type != "rack_mounted") & (EquipmentPlacement.room_id == room_id),
                # rack-mounted placements follow the rack's current room (equipment_placement.room_id is not
                # updated when a rack moves)
                (EquipmentPlacement.placement_type == "rack_mounted") & rack_room.c.rack_id.is_not(None),
            ),
            equipment_visible_clause(scope, EquipmentPlacement.equipment_id),
        )
        .order_by(EnvironmentalSensor.id)
        .limit(MAX_SENSORS_PER_ROOM + 1)
    )
    rows = (await db.execute(stmt)).all()
    snapshot = SensorSnapshot(room_id=room_id, metric=metric, as_of=as_of, truncated=len(rows) > MAX_SENSORS_PER_ROOM)
    rows = rows[:MAX_SENSORS_PER_ROOM]

    rack_dims: dict[uuid.UUID, tuple[int, int]] = {}
    revision_ids = {r[6] for r in rows if r[6] is not None}
    if revision_ids:
        from app.domain.catalog.models import RackModelRevision

        rack_dims = {
            rid: (w, d)
            for rid, w, d in (
                await db.execute(
                    select(RackModelRevision.id, RackModelRevision.width_mm, RackModelRevision.depth_mm).where(
                        RackModelRevision.id.in_(revision_ids)
                    )
                )
            ).all()
        }

    for sensor, asset, placement, rack_x, rack_y, rack_rot, revision_id in rows:
        x: float | None = None
        y: float | None = None
        source: str | None = None
        exact = False
        if placement.x_mm is not None and placement.y_mm is not None:
            x, y, source, exact = float(placement.x_mm), float(placement.y_mm), "placement", True
        elif placement.placement_type == "rack_mounted" and rack_x is not None and rack_y is not None:
            width, depth = rack_dims.get(revision_id, (None, None))
            x, y, _ = _rack_centre(rack_x, rack_y, rack_rot, width, depth)
            source, exact = "rack_footprint", False
        snapshot.points.append(
            SensorPoint(
                sensor_id=sensor.id, asset_tag=asset.asset_tag, name=sensor.name, sensor_kind=sensor.sensor_kind,
                measurement_role=sensor.measurement_role, lifecycle_status=asset.lifecycle_status, rack_id=placement.rack_id,
                placement_type=placement.placement_type, x_mm=x, y_mm=y, position_source=source, position_exact=exact,
                flow_direction_deg=sensor.flow_direction_deg, metric=metric,
            )
        )

    ids = [p.sensor_id for p in snapshot.points]
    if not ids:
        return snapshot

    latest_rows = (
        await db.execute(
            select(TelemetryReading, Integration.poll_interval_seconds, Integration.name)
            .join(Integration, Integration.id == TelemetryReading.integration_id)
            .where(
                TelemetryReading.managed_asset_id.in_(ids), TelemetryReading.metric == metric,
                TelemetryReading.received_at <= as_of,
            )
            .order_by(TelemetryReading.managed_asset_id, TelemetryReading.occurred_at.desc(), TelemetryReading.id)
            .distinct(TelemetryReading.managed_asset_id)
        )
    ).all()
    latest = {r.managed_asset_id: (r, interval, name) for r, interval, name in latest_rows}

    alarms: dict[uuid.UUID, int] = {}
    for aid, _alarm_id in (
        await db.execute(
            select(Alarm.managed_asset_id, Alarm.id)
            .join(AlarmRule, AlarmRule.id == Alarm.rule_id)
            .where(Alarm.managed_asset_id.in_(ids), Alarm.status.in_(("ACTIVE", "ACKNOWLEDGED")), AlarmRule.metric == metric)
        )
    ).all():
        alarms[aid] = alarms.get(aid, 0) + 1

    for point in snapshot.points:
        point.active_alarm_count = alarms.get(point.sensor_id, 0)
        hit = latest.get(point.sensor_id)
        if hit is None:
            point.state = MISSING
            continue
        reading, interval, integration_name = hit
        point.occurred_at = reading.occurred_at
        point.poll_interval_seconds = interval
        # Always loaded (threshold lookup needs it); serializers withhold source identity from callers without
        # integration:read.
        point.integration_id = reading.integration_id
        point.integration_name = integration_name
        state, age = classify_reading(reading.occurred_at, as_of, interval)
        point.age_seconds = age
        if reading.unit != definition.canonical_unit:
            point.state = INVALID
            point.invalid_reason = "unit_mismatch"
            continue
        if state == INVALID:
            point.state = INVALID
            point.invalid_reason = "future_timestamp"
            continue
        point.state = state
        point.value = float(reading.value)
        point.unit = reading.unit
        presentation = convert_to_presentation(metric, Decimal(str(reading.value)), reading.unit)
        point.presentation_value = float(presentation.value)
        point.presentation_unit = presentation.unit
    return snapshot
