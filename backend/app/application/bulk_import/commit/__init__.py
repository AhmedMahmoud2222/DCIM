"""Per-domain commit functions (rack.py/equipment.py/catalog.py) share this result
shape. Every `commit_row` re-resolves references from `row.raw_data` rather than
trusting anything cached from the validate pass (other than the cheap, stable
`target_managed_asset_id`/`target_catalog_model_id`/`target_catalog_revision_id` the
validator already persisted) — the commit may run a meaningful time after validation, and
re-resolution is also what lets `IntegrityError`/domain-exception handling in the Celery
task (app/infrastructure/tasks/bulk_import.py) turn a genuinely stale reference into a
clean row-level failure instead of an inconsistent write."""

import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError


@dataclass
class RowCommitResult:
    status: str  # "committed" | "failed"
    message: str | None = None
    target_managed_asset_id: uuid.UUID | None = None
    target_catalog_model_id: uuid.UUID | None = None
    target_catalog_revision_id: uuid.UUID | None = None


def describe_integrity_error(exc: IntegrityError) -> str:
    """A raw Postgres error must never surface to a caller (matching this codebase's
    app/core/errors.py::_integrity_error_handler convention for the synchronous HTTP
    path) — translate the handful of constraint violations this pipeline can actually
    hit into a readable row-level message, with a safe generic fallback for anything
    else."""
    # `exc.orig` (the raw driver exception) carries the constraint name but not the
    # table/SQL text that only SQLAlchemy's own wrapper __str__ adds — check both so this
    # keeps working regardless of which string actually contains the identifying bit.
    text = f"{getattr(exc, 'orig', '')} {exc}".lower()
    # migration 0004_phase2_physical_spatial_model's `no_front_overlap`/`no_rear_overlap`
    # GiST exclusion constraints — the authoritative guard against two rows in the same
    # file (or a row vs. pre-existing state) occupying the same U range on the same rack.
    if "overlap" in text:
        return "Overlapping U position on this rack."
    if "asset_tag" in text:
        return "This asset_tag already exists (unique constraint violation)."
    if "rack_placement" in text:
        return "This rack's placement conflicts with a concurrent change."
    return "This row conflicts with existing data and could not be committed."
