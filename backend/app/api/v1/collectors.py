"""Collector identity, capability, assignment, health, and the WAN-buffered ingestion
contract (ARCHITECTURE_REVIEW.md §18/§19). Two distinct trust boundaries in this one
router, never confused: registration/administration endpoints are user-authenticated
(`require_permission`, the normal RBAC path); heartbeat/ingest endpoints are
collector-authenticated (`get_current_collector`, HMAC-signed, master prompt §9 --
NEVER the user JWT)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Literal, TypeVar

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.telemetry import TelemetryBatchIn, TelemetryBatchOut, ingest_collector_telemetry
from app.application import idempotency as idem
from app.application.collector_auth import CollectorAuthError, verify_collector_request
from app.application.collector_service import (
    assign_integration,
    classify_collector_health,
    classify_collector_health_bulk,
    current_assignment,
    declare_capabilities,
    record_heartbeat,
    register_collector,
    run_polling_cycle,
)
from app.application.discovery_service import ingest_discovery
from app.application.idempotency import IdempotencyConflict, IdempotencyStillProcessing
from app.application.network.neighbor_evidence import InvalidNeighborPayload
from app.application.network.neighbor_service import apply_scan_marker, ingest_neighbor
from app.application.network.profile_service import build_plan
from app.application.rbac import require_permission
from app.core.errors import ApiError, NotFoundError, UnauthorizedCollectorError
from app.core.logging import get_logger
from app.domain.integration.models import Collector, CollectorCapability

logger = get_logger(__name__)
router = APIRouter(prefix="/collectors", tags=["collectors"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# Pre-MVP consolidation hardening (Codex M1): every collector-authenticated endpoint
# shares this one dependency, so ONE bound here protects all of them, sized for the
# largest LEGITIMATE request any of them makes -- a full 500-record ingest batch
# (MAX_BATCH_RECORDS below), each record allowed up to MAX_RAW_ATTRIBUTES_BYTES (8192)
# of raw_attributes plus a few hundred bytes of surrounding JSON fields/syntax:
# 500 * (8192 + ~300) ~= 4.25 MiB. 8 MiB leaves real headroom without being
# effectively unbounded. Heartbeat's own body is a few dozen bytes, so this same cap
# applied there is generous, not tight.
MAX_COLLECTOR_REQUEST_BYTES = 8 * 1024 * 1024


async def _read_body_bounded(request: Request, *, max_bytes: int) -> bytes:
    """Never trust `Content-Length` alone -- it can be absent (chunked transfer),
    wrong, or a deliberate lie. This reads the ACTUAL bytes the client sends, off
    `request.stream()` (the same underlying source `request.body()` itself reads
    from), and aborts the instant the running total exceeds `max_bytes`, regardless of
    what any header claimed. HMAC verification still needs the complete raw body when
    the request is within the limit, so this returns exactly what `request.body()`
    would have -- it does not change what gets signed, only how much this process is
    willing to buffer before giving up."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise ApiError(
                    status_code=413, title="Request Too Large",
                    detail=f"Request body exceeds the maximum of {max_bytes} bytes.",
                )
        except ValueError:
            pass  # a malformed Content-Length is not itself trusted either way -- the byte-counted read below is authoritative

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise ApiError(
                status_code=413, title="Request Too Large",
                detail=f"Request body exceeds the maximum of {max_bytes} bytes.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def get_current_collector(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_collector_id: str = Header(..., alias="X-Collector-Id"),
    x_collector_timestamp: str = Header(..., alias="X-Collector-Timestamp"),
    x_collector_nonce: str = Header(..., alias="X-Collector-Nonce"),
    x_collector_signature: str = Header(..., alias="X-Collector-Signature"),
) -> Collector:
    """The collector machine-trust dependency (master prompt §9) -- deliberately a
    SEPARATE dependency from `app.api.deps.get_current_user`/`app.application.rbac.
    require_permission`, never composed with them. A collector never acquires RBAC
    permissions; it authenticates to a narrow, purpose-built set of endpoints only.

    Deliberately reads the body itself via the bounded stream reader above, rather
    than declaring a FastAPI-native Pydantic `body: SomeModel` parameter on the
    route -- empirically confirmed (a minimal repro against this exact FastAPI/
    Starlette version) that when a route ALSO has such a parameter, FastAPI resolves
    it independently and Starlette caches the full body the first time anything reads
    it, so declaring both would buffer the complete body before this bound ever runs,
    silently defeating it. The bytes are stashed on `request.state.raw_body` so the
    route itself can validate them into its own Pydantic model AFTER this bound has
    already been enforced."""
    raw_body = await _read_body_bounded(request, max_bytes=MAX_COLLECTOR_REQUEST_BYTES)
    request.state.raw_body = raw_body
    try:
        return await verify_collector_request(
            db, collector_id_header=x_collector_id, timestamp_header=x_collector_timestamp,
            nonce_header=x_collector_nonce, signature_header=x_collector_signature, raw_body=raw_body,
        )
    except CollectorAuthError as exc:
        raise UnauthorizedCollectorError(str(exc)) from exc


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _parse_body(request: Request, model: type[_ModelT]) -> _ModelT:
    """Validates `request.state.raw_body` (set by `get_current_collector` above)
    against a Pydantic model, producing the SAME 422 problem-detail shape FastAPI's
    own automatic body-parsing would have -- the route never declares that parameter
    itself, precisely to avoid the double-buffering this function's own docstring
    on `get_current_collector` explains."""
    from pydantic import ValidationError

    try:
        return model.model_validate_json(request.state.raw_body)
    except ValidationError as exc:
        raise ApiError(
            status_code=422, title="Validation Error", detail="One or more fields failed validation.",
            type_="https://dcim.internal/errors/validation",
        ) from exc


