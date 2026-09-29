"""Per-domain row validators (rack.py/equipment.py/catalog.py) share this result shape
and the `BatchState` duplicate-tracking helper threaded through every row of one job."""

import uuid
from dataclasses import dataclass, field


@dataclass
class RowValidationResult:
    status: str  # "valid" | "invalid"
    action: str | None  # "create" | "update" | None
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    target_managed_asset_id: uuid.UUID | None = None
    target_catalog_model_id: uuid.UUID | None = None
    target_catalog_revision_id: uuid.UUID | None = None
    # SEC (Codex PR #50 review, finding #5): the target entity's own `version` at the
    # moment it was resolved here (update-mode rows only, and only when a target was
    # actually resolved) — see app/domain/bulk_import/models.py::BulkImportRow.
    # expected_version's docstring for why this must be snapshotted here rather than
    # re-derived at commit time.
    expected_version: int | None = None


@dataclass
class BatchState:
    """Threaded through every row of one bulk-import job so within-file duplicates (the
    same asset_tag/catalog identity appearing twice in one workbook) are caught without a
    DB round-trip per row — a DB round-trip still separately catches a duplicate against
    a row that already exists from a *previous* job/request."""

    seen_asset_tags: set[str] = field(default_factory=set)
    # (manufacturer_name, category, model_name) -> row_number first seen at, for catalog
    # create_only duplicate-identity detection within the same file.
    seen_catalog_identities: dict[tuple[str, str, str], int] = field(default_factory=dict)


def error(field_name: str, message: str) -> dict:
    return {"field": field_name, "message": message}


def warning(field_name: str, message: str) -> dict:
    return {"field": field_name, "message": message}
