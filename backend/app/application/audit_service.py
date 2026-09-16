"""Writes AuditLog rows synchronously, in the caller's own transaction (§22's "Important
distinction": the primary audit entry is never deferred to the Outbox). Never call this
outside a transaction that also holds the domain mutation it describes."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditLog

_SENSITIVE_FIELD_NAMES = {"password", "password_hash", "access_token", "refresh_token", "secret", "credential"}


def _redact(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    return {k: ("***REDACTED***" if k.lower() in _SENSITIVE_FIELD_NAMES else v) for k, v in payload.items()}


async def write_audit_log(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    request_id: str | None,
    correlation_id: str | None,
    source: str = "api",
    user_agent: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    result: str = "success",
    reason: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        timestamp=datetime.now(UTC),
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id,
        correlation_id=correlation_id,
        source=source,
        user_agent=user_agent,
        before=_redact(before),
        after=_redact(after),
        result=result,
        reason=reason,
    )
    db.add(entry)
    await db.flush()
    return entry