# ------------------------------------------------------------------------- registration


class CollectorRegisterIn(BaseModel):
    name: str = Field(max_length=128)
    collector_type: str
    site_id: uuid.UUID | None = None
    version_string: str | None = Field(default=None, max_length=64)


class CollectorRegisterOut(BaseModel):
    id: uuid.UUID
    name: str
    collector_type: str
    site_id: uuid.UUID | None
    status: str
    secret: str  # plaintext -- returned exactly once, never retrievable again


@router.post("", response_model=CollectorRegisterOut, status_code=201)
async def create_collector(
    body: CollectorRegisterIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("collector:manage")),
) -> CollectorRegisterOut:
    request_id, correlation_id = _request_ids(request)
    collector, secret = await register_collector(
        db, name=body.name, collector_type=body.collector_type, site_id=body.site_id,
        version_string=body.version_string, actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    return CollectorRegisterOut(
        id=collector.id, name=collector.name, collector_type=collector.collector_type,
        site_id=collector.site_id, status=collector.status, secret=secret,
    )


class CollectorOut(BaseModel):
    id: uuid.UUID
    name: str
    collector_type: str
    site_id: uuid.UUID | None
    status: str
    version_string: str | None
    health: str
    last_heartbeat_at: datetime | None
    seconds_since_heartbeat: float | None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[CollectorOut])
async def list_collectors(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("collector:read")),
) -> list[CollectorOut]:
    collectors = (await db.execute(select(Collector))).scalars().all()
    health_by_id = await classify_collector_health_bulk(db, [c.id for c in collectors])
    out = []
    for c in collectors:
        health = health_by_id[c.id]
        out.append(
            CollectorOut(
                id=c.id, name=c.name, collector_type=c.collector_type, site_id=c.site_id, status=c.status,
                version_string=c.version_string, health=health.state, last_heartbeat_at=health.last_heartbeat_at,
                seconds_since_heartbeat=health.seconds_since_heartbeat,
            )
        )
    return out


