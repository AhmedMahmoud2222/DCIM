"""Shared upload-time discipline for the three per-domain upload endpoints
(app/api/v1/racks.py / equipment.py / catalog_designer.py) — content-sniffed (never
trusted by extension or declared content-type, mirroring app/api/v1/floor_plans.py's
`upload_floor_plan_file`) and size-capped before any parsing is attempted."""

import hashlib

from app.application.bulk_import.limits import MAX_BULK_IMPORT_FILE_SIZE_BYTES
from app.core.errors import ApiError
from app.domain.bulk_import.models import IMPORT_MODES

_XLSX_MAGIC = b"PK\x03\x04"


def validate_mode(mode: str) -> str:
    if mode not in IMPORT_MODES:
        raise ApiError(status_code=422, title="Invalid Mode", detail=f"mode must be one of {IMPORT_MODES}, got {mode!r}.")
    return mode


def validate_upload_bytes(content: bytes) -> str:
    """Returns the sha256 hex digest on success; raises ApiError (413/422) otherwise."""
    if len(content) > MAX_BULK_IMPORT_FILE_SIZE_BYTES:
        raise ApiError(
            status_code=413, title="Payload Too Large",
            detail=f"Uploaded file exceeds the maximum allowed size of {MAX_BULK_IMPORT_FILE_SIZE_BYTES} bytes.",
        )
    if content[:4] != _XLSX_MAGIC:
        raise ApiError(
            status_code=422, title="Unsupported File Type",
            detail="File content is not recognized as an XLSX workbook (checked by content, not filename).",
        )
    return hashlib.sha256(content).hexdigest()
