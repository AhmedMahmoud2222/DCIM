"""Append a new immutable contract revision to an existing metric mapping (Issue #128 / G1).

Internal service only: G1 deliberately adds no HTTP mutation surface. A reviewed mapping-update endpoint
(a later change) calls this. Existing revisions are never touched; readings already stored keep pointing at the
revision they were interpreted under.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.models import (
    IntegrationMetricMapping,
    IntegrationMetricMappingRevision,
    revision_values,
)
from app.domain.telemetry.registry import REGISTRY_VERSION, validate_metric_unit


async def append_mapping_revision(
    db: AsyncSession, mapping_id: uuid.UUID, *, unit: str, scale: Decimal | float | int = 1,
    registry_version: str | None = REGISTRY_VERSION, created_by: uuid.UUID | None = None,
) -> IntegrationMetricMappingRevision:
    """Create revision N+1 and move the mapping's current view to it, atomically.

    The mapping row is locked first, so concurrent callers serialise and receive consecutive revision numbers; the
    `(mapping_id, revision)` unique constraint is the backstop. `effective_from` is the server clock now: a revision
    never applies retroactively, and pinned records keep resolving the revision they carry.
    """
    mapping = (
        await db.execute(
            select(IntegrationMetricMapping).where(IntegrationMetricMapping.id == mapping_id).with_for_update()
        )
    ).scalar_one()
    if registry_version is not None:
        validate_metric_unit(mapping.canonical_metric, unit, registry_version=registry_version)
    last = (
        await db.execute(
            select(func.max(IntegrationMetricMappingRevision.revision)).where(
                IntegrationMetricMappingRevision.mapping_id == mapping.id
            )
        )
    ).scalar_one()
    mapping.unit = unit
    mapping.scale = Decimal(str(scale))  # type: ignore[assignment]
    mapping.registry_version = registry_version
    values = revision_values(
        mapping, revision=(last or 0) + 1, provenance="authored", effective_from=datetime.now(UTC)
    )
    values["created_by"] = created_by
    revision = IntegrationMetricMappingRevision(**values)
    db.add(revision)
    await db.flush()
    mapping.current_revision_id = revision.id
    await db.flush()
    return revision