@router.get("/{collector_id}", response_model=CollectorOut)
async def get_collector(
    collector_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("collector:read")),
) -> CollectorOut:
    c = await db.get(Collector, collector_id)
    if c is None:
        raise NotFoundError(f"Collector {collector_id} not found.")
    health = await classify_collector_health(db, c.id)
    return CollectorOut(
        id=c.id, name=c.name, collector_type=c.collector_type, site_id=c.site_id, status=c.status,
        version_string=c.version_string, health=health.state, last_heartbeat_at=health.last_heartbeat_at,
        seconds_since_heartbeat=health.seconds_since_heartbeat,
    )


class CapabilityDeclareIn(BaseModel):
    # Pre-MVP consolidation hardening (Codex FV2): bounded so a request cannot force
    # unbounded per-unique-code work in `declare_capabilities` (one INSERT attempt per
    # entry). 64 codes and 64 characters each are both far more generous than any real
    # deployment needs -- the built-in codes today are "icmp"/"rest"/"snmp".
    protocol_codes: list[str] = Field(max_length=64)

    @field_validator("protocol_codes")
    @classmethod
    def _bound_each_code_length(cls, v: list[str]) -> list[str]:
        for code in v:
            if len(code) > 64:
                raise ValueError("Each protocol_code must be at most 64 characters.")
        return v


@router.post("/{collector_id}/capabilities", status_code=204)
async def declare_collector_capabilities(
    collector_id: uuid.UUID, body: CapabilityDeclareIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("collector:manage")),
) -> None:
    if await db.get(Collector, collector_id) is None:
        raise NotFoundError(f"Collector {collector_id} not found.")
    request_id, correlation_id = _request_ids(request)
    await declare_capabilities(
        db, collector_id=collector_id, protocol_codes=body.protocol_codes,
        actor_user_id=ctx.user.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()


class CollectorCapabilityOut(BaseModel):
    protocol_code: str


@router.get("/{collector_id}/capabilities", response_model=list[CollectorCapabilityOut])
async def get_collector_capabilities(
    collector_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("collector:read"))
) -> list[CollectorCapabilityOut]:
    rows = (await db.execute(select(CollectorCapability).where(CollectorCapability.collector_id == collector_id))).scalars().all()
    return [CollectorCapabilityOut(protocol_code=r.protocol_code) for r in rows]


class AssignmentIn(BaseModel):
    integration_id: uuid.UUID


class AssignmentOut(BaseModel):
    id: uuid.UUID
    collector_id: uuid.UUID
    integration_id: uuid.UUID
    effective_from: datetime
    effective_to: datetime | None


@router.post("/{collector_id}/assignments", response_model=AssignmentOut, status_code=201)
async def create_assignment(
    collector_id: uuid.UUID, body: AssignmentIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("collector:assign")),
) -> AssignmentOut:
    request_id, correlation_id = _request_ids(request)
    assignment = await assign_integration(
        db, integration_id=body.integration_id, collector_id=collector_id, actor_user_id=ctx.user.id,
        request_id=request_id, correlation_id=correlation_id,
    )
    return AssignmentOut(
        id=assignment.id, collector_id=assignment.collector_id, integration_id=assignment.integration_id,
        effective_from=assignment.effective_from, effective_to=assignment.effective_to,
    )


@router.post("/{collector_id}/poll-now", status_code=200)
async def trigger_poll_cycle(
    collector_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("collector:manage")),
) -> list[dict]:
    """Manual trigger for this phase's foundation -- NOT a scheduled production polling
    loop (wiring `run_polling_cycle` into Celery beat on a per-integration
    `poll_interval_seconds` cadence is explicitly deferred; see
    PHASE8_EDGE_COLLECTOR_CONTRACT.md's open decisions). Exists so the polling
    orchestrator, driver abstraction, and discovery ingestion can be exercised through
    the real API, not only via direct service-layer calls in tests."""
    if await db.get(Collector, collector_id) is None:
        raise NotFoundError(f"Collector {collector_id} not found.")
    outcomes = await run_polling_cycle(db, collector_id=collector_id)
    return [
        {
            "integration_id": str(o.integration_id), "succeeded": o.succeeded, "error": o.error,
            "external_identifier": o.external_identifier,
        }
        for o in outcomes
    ]


