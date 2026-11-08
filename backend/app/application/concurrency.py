"""Generic optimistic-concurrency helper (§17 of the Phase 1 prompt; §36/§7c of the
architecture). Applied in Phase 1 to Room (a real entity) as the reusable pattern later
phases apply to RackPlacement/EquipmentPlacement (via FOR UPDATE + range-exclusion, a
different, more specialized mechanism — see §7c) and PowerConnection (via this same
If-Match/version pattern, §13a)."""

import uuid
from typing import Any, TypeVar

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ConflictError, NotFoundError

_RowT = TypeVar("_RowT")


def parse_if_match(if_match: str | None) -> int | None:
    """`If-Match` is expected to carry the resource's current `version` as a plain
    integer (e.g. `If-Match: 3`). Returns None when the header is absent — callers decide
    whether a missing If-Match is acceptable for their endpoint."""
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            title="Bad Request",
            detail="If-Match header must be an integer version.",
        ) from exc


def require_if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> int:
    version = parse_if_match(if_match)
    if version is None:
        raise ApiError(
            status_code=428,
            title="Precondition Required",
            detail="This operation requires an If-Match header carrying the resource's current version.",
        )
    return version


def check_version_match(*, expected: int, actual: int) -> None:
    if expected != actual:
        raise ConflictError(
            detail=f"Resource has been modified by another request (expected version {expected}, current version {actual})."
        )


async def lock_versioned_row(
    db: AsyncSession, model: type[_RowT], row_id: uuid.UUID, *, expected_version: int, label: str | None = None
) -> _RowT:
    """Atomic check-and-write for `If-Match` mutations: `SELECT ... FOR UPDATE`, *then* compare.

    Loading the row and comparing in Python (`db.get` + `check_version_match`) is not atomic
    with the later UPDATE: under READ COMMITTED two requests can both read version N, both
    pass the check, and both commit N+1. The row lock makes the second request wait for the
    first to finish; `populate_existing` then refreshes any identity-map copy so the
    comparison sees the committed post-lock version and the loser gets a 409. Same primitive
    as `lock_draft_revision_for_edit` (catalog_designer_service.py).

    The caller mutates the returned row and increments `.version` itself, in the same
    transaction as its audit/outbox writes."""
    row = await db.get(model, row_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise NotFoundError(f"{label or model.__name__} {row_id} not found.")
    actual: Any = getattr(row, "version")  # noqa: B009 - generic over any versioned model
    check_version_match(expected=expected_version, actual=actual)
    return row
