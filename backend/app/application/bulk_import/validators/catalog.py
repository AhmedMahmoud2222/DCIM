"""Catalog bulk-import row validator.

Unlike rack/equipment import (which must never auto-create legacy catalog rows),
catalog import IS the catalog-authoring pipeline — `create_only` mode is expected to
find-or-create the `Manufacturer`/`CatalogModel` and always creates a brand-new draft
`CatalogModelRevision` (see commit/catalog.py). Validation therefore never treats a
missing manufacturer/model as an error in `create_only` mode (it will be created at
commit); in `update_existing` mode every referenced row must already exist, and a
`revision_number` targeting anything other than a `draft` revision is a hard row error —
`lock_draft_revision_for_edit`'s own immutability guard is the authoritative check, but
surfacing the same rule here gives the operator an accurate preview before committing."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.bulk_import.resolvers import RowRejected, cell_str, optional_float, optional_int
from app.application.bulk_import.validators import BatchState, RowValidationResult, error
from app.domain.catalog.designer_models import (
    AIRFLOW_DIRECTIONS,
    CATEGORIES,
    DIMENSION_UNITS,
    POWER_REDUNDANCY_MODES,
    WEIGHT_UNITS,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
)
from app.domain.placement.models import PLACEMENT_TYPES

_BRIDGED_CATEGORIES = ("rack", "equipment")


def _split_csv(value: object) -> list[str]:
    text = cell_str(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


async def validate_row(
    db: AsyncSession, *, row_number: int, raw: dict, mode: str, batch_state: BatchState
) -> RowValidationResult:
    errors: list[dict] = []
    warnings: list[dict] = []

    manufacturer_name = cell_str(raw.get("manufacturer_name"))
    category = cell_str(raw.get("category"))
    model_name = cell_str(raw.get("model_name"))

    if not manufacturer_name:
        errors.append(error("manufacturer_name", "manufacturer_name is required."))
    if not model_name:
        errors.append(error("model_name", "model_name is required."))
    if not category:
        errors.append(error("category", "category is required."))
    elif category not in CATEGORIES:
        errors.append(error("category", f"category must be one of {CATEGORIES}, got {category!r}."))
    elif category not in _BRIDGED_CATEGORIES:
        errors.append(
            error("category", f"category {category!r} is not yet supported by the catalog designer workflow "
                  f"(only {_BRIDGED_CATEGORIES} in this phase).")
        )

    if manufacturer_name and model_name and category:
        identity = (manufacturer_name, category, model_name)
        if identity in batch_state.seen_catalog_identities:
            first_row = batch_state.seen_catalog_identities[identity]
            errors.append(
                error(
                    "model_name",
                    f"Duplicate catalog identity (manufacturer={manufacturer_name!r}, category={category!r}, "
                    f"model={model_name!r}) also appears at row {first_row} in this file.",
                )
            )
        else:
            batch_state.seen_catalog_identities[identity] = row_number

    dimension_unit = cell_str(raw.get("dimension_unit"))
    if dimension_unit is not None and dimension_unit not in DIMENSION_UNITS:
        errors.append(error("dimension_unit", f"dimension_unit must be one of {DIMENSION_UNITS}."))
    weight_unit = cell_str(raw.get("weight_unit"))
    if weight_unit is not None and weight_unit not in WEIGHT_UNITS:
        errors.append(error("weight_unit", f"weight_unit must be one of {WEIGHT_UNITS}."))
    airflow_direction = cell_str(raw.get("airflow_direction"))
    if airflow_direction is not None and airflow_direction not in AIRFLOW_DIRECTIONS:
        errors.append(error("airflow_direction", f"airflow_direction must be one of {AIRFLOW_DIRECTIONS}."))
    power_redundancy_mode = cell_str(raw.get("power_redundancy_mode"))
    if power_redundancy_mode is not None and power_redundancy_mode not in POWER_REDUNDANCY_MODES:
        errors.append(error("power_redundancy_mode", f"power_redundancy_mode must be one of {POWER_REDUNDANCY_MODES}."))

    for placement_type in _split_csv(raw.get("supported_placement_types")):
        if placement_type not in PLACEMENT_TYPES:
            errors.append(
                error(
                    "supported_placement_types",
                    f"{placement_type!r} is not a valid placement type; must be one of {PLACEMENT_TYPES}.",
                )
            )

    for field_name in (
        "width_value", "height_value", "depth_value", "weight_value", "rated_power_w", "typical_power_w",
        "max_power_w", "heat_dissipation_btu_hr",
    ):
        try:
            optional_float(raw.get(field_name), field_name)
        except RowRejected as exc:
            errors.append(error(field_name, str(exc)))

    revision_number = None
    try:
        revision_number = optional_int(raw.get("revision_number"), "revision_number")
    except RowRejected as exc:
        errors.append(error("revision_number", str(exc)))
    clone_from_revision_number = None
    try:
        clone_from_revision_number = optional_int(raw.get("clone_from_revision_number"), "clone_from_revision_number")
    except RowRejected as exc:
        errors.append(error("clone_from_revision_number", str(exc)))

    try:
        rack_unit_height = optional_int(raw.get("rack_unit_height"), "rack_unit_height")
    except RowRejected as exc:
        errors.append(error("rack_unit_height", str(exc)))
        rack_unit_height = None
    if rack_unit_height is not None and rack_unit_height <= 0:
        errors.append(error("rack_unit_height", "rack_unit_height must be positive."))

    target_catalog_model_id = None
    target_catalog_revision_id = None
    expected_version = None
    action = "create"

    if mode == "update_existing":
        action = "update"
        if revision_number is None:
            errors.append(error("revision_number", "revision_number is required in update_existing mode."))
        elif manufacturer_name and model_name and category and not errors:
            manufacturer = (
                await db.execute(select(Manufacturer).where(Manufacturer.name == manufacturer_name))
            ).scalar_one_or_none()
            if manufacturer is None:
                errors.append(error("manufacturer_name", f"No such manufacturer {manufacturer_name!r}."))
            else:
                model = (
                    await db.execute(
                        select(CatalogModel).where(
                            CatalogModel.manufacturer_id == manufacturer.id, CatalogModel.model_name == model_name
                        )
                    )
                ).scalar_one_or_none()
                if model is None:
                    errors.append(error("model_name", f"No such catalog model {manufacturer_name!r} / {model_name!r}."))
                elif model.category != category:
                    errors.append(
                        error("category", f"Existing model {model_name!r} has category {model.category!r}, not {category!r}.")
                    )
                else:
                    target_catalog_model_id = model.id
                    revision = (
                        await db.execute(
                            select(CatalogModelRevision).where(
                                CatalogModelRevision.catalog_model_id == model.id,
                                CatalogModelRevision.revision_number == revision_number,
                            )
                        )
                    ).scalar_one_or_none()
                    if revision is None:
                        errors.append(error("revision_number", f"No such revision number {revision_number} for this model."))
                    elif revision.lifecycle_status != "draft":
                        errors.append(
                            error(
                                "revision_number",
                                f"Revision {revision_number} is {revision.lifecycle_status!r}, not draft; it cannot be updated.",
                            )
                        )
                    else:
                        target_catalog_revision_id = revision.id
                        # SEC (Codex PR #50 review, finding #5): snapshot the revision's
                        # own `version` now, at the moment it's resolved here — see
                        # validators/rack.py's identical comment for the full reasoning
                        # (commit/catalog.py currently re-fetches "the current version"
                        # at commit time instead, which can never detect staleness since
                        # it always trivially matches itself).
                        expected_version = revision.version
    else:
        if clone_from_revision_number is not None and manufacturer_name and model_name and category:
            manufacturer = (
                await db.execute(select(Manufacturer).where(Manufacturer.name == manufacturer_name))
            ).scalar_one_or_none()
            if manufacturer is not None:
                model = (
                    await db.execute(
                        select(CatalogModel).where(
                            CatalogModel.manufacturer_id == manufacturer.id, CatalogModel.model_name == model_name
                        )
                    )
                ).scalar_one_or_none()
                if model is not None:
                    source = (
                        await db.execute(
                            select(CatalogModelRevision).where(
                                CatalogModelRevision.catalog_model_id == model.id,
                                CatalogModelRevision.revision_number == clone_from_revision_number,
                            )
                        )
                    ).scalar_one_or_none()
                    if source is None:
                        errors.append(
                            error(
                                "clone_from_revision_number",
                                f"No such revision number {clone_from_revision_number} to clone from.",
                            )
                        )
                    elif source.lifecycle_status not in ("published", "retired"):
                        errors.append(
                            error(
                                "clone_from_revision_number",
                                f"Revision {clone_from_revision_number} is {source.lifecycle_status!r}; only a published or "
                                "retired revision may be cloned.",
                            )
                        )
            # A brand-new manufacturer/model (not found yet) is not itself an error here —
            # if it doesn't exist, clone_from_revision_number cannot refer to anything real
            # either, so this is only a meaningful check once the model is known to exist.

    status = "invalid" if errors else "valid"
    return RowValidationResult(
        status=status, action=action, errors=errors, warnings=warnings,
        target_catalog_model_id=target_catalog_model_id, target_catalog_revision_id=target_catalog_revision_id,
        expected_version=expected_version,
    )