# --------------------------------------------------------------- collector-authenticated


class HeartbeatIn(BaseModel):
    queue_depth: int | None = Field(default=None, ge=0)
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    mem_pct: float | None = Field(default=None, ge=0, le=100)
    # max_length matches CollectorHeartbeat.status's actual column width
    # (String(16)) -- an oversized value is rejected as a clean 422 at the API
    # boundary instead of reaching Postgres and surfacing as an unhandled 500
    # (the same class of bug as Finding M1, PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md).
    status: str = Field(default="ok", max_length=16)


@router.post("/{collector_id}/heartbeat", status_code=204)
async def heartbeat(
    collector_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    collector: Collector = Depends(get_current_collector),
) -> None:
    if collector.id != collector_id:
        raise UnauthorizedCollectorError("Signed collector identity does not match the URL path.")
    body = _parse_body(request, HeartbeatIn)
    await record_heartbeat(
        db, collector=collector, queue_depth=body.queue_depth, cpu_pct=body.cpu_pct, mem_pct=body.mem_pct, status=body.status,
    )


MAX_BATCH_RECORDS = 500
# Bounds total ingest payload size alongside MAX_BATCH_RECORDS: the record count cap
# alone does not stop a single record from carrying an arbitrarily large
# `raw_attributes` blob (PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding S1 -- "oversized
# payload" was previously only checked by record count, not per-record size).
MAX_RAW_ATTRIBUTES_BYTES = 8192


def _attributes_within_bound(attributes: dict) -> bool:
    return len(json.dumps(attributes)) <= MAX_RAW_ATTRIBUTES_BYTES


class IngestRecordIn(BaseModel):
    dedup_key: str = Field(max_length=255)
    integration_id: uuid.UUID
    external_identifier: str = Field(max_length=255)
    occurred_at: datetime
    raw_attributes: dict = Field(default_factory=dict)
    # Issue #101: collectors that predate neighbor discovery omit this and stay "device".
    record_type: Literal["device", "neighbor", "neighbor_scan"] = "device"

    @model_validator(mode="after")
    def _bound_device_attributes_size(self) -> "IngestRecordIn":
        # Device records keep the batch-level 422. Neighbor records are checked per record in
        # `ingest_batch` instead: one hostile LLDP/CDP peer must not make Central refuse (and the
        # edge re-send) a whole batch that also carries other devices' data.
        if self.record_type == "device" and not _attributes_within_bound(self.raw_attributes):
            raise ValueError(f"raw_attributes must serialize to at most {MAX_RAW_ATTRIBUTES_BYTES} bytes")
        return self


class IngestBatchIn(BaseModel):
    batch_id: str = Field(max_length=255)
    records: list[IngestRecordIn]


class IngestRecordResult(BaseModel):
    dedup_key: str
    status: str  # "accepted" | "duplicate" | "rejected"
    # Pre-MVP consolidation hardening (Codex M3): a stable, safe-to-parse code a
    # collector's own retry/backoff logic can branch on, independent of `error`'s
    # exact wording -- NOT_ASSIGNED / IDEMPOTENCY_CONFLICT / PROCESSING /
    # INTERNAL_PROCESSING_ERROR. `error` is always a hand-written, safe message; for
    # `INTERNAL_PROCESSING_ERROR` it is deliberately generic. Unexpected exception
    # details can contain SQL parameters or payload data and are neither returned to
    # the collector nor logged.
    error_code: str | None = None
    error: str | None = None


class IngestBatchOut(BaseModel):
    batch_id: str
    results: list[IngestRecordResult]


