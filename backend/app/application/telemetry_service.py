"""Application boundary for append-only telemetry ingestion.

Phase 10C additions (below `ingest_reading`): binding management and latest-status
caching for instantiated port/power-inlet markers (mapping_models.py's own docstring has
the full rationale for why this is a cache, not a series, and a separate table family
from the MVP `TelemetryReading` pipeline above)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.physical.ports import EquipmentPort, EquipmentPowerInlet
from app.domain.power.models import PowerCapacity
from app.domain.telemetry.mapping_models import (
    LINK_STATES,
    TELEMETRY_TARGET_TYPES,
    THRESHOLD_STATUS_LEVELS,
    PortTelemetryBinding,
    TelemetryLatestStatus,
)
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading, telemetry_series_key
from app.domain.telemetry.registry import convert_to_canonical


class MetricMappingNotFound(ValueError):
    pass


@dataclass(frozen=True)
class TelemetryIngestResult:
    reading_id: uuid.UUID | None
    duplicate: bool


async def ingest_reading(
    db: AsyncSession,
    *,
    collector_id: uuid.UUID,
    integration_id: uuid.UUID,
    dedup_key: str,
    external_identifier: str,
    source_identifier: str,
    occurred_at: datetime,
    value: float,
    attributes: dict | None = None,
) -> TelemetryIngestResult:
    mapping = (
        await db.execute(
            select(IntegrationMetricMapping).where(
                IntegrationMetricMapping.integration_id == integration_id,
                IntegrationMetricMapping.source_identifier == source_identifier,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise MetricMappingNotFound("No metric mapping exists for this integration source identifier.")
    raw_value = Decimal(str(value))
    source_scale = Decimal(str(mapping.scale))
    if mapping.registry_version is None:
        # Rows/mappings created before the registry retain their historic meaning.
        stored_value = raw_value * source_scale
        stored_unit = mapping.unit
        registry_version = None
    else:
        canonical = convert_to_canonical(
            mapping.canonical_metric, raw_value, mapping.unit,
            source_scale=source_scale, registry_version=mapping.registry_version,
        )
        stored_value = canonical.value
        stored_unit = canonical.unit
        registry_version = canonical.registry_version
    received_at = datetime.now(UTC)
    statement = (
        insert(TelemetryReading)
        .values(
            id=uuid.uuid4(),
            collector_id=collector_id,
            integration_id=integration_id,
            mapping_id=mapping.id,
            managed_asset_id=mapping.managed_asset_id,
            external_identifier=external_identifier,
            series_key=telemetry_series_key(
                integration_id, mapping.managed_asset_id, external_identifier, mapping.canonical_metric, stored_unit,
                registry_version,
            ),
            dedup_key=dedup_key,
            metric=mapping.canonical_metric,
            unit=stored_unit,
            value=stored_value,
            raw_value=raw_value,
            raw_unit=mapping.unit,
            source_scale=source_scale,
            registry_version=registry_version,
            occurred_at=occurred_at,
            received_at=received_at,
            attributes=attributes or {},
        )
        .on_conflict_do_nothing(constraint="uq_telemetry_reading_collector_dedup")
        .returning(TelemetryReading.id)
    )
    reading_id = (await db.execute(statement)).scalar_one_or_none()
    if reading_id is not None:
        # The explicit application boundary prevents the telemetry ORM domain from
        # depending on alarm persistence while retaining one transaction.
        from app.application.alarm_service import evaluate_reading

        reading = await db.get(TelemetryReading, reading_id)
        assert reading is not None
        await evaluate_reading(db, reading)
        # A delayed edge replay for a day already compacted is incorporated into the
        # authoritative daily statistics; occurred_at, never receipt time, chooses it.
        from app.application.telemetry_retention import merge_late_reading

        merged = await merge_late_reading(db, reading)
        if merged:
            await db.delete(reading)  # aggregate update is in the same transaction
    return TelemetryIngestResult(reading_id=reading_id, duplicate=reading_id is None)


# --------------------------------------------------------------------------------------
# Phase 10C: port/power-inlet telemetry bindings + cached latest status
# --------------------------------------------------------------------------------------


class BindingNotFound(ValueError):
    pass


class InvalidBindingTarget(ValueError):
    pass


async def create_port_telemetry_binding(
    db: AsyncSession,
    *,
    equipment_id: uuid.UUID,
    target_type: str,
    equipment_port_id: uuid.UUID | None,
    equipment_power_inlet_id: uuid.UUID | None,
    protocol: str,
    external_ref: str,
    label: str | None,
) -> PortTelemetryBinding:
    """Service-layer mirror of `target_reference_matches_type` (defense in depth, the
    same division of labor `equipment_instantiation_service.connect_port`'s pdu_outlet
    check already uses) plus the one thing the DB CHECK cannot verify: that the
    referenced port/inlet actually belongs to `equipment_id`, not merely to *some*
    equipment."""
    if target_type not in TELEMETRY_TARGET_TYPES:
        raise InvalidBindingTarget(f"Unsupported target_type: {target_type!r}")

    if target_type == "network_port":
        if equipment_port_id is None or equipment_power_inlet_id is not None:
            raise InvalidBindingTarget("network_port bindings require equipment_port_id and no equipment_power_inlet_id.")
        port = await db.get(EquipmentPort, equipment_port_id)
        if port is None:
            raise BindingNotFound(f"EquipmentPort {equipment_port_id} not found.")
        if port.equipment_id != equipment_id:
            raise InvalidBindingTarget(f"EquipmentPort {equipment_port_id} does not belong to equipment {equipment_id}.")
    elif target_type == "power_inlet":
        if equipment_power_inlet_id is None or equipment_port_id is not None:
            raise InvalidBindingTarget("power_inlet bindings require equipment_power_inlet_id and no equipment_port_id.")
        inlet = await db.get(EquipmentPowerInlet, equipment_power_inlet_id)
        if inlet is None:
            raise BindingNotFound(f"EquipmentPowerInlet {equipment_power_inlet_id} not found.")
        if inlet.equipment_id != equipment_id:
            raise InvalidBindingTarget(
                f"EquipmentPowerInlet {equipment_power_inlet_id} does not belong to equipment {equipment_id}."
            )
    else:  # environmental
        if equipment_port_id is not None or equipment_power_inlet_id is not None:
            raise InvalidBindingTarget("environmental bindings must not reference a port or inlet.")

    binding = PortTelemetryBinding(
        equipment_id=equipment_id,
        target_type=target_type,
        equipment_port_id=equipment_port_id,
        equipment_power_inlet_id=equipment_power_inlet_id,
        protocol=protocol,
        external_ref=external_ref,
        label=label,
    )
    db.add(binding)
    await db.flush()
    return binding


async def list_port_telemetry_bindings(db: AsyncSession, *, equipment_id: uuid.UUID) -> list[PortTelemetryBinding]:
    return list(
        (
            await db.execute(
                select(PortTelemetryBinding)
                .where(PortTelemetryBinding.equipment_id == equipment_id)
                .order_by(PortTelemetryBinding.created_at)
            )
        ).scalars()
    )


def evaluate_link_status(raw_link_state: str, error_rate_pct: float, *, degraded_error_rate_pct: float = 1.0) -> str:
    """`UP` unless the poller itself reports `DOWN`, or the observed error rate exceeds
    `degraded_error_rate_pct` (1% by default — ordinary transient loss on a healthy link
    runs well under this, so this only fires for a link actually struggling)."""
    if raw_link_state not in LINK_STATES:
        raise ValueError(f"Unsupported link state: {raw_link_state!r}")
    if raw_link_state == "DOWN":
        return "DOWN"
    if raw_link_state == "DEGRADED" or error_rate_pct > degraded_error_rate_pct:
        return "DEGRADED"
    return "UP"


def evaluate_power_status(
    active_power_watts: float, rated_capacity_kw: float | None, *, warning_pct: float = 80.0, critical_pct: float = 95.0
) -> str:
    """`NORMAL`/`WARNING`/`CRITICAL` by percentage of the inlet's rated capacity, in the
    same kW terms `power_capacity.py` already uses (never amps — that would need a
    voltage assumption this module has no authority to make). `rated_capacity_kw` is
    `None` whenever no `PowerCapacity` row is configured for this inlet's `PowerNode` —
    exactly the "capacity unknown" case `power_capacity.py`'s own `data_quality` field
    already names — and this returns `NORMAL` rather than fabricating a threshold breach
    that cannot actually be evaluated."""
    if rated_capacity_kw is None or rated_capacity_kw <= 0:
        return "NORMAL"
    utilization_pct = (active_power_watts / 1000.0 / rated_capacity_kw) * 100
    if utilization_pct >= critical_pct:
        return "CRITICAL"
    if utilization_pct >= warning_pct:
        return "WARNING"
    return "NORMAL"


def evaluate_environmental_status(
    temperature_celsius: float, *, warning_celsius: float = 27.0, critical_celsius: float = 32.0
) -> str:
    """Generic ASHRAE-style recommended/allowable envelope thresholds for a data-hall
    sensor (27C/32C) — deliberately not vendor- or site-specific, matching this
    codebase's stated preference for a generic model over per-vendor special-casing."""
    if temperature_celsius >= critical_celsius:
        return "CRITICAL"
    if temperature_celsius >= warning_celsius:
        return "WARNING"
    return "NORMAL"


