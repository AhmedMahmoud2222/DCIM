"""Integration CRUD (ARCHITECTURE_REVIEW.md §20). Credentials are never returned in any
response body (write-only from the API's perspective, exactly like a password) --
`credential_ciphertext` is Fernet-encrypted at write time (app/core/secrets.py) and
only ever decrypted in-process by the polling orchestrator
(app/application/collector_service.py's `run_polling_cycle`), never serialized back out
through this router."""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.collector_service import current_assignment, current_assignments_bulk
from app.application.concurrency import check_version_match, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.core.secrets import encrypt_secret
from app.domain.integration.models import CollectorAssignment, Integration

router = APIRouter(prefix="/integrations", tags=["integrations"])
POLL_INTERVAL_PRESETS = frozenset((60, 180, 300, 600, 900, 1800))
DEFAULT_POLL_INTERVAL_SECONDS = 300

# Pre-MVP consolidation hardening (Codex FV2): `config` is a one-time, admin-authored
# object (a REST integration's path/method/headers, an SNMP integration's version),
# not a per-poll payload -- more generous than `IngestRecordIn.raw_attributes`'s 8192
# bytes is appropriate, but "arbitrary dict, no bound at all" is not. 16 KiB and 6
# levels of nesting comfortably fit any real integration's configuration (a REST
# integration's own `headers` dict is one level deep) without being unbounded.
MAX_CONFIG_BYTES = 16_384
MAX_CONFIG_NESTING_DEPTH = 6


def _json_nesting_depth(value: object, current: int = 0) -> int:
    if isinstance(value, dict) and value:
        return max(_json_nesting_depth(v, current + 1) for v in value.values())
    if isinstance(value, list) and value:
        return max(_json_nesting_depth(v, current + 1) for v in value)
    return current


def _bound_config(v: dict) -> dict:
    if len(json.dumps(v)) > MAX_CONFIG_BYTES:
        raise ValueError(f"config must serialize to at most {MAX_CONFIG_BYTES} bytes")
    if _json_nesting_depth(v) > MAX_CONFIG_NESTING_DEPTH:
        raise ValueError(f"config must not nest more than {MAX_CONFIG_NESTING_DEPTH} levels deep")
    return v


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


class IntegrationIn(BaseModel):
    name: str = Field(max_length=128)
    integration_type: str
    site_id: uuid.UUID | None = None
    target_host: str = Field(max_length=255)
    target_port: int | None = None
    config: dict = Field(default_factory=dict)
    credential: str | None = Field(default=None, max_length=2000, description="Plaintext, write-only -- never returned.")
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    enabled: bool = True

    @field_validator("config")
    @classmethod
    def _validate_config_bounds(cls, v: dict) -> dict:
        return _bound_config(v)

    @field_validator("poll_interval_seconds")
    @classmethod
    def _validate_poll_interval(cls, v: int) -> int:
        if v not in POLL_INTERVAL_PRESETS:
            raise ValueError("poll_interval_seconds must be one of 60, 180, 300, 600, 900, 1800")
        return v


class IntegrationOut(BaseModel):
    id: uuid.UUID
    name: str
    integration_type: str
    site_id: uuid.UUID | None
    target_host: str
    target_port: int | None
    config: dict
    enabled: bool
    poll_interval_seconds: int
    last_poll_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    has_credential: bool
    assigned_collector_id: uuid.UUID | None
    version: int

    model_config = {"from_attributes": True}


def _build_out(integration: Integration, assignment: CollectorAssignment | None) -> IntegrationOut:
    return IntegrationOut(
        id=integration.id,
        name=integration.name,
        integration_type=integration.integration_type,
        site_id=integration.site_id,
        target_host=integration.target_host,
        target_port=integration.target_port,
        config=integration.config,
        enabled=integration.enabled,
        poll_interval_seconds=integration.poll_interval_seconds,
        last_poll_at=integration.last_poll_at,
        last_success_at=integration.last_success_at,
        last_failure_at=integration.last_failure_at,
        consecutive_failures=integration.consecutive_failures,
        has_credential=integration.credential_ciphertext is not None,
        assigned_collector_id=assignment.collector_id if assignment else None,
        version=integration.version,
    )