async def _release_claim_safely(db: AsyncSession, claim_ref: idem.ClaimRef, *, record_index: int) -> None:
    """`idem.release_claim()` runs its own DELETE + COMMIT (idempotency.py's
    `release_claim`) -- either can itself fail (lock contention, connection loss, a
    stale/duplicate delete racing this one). Left uncaught, that new exception would
    propagate out of `ingest_batch()`'s own except blocks entirely, past the
    sanitized per-record logging they otherwise reach, and into the GLOBAL handlers in
    app/core/errors.py -- which log `str(exc)`/`str(exc.orig)` plus, for the
    catch-all, a full traceback. Python's implicit exception chaining means that
    traceback also prints the ORIGINAL exception this record was already handling (via
    its own "During handling of the above exception..." section) -- silently undoing
    SEC-07's sanitization for exactly the exception text it exists to keep out of logs
    (Codex's Phase 11 independent review, Issue #38).

    Rolling back here is safe specifically because the per-record loop below reads the
    collector's identity from the `collector_id` path parameter, never from
    `collector.id` -- so expiring `collector` cannot crash a later iteration with
    MissingGreenlet (see the Finding I4 comment above `begin_nested()`).

    Codex's second-round review (Issue #38 / PR #49): the `db.rollback()` recovery
    attempt itself can fail (e.g. the connection is already gone), and an unguarded
    call would let THAT new exception escape this function just as unsanitized as the
    one it was meant to recover from -- reaching the same global catch-all handler,
    whose `exc_info=True` traceback would then chain all three exceptions (the
    original per-record failure, the release failure, and the rollback failure) into
    one log line. If rollback itself fails, the session is unusable for any further
    record in this batch, so this raises `ApiError` to abort the whole request rather
    than let siblings run against a broken connection -- deliberately `ApiError`, not
    a bare exception, because its handler (app/core/errors.py's `_api_error_handler`)
    never logs exception text at all, only the fixed `detail` below. Any record from
    an earlier iteration that already reached its own `await db.commit()` stays
    committed regardless; the collector's existing whole-batch-retry contract (this
    endpoint's own docstring) already covers replaying the rest safely, since every
    record is idempotent on its own `dedup_key`."""
    try:
        await idem.release_claim(db, claim_ref)
    except Exception:  # noqa: BLE001 -- must never leak upstream unsanitized; see docstring.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 -- the session is unusable; abort rather than
            # continue processing siblings on a connection that failed to even roll back.
            logger.error("ingest_batch_claim_release_rollback_failed", record_index=record_index)
            raise ApiError(
                status_code=503, title="Service Unavailable",
                detail="The database connection became unusable while processing this batch; retry the entire batch.",
            ) from None
        logger.error("ingest_batch_claim_release_failed", record_index=record_index)


def _parse_scan_marker(attributes: dict) -> dict:
    protocol, started, complete = attributes.get("protocol"), attributes.get("scan_started_at"), attributes.get("complete")
    if protocol not in ("lldp", "cdp") or not isinstance(complete, bool) or not isinstance(started, str):
        raise InvalidNeighborPayload("scan marker is invalid")
    try:
        parsed = datetime.fromisoformat(started)
    except ValueError as exc:
        raise InvalidNeighborPayload("scan marker is invalid") from exc
    return {"protocol": protocol, "scan_started_at": parsed, "complete": complete}


