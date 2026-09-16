"""Generic optimistic-concurrency helper (§17 of the Phase 1 prompt; §36/§7c of the
architecture). Applied in Phase 1 to Room (a real entity) as the reusable pattern later
phases apply to RackPlacement/EquipmentPlacement (via FOR UPDATE + range-exclusion, a
different, more specialized mechanism — see §7c) and PowerConnection (via this same
If-Match/version pattern, §13a)."""

from fastapi import Header

from app.core.errors import ApiError, ConflictError


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
