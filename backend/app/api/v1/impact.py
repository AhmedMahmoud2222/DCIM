"""Read-only failure-impact ("what-if") simulation API (Phase 10C). See
app/application/impact_service.py's module docstring for the traversal design."""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.impact_service import (
    IMPACT_TARGET_TYPES,
    ImpactedEquipment,
    ImpactSimulationResult,
    ImpactTargetNotFound,
    simulate_failure,
)
from app.application.power_graph import GraphTraversalBounded
from app.application.rbac import require_permission
from app.core.errors import ApiError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/impact", tags=["impact"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class ImpactSimulateIn(BaseModel):
    target_type: str
    target_id: uuid.UUID


class ImpactedEquipmentOut(BaseModel):
    equipment_id: uuid.UUID
    asset_tag: str
    hostname: str | None
    service: str | None
    hop: int
    impact_type: str
    message: str


class ImpactSimulationOut(BaseModel):
    target_type: str
    target_id: uuid.UUID
    directly_impacted: list[ImpactedEquipmentOut]
    indirectly_impacted: list[ImpactedEquipmentOut]
    lost_redundancy_paths: list[str]
    affected_services: list[str]


def _equipment_out(item: ImpactedEquipment) -> ImpactedEquipmentOut:
    return ImpactedEquipmentOut(
        equipment_id=item.equipment_id, asset_tag=item.asset_tag, hostname=item.hostname, service=item.service,
        hop=item.hop, impact_type=item.impact_type, message=item.message,
    )


def _result_out(result: ImpactSimulationResult) -> ImpactSimulationOut:
    return ImpactSimulationOut(
        target_type=result.target_type,
        target_id=result.target_id,
        directly_impacted=[_equipment_out(i) for i in result.directly_impacted],
        indirectly_impacted=[_equipment_out(i) for i in result.indirectly_impacted],
        lost_redundancy_paths=result.lost_redundancy_paths,
        affected_services=result.affected_services,
    )


@router.post("/simulate", response_model=ImpactSimulationOut)
async def simulate_impact(
    body: ImpactSimulateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    # Read-only: simulating a failure never mutates PowerConnection/PortConnection/
    # equipment rows, so this reuses power:read rather than introducing a new
    # permission — the same "no unnecessary new surface" judgment call
    # equipment_instantiation_service.py's port-connect check already makes for
    # pdu_outlet validation (service-layer, not a whole new permission).
    ctx=Depends(require_permission("power:read")),
) -> ImpactSimulationOut:
    if body.target_type not in IMPACT_TARGET_TYPES:
        raise ApiError(
            status_code=422, title="Invalid target type", detail=f"target_type must be one of {IMPACT_TARGET_TYPES}."
        )
    try:
        result = await simulate_failure(db, target_type=body.target_type, target_id=body.target_id)
    except ImpactTargetNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except GraphTraversalBounded as exc:
        request_id, correlation_id = _request_ids(request)
        logger.warning(
            "impact_simulation_traversal_bound_exceeded",
            root_id=str(exc.root_id), limit_kind=exc.limit_kind, target_type=body.target_type,
            request_id=request_id, correlation_id=correlation_id,
        )
        raise ApiError(
            status_code=422, title="Graph Too Large",
            detail=f"Impact simulation from {exc.root_id} exceeded its {exc.limit_kind} bound.",
        ) from exc
    return _result_out(result)