@router.post("/{collector_id}/ingest", response_model=IngestBatchOut)
async def ingest_batch(
    collector_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    collector: Collector = Depends(get_current_collector),
) -> IngestBatchOut:
    """§10 of the master prompt (WAN outage / store-and-forward): the CENTRAL side of
    the store-and-forward contract -- accepts a batch the collector buffered locally
    while disconnected, ACKs per-record (so a collector can drop only the
    successfully-ACKed records from its own local queue and retry the rest), and is
    safe under retry/duplicate-batch-replay/partial-prior-failure because every record
    is idempotent on its own `dedup_key`, reusing the EXISTING `IdempotencyKey`
    mechanism (`app.application.idempotency`) exactly as every other endpoint in this
    codebase does -- never a second idempotency framework.

    `occurred_at` (the device's own observation time, as the collector recorded it) is
    preserved verbatim in `raw_attributes` and never overwritten by the time this
    endpoint runs (`received_at`, implicitly "now") -- the master prompt's explicit
    "do not replace the actual device occurrence time with the time the central server
    receives the backlog.\""""
    if collector.id != collector_id:
        raise UnauthorizedCollectorError("Signed collector identity does not match the URL path.")
    body = _parse_body(request, IngestBatchIn)
    if len(body.records) > MAX_BATCH_RECORDS:
        raise ApiError(
            status_code=413, title="Batch Too Large",
            detail=f"A batch may contain at most {MAX_BATCH_RECORDS} records; got {len(body.records)}.",
        )

    results: list[IngestRecordResult] = []
    for record in body.records:
        if record.record_type != "device" and not _attributes_within_bound(record.raw_attributes):
            # Permanent and deterministic: no claim is taken and nothing is stored.
            results.append(
                IngestRecordResult(
                    dedup_key=record.dedup_key, status="rejected", error_code="INVALID_PAYLOAD",
                    error="The record is too large and will not be retried.",
                )
            )
            continue
        request_hash = idem.hash_request_body(record.model_dump(mode="json"))
        # Findings I2/I4 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): `get_or_claim` itself
        # can raise (a reused dedup_key with a different payload, or a claim still
        # genuinely in flight) -- that must be isolated to THIS record exactly like
        # every other per-record failure, never allowed to escape and fail sibling
        # records still to be processed.
        try:
            outcome = await idem.get_or_claim(
                db, key=f"{collector_id}:{record.dedup_key}", endpoint="collector_ingest", request_hash=request_hash,
            )
        except IdempotencyConflict:
            results.append(
                IngestRecordResult(
                    dedup_key=record.dedup_key, status="rejected", error_code="IDEMPOTENCY_CONFLICT",
                    error="This dedup_key was already used with a different request body.",
                )
            )
            continue
        except IdempotencyStillProcessing:
            results.append(
                IngestRecordResult(
                    dedup_key=record.dedup_key, status="rejected", error_code="PROCESSING",
                    error="This record is still being processed by a concurrent request; retry shortly.",
                )
            )
            continue

        if outcome.cached is not None:
            results.append(IngestRecordResult(dedup_key=record.dedup_key, status="duplicate"))
            continue

        assert outcome.claim is not None
        claim_ref = idem.ClaimRef.of(outcome.claim)
        # Finding I4: a failed record must not corrupt the SESSION-wide ORM state that
        # later records (and the request-scoped `collector` object obtained once via
        # Depends(get_current_collector) before this loop began) still depend on. A
        # full `await db.rollback()` here expires EVERY object the session is
        # currently tracking, not just this record's own attempted writes -- the next
        # record's `collector.id` read would then need an implicit lazy reload outside
        # an awaited context, crashing with MissingGreenlet. A SAVEPOINT (nested
        # transaction) is the correct, narrower primitive: rolling one back discards
        # only the DML issued since it was opened (this record's own attempted
        # DiscoveredDevice/ReconciliationDiff/Outbox writes), leaving every other
        # object in the session's identity map -- including `collector` -- untouched
        # and still perfectly usable on the next iteration.
        try:
            async with db.begin_nested():
                # §27 (trust boundary): a valid collector signature only proves WHO is
                # sending this batch, never that this collector is authorized to report
                # for `record.integration_id` -- a registered collector is semi-trusted,
                # not an implicitly-authoritative source for any integration it names
                # (PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding S2). Per-record, not
                # per-batch, so one record naming an unassigned integration doesn't cost
                # the whole batch.
                assignment = await current_assignment(db, record.integration_id)
                if assignment is None or assignment.collector_id != collector_id:
                    raise ApiError(
                        status_code=403, title="Not Assigned",
                        detail=(
                            f"Collector {collector_id} is not the currently assigned collector "
                            f"for integration {record.integration_id}."
                        ),
                    )
                if record.record_type == "neighbor":
                    await ingest_neighbor(
                        db, collector_id=collector_id, integration_id=record.integration_id,
                        occurred_at=record.occurred_at, raw_attributes=record.raw_attributes,
                    )
                elif record.record_type == "neighbor_scan":
                    marker = _parse_scan_marker(record.raw_attributes)
                    await apply_scan_marker(
                        db, integration_id=record.integration_id, protocol=marker["protocol"],
                        scan_started_at=marker["scan_started_at"], complete=marker["complete"],
                    )
                else:
                    enriched_attrs = dict(record.raw_attributes)
                    enriched_attrs["occurred_at"] = record.occurred_at.isoformat()
                    enriched_attrs["received_at"] = datetime.now(UTC).isoformat()
                    await ingest_discovery(
                        db, integration_id=record.integration_id, external_identifier=record.external_identifier,
                        raw_attributes=enriched_attrs, correlation_id=None, causation_id=body.batch_id,
                    )
                await idem.complete_claim(db, outcome.claim, response_status=200, response_body={"status": "accepted"})
            # The savepoint above released cleanly (no exception) -- persist it for
            # real and make it visible to other sessions/requests.
            await db.commit()
            results.append(IngestRecordResult(dedup_key=record.dedup_key, status="accepted"))
        except InvalidNeighborPayload:
            # Permanent: retrying the same bytes cannot succeed, so the collector may drop it.
            await _release_claim_safely(db, claim_ref, record_index=len(results))
            results.append(
                IngestRecordResult(
                    dedup_key=record.dedup_key, status="rejected", error_code="INVALID_PAYLOAD",
                    error="The record could not be interpreted and will not be retried.",
                )
            )
        except ApiError as exc:
            # The nested transaction has already been rolled back to its savepoint by
            # the `async with` block above (automatic on exception), so only this
            # record's own attempted writes were discarded. The claim is released so a
            # retry of just this record can succeed later. `ApiError.detail` is always
            # a hand-written, safe-for-collectors message (the only one raised in this
            # block today is the "Not Assigned" 403 above) -- safe to return as-is.
            await _release_claim_safely(db, claim_ref, record_index=len(results))
            results.append(
                IngestRecordResult(dedup_key=record.dedup_key, status="rejected", error_code="NOT_ASSIGNED", error=exc.detail)
            )
        except Exception:  # noqa: BLE001 -- one record's failure (e.g. a
            # database constraint violation from ingest_discovery) must not fail the
            # rest of the batch -- same savepoint/claim-release reasoning as above.
            # Exception messages, tracebacks and request-derived identifiers may contain
            # credentials, SQL parameters or raw telemetry. Log only fixed fields and
            # the record position; generic ACKs retain the existing retry contract.
            await _release_claim_safely(db, claim_ref, record_index=len(results))
            logger.error(
                "ingest_batch_record_processing_failed",
                error_code="INTERNAL_PROCESSING_ERROR", record_index=len(results),
            )
            results.append(
                IngestRecordResult(
                    dedup_key=record.dedup_key, status="rejected", error_code="INTERNAL_PROCESSING_ERROR",
                    error="An internal error occurred while processing this record.",
                )
            )

    return IngestBatchOut(batch_id=body.batch_id, results=results)