async def _current_rated_capacity_kw(db: AsyncSession, power_node_id: uuid.UUID) -> float | None:
    capacity = (
        await db.execute(
            select(PowerCapacity).where(PowerCapacity.power_node_id == power_node_id, PowerCapacity.effective_to.is_(None))
        )
    ).scalar_one_or_none()
    if capacity is None:
        return None
    if capacity.configured_capacity_kw is not None:
        return float(capacity.configured_capacity_kw)
    if capacity.rated_capacity_kw is not None:
        return float(capacity.rated_capacity_kw)
    return None


async def record_latest_status(
    db: AsyncSession, *, binding_id: uuid.UUID, payload: dict, sampled_at: datetime
) -> TelemetryLatestStatus:
    """Evaluate and upsert the one cached `TelemetryLatestStatus` row for `binding_id`,
    keeping the newest sample by the poller's own `sampled_at`, never by arrival order.
    `payload` must carry the fields for the binding's own `target_type` (see
    mapping_models.py's `TelemetryLatestStatus` docstring for the expected keys per
    type) — a mismatch raises `KeyError`, surfaced by the route as a clean 422.

    Ordering contract (see `_LATEST_STATUS_ORDERING` below for why this is enforced in
    SQL rather than by a read-then-write in Python):

    * `sampled_at` strictly newer than the stored row — applied; this call's status and
      payload become the cached current status.
    * `sampled_at` strictly older than the stored row (a *stale* sample: a delayed
      retry, a re-queued batch, a slow poller losing a race to a faster one) — rejected.
      The stored row is left exactly as it was.
    * `sampled_at` equal to the stored row (a *duplicate* re-delivery of one sample) —
      treated as a no-op, first writer wins. There is no ordering evidence to prefer
      either writer, so making the outcome independent of arrival order is the whole
      point; last-writer-wins here would reintroduce the same nondeterminism under
      concurrency that the strict comparison exists to remove.

    In every case the return value is the row that is authoritative *after* this call —
    this call's own row when it was applied, the retained newer row when it was not.
    Callers that need to distinguish the two compare the returned `sampled_at` against
    the one they submitted; the signature and return type are unchanged.
    """
    binding = await db.get(PortTelemetryBinding, binding_id)
    if binding is None:
        raise BindingNotFound(f"PortTelemetryBinding {binding_id} not found.")

    if binding.target_type == "network_port":
        status_level = evaluate_link_status(payload["link_state"], float(payload.get("error_rate_pct", 0.0)))
    elif binding.target_type == "power_inlet":
        assert binding.equipment_power_inlet_id is not None
        inlet = await db.get(EquipmentPowerInlet, binding.equipment_power_inlet_id)
        assert inlet is not None
        rated_capacity_kw = await _current_rated_capacity_kw(db, inlet.power_node_id)
        status_level = evaluate_power_status(float(payload["active_power_watts"]), rated_capacity_kw)
    else:
        status_level = evaluate_environmental_status(float(payload["temperature_celsius"]))
    assert status_level in LINK_STATES or status_level in THRESHOLD_STATUS_LEVELS

    received_at = datetime.now(UTC)
    insert_statement = insert(TelemetryLatestStatus).values(
        id=uuid.uuid4(), binding_id=binding_id, status_level=status_level, payload=payload,
        sampled_at=sampled_at, received_at=received_at,
    )
    statement = insert_statement.on_conflict_do_update(
        index_elements=[TelemetryLatestStatus.binding_id],
        set_={"status_level": status_level, "payload": payload, "sampled_at": sampled_at, "received_at": received_at},
        # _LATEST_STATUS_ORDERING: the guard that makes a late-arriving stale sample
        # unable to overwrite a newer one. It belongs in the statement, not in a Python
        # `if row.sampled_at < sampled_at` around it: two sessions ingesting for the same
        # binding both read the same "before" row, both decide they are newer, and the
        # slower one then overwrites the faster one's newer reading — a lost update no
        # amount of retrying fixes. PostgreSQL evaluates this WHERE *after* taking the
        # conflicting row's lock and re-reading its committed version, so a concurrent
        # writer sees the other session's already-committed sampled_at and declines,
        # atomically, in the one statement. `excluded` is the row this call proposed.
        where=TelemetryLatestStatus.__table__.c.sampled_at < insert_statement.excluded.sampled_at,
    ).returning(TelemetryLatestStatus.id)
    status_id = (await db.execute(statement)).scalar_one_or_none()
    if status_id is None:
        # The guard declined: a stale or duplicate sample. `DO UPDATE ... WHERE` that
        # matches nothing returns no row, and the stored row keeps its own status,
        # payload, sampled_at and received_at untouched. Return that retained row.
        row = (
            await db.execute(select(TelemetryLatestStatus).where(TelemetryLatestStatus.binding_id == binding_id))
        ).scalar_one()
    else:
        stored = await db.get(TelemetryLatestStatus, status_id)
        assert stored is not None
        row = stored
    # `db_session` fixtures/routes run with expire_on_commit=False, so a *second* ingest
    # for the same binding_id would otherwise return the first call's stale, still-
    # identity-mapped Python object here (the raw Core upsert above never touches the
    # ORM's in-memory attributes on conflict) -- refresh forces it back in sync with the
    # row now actually stored, whichever call wrote it.
    await db.refresh(row)
    return row