async def _to_out(db: AsyncSession, integration: Integration) -> IntegrationOut:
    assignment = await current_assignment(db, integration.id)
    return _build_out(integration, assignment)


@router.post("", response_model=IntegrationOut, status_code=201)
async def create_integration(
    body: IntegrationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:manage")),
) -> IntegrationOut:
    request_id, correlation_id = _request_ids(request)
    integration = Integration(
        id=uuid.uuid4(),
        name=body.name,
        integration_type=body.integration_type,
        site_id=body.site_id,
        enabled=body.enabled,
        target_host=body.target_host,
        target_port=body.target_port,
        config=body.config,
        credential_ciphertext=encrypt_secret(body.credential) if body.credential else None,
        poll_interval_seconds=body.poll_interval_seconds,
        version=1,
    )
    db.add(integration)
    await db.flush()
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="integration.create",
        entity_type="integration",
        entity_id=integration.id,
        request_id=request_id,
        correlation_id=correlation_id,
        after={"name": body.name, "integration_type": body.integration_type},
    )
    await write_outbox_event(
        db,
        event_type="IntegrationEnabled" if body.enabled else "IntegrationDisabled",
        aggregate_type="integration",
        aggregate_id=integration.id,
        payload={"name": body.name},
        correlation_id=correlation_id,
        causation_id=request_id,
    )
    await db.commit()
    return await _to_out(db, integration)


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:read")),
) -> list[IntegrationOut]:
    rows = (await db.execute(select(Integration))).scalars().all()
    assignment_by_integration = await current_assignments_bulk(db, [r.id for r in rows])
    return [_build_out(r, assignment_by_integration.get(r.id)) for r in rows]


@router.get("/{integration_id}", response_model=IntegrationOut)
async def get_integration(
    integration_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("integration:read")),
) -> IntegrationOut:
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError(f"Integration {integration_id} not found.")
    return await _to_out(db, integration)


class IntegrationPatchIn(BaseModel):
    enabled: bool | None = None
    poll_interval_seconds: int | None = None
    target_host: str | None = Field(default=None, max_length=255)
    target_port: int | None = None
    config: dict | None = None
    credential: str | None = Field(default=None, max_length=2000, description="If provided, replaces the stored credential.")

    @field_validator("config")
    @classmethod
    def _validate_config_bounds(cls, v: dict | None) -> dict | None:
        return v if v is None else _bound_config(v)

    @field_validator("poll_interval_seconds")
    @classmethod
    def _validate_poll_interval(cls, v: int | None) -> int | None:
        if v is not None and v not in POLL_INTERVAL_PRESETS:
            raise ValueError("poll_interval_seconds must be one of 60, 180, 300, 600, 900, 1800")
        return v


@router.patch("/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationPatchIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("integration:manage")),
) -> IntegrationOut:
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError(f"Integration {integration_id} not found.")
    check_version_match(expected=if_match_version, actual=integration.version)

    request_id, correlation_id = _request_ids(request)
    before = {"enabled": integration.enabled, "poll_interval_seconds": integration.poll_interval_seconds}
    if body.enabled is not None:
        integration.enabled = body.enabled
    if body.poll_interval_seconds is not None:
        integration.poll_interval_seconds = body.poll_interval_seconds
    if body.target_host is not None:
        integration.target_host = body.target_host
    if body.target_port is not None:
        integration.target_port = body.target_port
    if body.config is not None:
        integration.config = body.config
    if body.credential is not None:
        integration.credential_ciphertext = encrypt_secret(body.credential)
    integration.version += 1

    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="integration.update",
        entity_type="integration",
        entity_id=integration.id,
        request_id=request_id,
        correlation_id=correlation_id,
        before=before,
        after={"enabled": integration.enabled, "poll_interval_seconds": integration.poll_interval_seconds},
    )
    if body.enabled is not None:
        await write_outbox_event(
            db,
            event_type="IntegrationEnabled" if body.enabled else "IntegrationDisabled",
            aggregate_type="integration",
            aggregate_id=integration.id,
            payload={"name": integration.name},
            correlation_id=correlation_id,
            causation_id=request_id,
        )
    await db.commit()
    return await _to_out(db, integration)