@router.post("/{collector_id}/telemetry", response_model=TelemetryBatchOut)
async def ingest_telemetry_batch(
    collector_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    collector: Collector = Depends(get_current_collector),
) -> TelemetryBatchOut:
    """Separate from discovery: telemetry never promotes discovered state."""
    if collector.id != collector_id:
        raise UnauthorizedCollectorError("Signed collector identity does not match the URL path.")
    return await ingest_collector_telemetry(db, collector=collector, body=_parse_body(request, TelemetryBatchIn))


class PlanIntegrationOut(BaseModel):
    integration_id: uuid.UUID
    target_host: str
    target_port: int | None
    snmp_version: str | None
    snmpv3: dict | None
    poll_interval_seconds: int
    plan: dict | None


MAX_PLAN_INTEGRATIONS = 500


@router.get("/{collector_id}/discovery-plan", response_model=list[PlanIntegrationOut])
async def discovery_plan(
    collector_id: uuid.UUID, db: AsyncSession = Depends(get_db), collector: Collector = Depends(get_current_collector),
) -> list[PlanIntegrationOut]:
    """What this collector must execute: its currently assigned, enabled SNMP integrations
    with the resolved, **secret-free** profile plan (OIDs, neighbor tables, metric
    mappings) and the non-secret SNMPv3 descriptor. Credentials never travel here; the edge
    holds them locally. Only integrations assigned to the signing collector are returned."""
    if collector.id != collector_id:
        raise UnauthorizedCollectorError("Signed collector identity does not match the URL path.")
    from app.domain.integration.models import CollectorAssignment, Integration

    rows = (
        await db.execute(
            select(Integration)
            .join(CollectorAssignment, CollectorAssignment.integration_id == Integration.id)
            .where(
                CollectorAssignment.collector_id == collector_id, CollectorAssignment.effective_to.is_(None),
                Integration.enabled.is_(True), Integration.integration_type == "snmp",
            )
            .order_by(Integration.name)
            .limit(MAX_PLAN_INTEGRATIONS)
        )
    ).scalars().all()
    out = []
    for integration in rows:
        config = integration.config or {}
        out.append(
            PlanIntegrationOut(
                integration_id=integration.id, target_host=integration.target_host, target_port=integration.target_port,
                snmp_version=config.get("version"), snmpv3=config.get("snmpv3"),
                poll_interval_seconds=integration.poll_interval_seconds,
                plan=await build_plan(db, integration.device_profile_id) if integration.device_profile_id else None,
            )
        )
    return out