@dataclass(frozen=True)
class LatestPortStatus:
    binding: PortTelemetryBinding
    status: TelemetryLatestStatus | None


async def get_latest_status_for_equipment(db: AsyncSession, *, equipment_id: uuid.UUID) -> list[LatestPortStatus]:
    rows = (
        await db.execute(
            select(PortTelemetryBinding, TelemetryLatestStatus)
            .outerjoin(TelemetryLatestStatus, TelemetryLatestStatus.binding_id == PortTelemetryBinding.id)
            .where(PortTelemetryBinding.equipment_id == equipment_id)
            .order_by(PortTelemetryBinding.created_at)
        )
    ).all()
    return [LatestPortStatus(binding=binding, status=status) for binding, status in rows]


async def get_latest_status_for_rack(db: AsyncSession, *, rack_id: uuid.UUID) -> list[LatestPortStatus]:
    """Every binding for every currently rack-mounted equipment in `rack_id` — the query
    the rack-elevation overlay's telemetry polling actually needs (one bounded call per
    rack, never one call per marker)."""
    from app.domain.placement.models import EquipmentPlacement

    rows = (
        await db.execute(
            select(PortTelemetryBinding, TelemetryLatestStatus)
            .join(EquipmentPlacement, EquipmentPlacement.equipment_id == PortTelemetryBinding.equipment_id)
            .outerjoin(TelemetryLatestStatus, TelemetryLatestStatus.binding_id == PortTelemetryBinding.id)
            .where(
                EquipmentPlacement.rack_id == rack_id,
                EquipmentPlacement.placement_type == "rack_mounted",
                EquipmentPlacement.effective_to.is_(None),
            )
            .order_by(PortTelemetryBinding.created_at)
        )
    ).all()
    return [LatestPortStatus(binding=binding, status=status) for binding, status in rows]