class TelemetryContractOut(BaseModel):
    """One immutable conversion contract the collector pins onto readings it acquires (Issue #128 / G1)."""

    integration_id: uuid.UUID
    source_identifier: str
    mapping_revision_id: uuid.UUID
    revision: int
    canonical_metric: str
    source_unit: str
    source_scale: str
    registry_version: str | None
    conversion_hash: str
    provenance: str
    effective_from: datetime


MAX_TELEMETRY_CONTRACTS = 5000


@router.get("/{collector_id}/telemetry-contracts", response_model=list[TelemetryContractOut])
async def telemetry_contracts(
    collector_id: uuid.UUID, db: AsyncSession = Depends(get_db), collector: Collector = Depends(get_current_collector),
) -> list[TelemetryContractOut]:
    """The current mapping revision of every metric mapping on this collector's assigned, enabled integrations.

    A collector copies `mapping_revision_id` onto each telemetry record at acquisition and replays it unchanged, so a
    later mapping change cannot reinterpret a queued sample. Secret-free; bounded and deterministically ordered.
    """
    if collector.id != collector_id:
        raise UnauthorizedCollectorError("Signed collector identity does not match the URL path.")
    from app.domain.integration.models import CollectorAssignment, Integration
    from app.domain.telemetry.models import IntegrationMetricMapping, IntegrationMetricMappingRevision

    rows = (
        await db.execute(
            select(IntegrationMetricMappingRevision)
            .join(IntegrationMetricMapping, IntegrationMetricMapping.current_revision_id == IntegrationMetricMappingRevision.id)
            .join(Integration, Integration.id == IntegrationMetricMapping.integration_id)
            .join(CollectorAssignment, CollectorAssignment.integration_id == Integration.id)
            .where(
                CollectorAssignment.collector_id == collector_id, CollectorAssignment.effective_to.is_(None),
                Integration.enabled.is_(True),
            )
            .order_by(IntegrationMetricMappingRevision.integration_id, IntegrationMetricMappingRevision.source_identifier)
            .limit(MAX_TELEMETRY_CONTRACTS)
        )
    ).scalars().all()
    return [
        TelemetryContractOut(
            integration_id=row.integration_id, source_identifier=row.source_identifier, mapping_revision_id=row.id,
            revision=row.revision, canonical_metric=row.canonical_metric, source_unit=row.source_unit,
            source_scale=format(row.source_scale, "f"), registry_version=row.registry_version,
            conversion_hash=row.conversion_hash, provenance=row.provenance, effective_from=row.effective_from,
        )
        for row in rows
    ]
