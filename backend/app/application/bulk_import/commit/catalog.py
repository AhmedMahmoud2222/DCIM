"""Catalog bulk-import commit. `create_only` mode finds-or-creates the `Manufacturer`/
`CatalogModel` (the same `INSERT ... ON CONFLICT DO NOTHING RETURNING id` pattern
`app/application/catalog_designer_service.py`'s `_find_or_create_rack_model`/
`_find_or_create_equipment_model` already use for the legacy bridge tables) and always
creates a brand-new draft `CatalogModelRevision` — via `clone_revision` when
`clone_from_revision_number` is given, otherwise via the same construction path as
`POST /catalog/models/{model_id}/revisions` (app/api/v1/catalog_designer.py). Never
publishes anything — import only ever produces/edits drafts.

`update_existing` mode goes through `lock_draft_revision_for_edit` — the same atomicity
primitive `PATCH /catalog/revisions/{id}` uses — so a row targeting anything other than a
`draft` revision fails cleanly (never a silent no-op, never touching the row's bytes) and
two concurrent writers to the same revision serialize correctly."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.bulk_import.commit import RowCommitResult
from app.application.bulk_import.resolvers import RowRejected, cell_str, optional_float, optional_int
from app.application.catalog_designer_service import allocate_revision_number, clone_revision, lock_draft_revision_for_edit
from app.application.outbox_service import write_outbox_event
from app.core.errors import ConflictError, NotFoundError
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow
from app.domain.catalog.designer_models import CatalogModel, CatalogModelRevision, Manufacturer

_REVISION_SCALAR_FIELDS = (
    "dimension_unit", "width_value", "height_value", "depth_value", "rack_unit_height", "weight_unit", "weight_value",
    "mounting_orientation", "airflow_direction", "rated_power_w", "typical_power_w", "max_power_w",
    "heat_dissipation_btu_hr", "power_redundancy_mode",
)


def _split_csv(value: object) -> list[str]:
    text = cell_str(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _scalar_overrides(raw: dict) -> dict:
    overrides: dict = {}
    for field_name in _REVISION_SCALAR_FIELDS:
        value: object
        if field_name in ("width_value", "height_value", "depth_value", "weight_value", "rated_power_w",
                          "typical_power_w", "max_power_w", "heat_dissipation_btu_hr"):
            value = optional_float(raw.get(field_name), field_name)
        elif field_name == "rack_unit_height":
            value = optional_int(raw.get(field_name), field_name)
        else:
            value = cell_str(raw.get(field_name))
        if value is not None:
            overrides[field_name] = value
    placement_types = _split_csv(raw.get("supported_placement_types"))
    if placement_types:
        overrides["supported_placement_types"] = placement_types
    return overrides


async def _find_or_create_manufacturer(db: AsyncSession, name: str) -> Manufacturer:
    inserted_id = (
        await db.execute(
            pg_insert(Manufacturer).values(id=uuid.uuid4(), name=name).on_conflict_do_nothing(
                constraint="uq_manufacturer_name"
            ).returning(Manufacturer.id)
        )
    ).scalar_one_or_none()
    manufacturer_id = inserted_id or (
        await db.execute(select(Manufacturer.id).where(Manufacturer.name == name))
    ).scalar_one()
    manufacturer = await db.get(Manufacturer, manufacturer_id)
    assert manufacturer is not None
    return manufacturer


async def _find_or_create_catalog_model(
    db: AsyncSession, *, manufacturer: Manufacturer, category: str, model_name: str, model_number: str | None,
    subtype: str | None, description: str | None, tags: list[str],
) -> CatalogModel:
    existing = (
        await db.execute(
            select(CatalogModel).where(CatalogModel.manufacturer_id == manufacturer.id, CatalogModel.model_name == model_name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.category != category:
            raise RowRejected(
                "category", f"Existing model {model_name!r} has category {existing.category!r}, not {category!r}."
            )
        return existing
    model = CatalogModel(
        manufacturer_id=manufacturer.id, category=category, subtype=subtype, model_name=model_name,
        model_number=model_number, description=description, tags=tags,
    )
    db.add(model)
    await db.flush()
    return model


async def commit_row(db: AsyncSession, *, job: BulkImportJob, row: BulkImportRow, mode: str) -> RowCommitResult:
    raw = row.raw_data
    manufacturer_name = cell_str(raw.get("manufacturer_name"))
    category = cell_str(raw.get("category"))
    model_name = cell_str(raw.get("model_name"))
    if not manufacturer_name or not category or not model_name:
        raise RowRejected("model_name", "manufacturer_name, category, and model_name are all required.")

    if mode == "update_existing":
        revision_number = optional_int(raw.get("revision_number"), "revision_number")
        if revision_number is None:
            raise RowRejected("revision_number", "revision_number is required in update_existing mode.")

        revision_id = row.target_catalog_revision_id
        if revision_id is None:
            manufacturer = (
                await db.execute(select(Manufacturer).where(Manufacturer.name == manufacturer_name))
            ).scalar_one_or_none()
            if manufacturer is None:
                raise RowRejected("manufacturer_name", f"No such manufacturer {manufacturer_name!r}.")
            model = (
                await db.execute(
                    select(CatalogModel).where(
                        CatalogModel.manufacturer_id == manufacturer.id, CatalogModel.model_name == model_name
                    )
                )
            ).scalar_one_or_none()
            if model is None:
                raise RowRejected("model_name", f"No such catalog model {manufacturer_name!r} / {model_name!r}.")
            revision_row = (
                await db.execute(
                    select(CatalogModelRevision).where(
                        CatalogModelRevision.catalog_model_id == model.id,
                        CatalogModelRevision.revision_number == revision_number,
                    )
                )
            ).scalar_one_or_none()
            if revision_row is None:
                raise RowRejected("revision_number", f"No such revision number {revision_number} for this model.")
            revision_id = revision_row.id

        current = await db.get(CatalogModelRevision, revision_id)
        if current is None:
            raise RowRejected("revision_number", "The target revision no longer exists.")
        try:
            revision = await lock_draft_revision_for_edit(db, revision_id=revision_id, if_match_version=current.version)
        except NotFoundError as exc:
            raise RowRejected("revision_number", "The target revision no longer exists.") from exc
        except ConflictError as exc:
            # Never a silent no-op: a published/retired target is a hard row failure,
            # and the row is left byte-for-byte unchanged (lock_draft_revision_for_edit
            # raises before mutating anything).
            raise RowRejected("revision_number", exc.detail) from exc

        overrides = _scalar_overrides(raw)
        for field_name, value in overrides.items():
            setattr(revision, field_name, value)
        await db.flush()

        await write_audit_log(
            db, actor_user_id=job.uploaded_by_user_id, action="catalog.bulk_import.update_revision",
            entity_type="catalog_model_revision", entity_id=revision.id, request_id=None, correlation_id=str(job.id),
            after={"bulk_import_job_id": str(job.id), **{k: str(v) for k, v in overrides.items()}},
        )
        await write_outbox_event(
            db, event_type="CatalogModelRevisionDraftUpdated", aggregate_type="catalog_model_revision",
            aggregate_id=revision.id, payload={"version": revision.version, "bulk_import_job_id": str(job.id)},
            correlation_id=str(job.id),
        )
        return RowCommitResult(
            status="committed", target_catalog_model_id=revision.catalog_model_id, target_catalog_revision_id=revision.id,
        )

    # create_only
    manufacturer = await _find_or_create_manufacturer(db, manufacturer_name)
    model_number = cell_str(raw.get("model_number"))
    subtype = cell_str(raw.get("subtype"))
    description = cell_str(raw.get("description"))
    tags = _split_csv(raw.get("tags"))
    model = await _find_or_create_catalog_model(
        db, manufacturer=manufacturer, category=category, model_name=model_name, model_number=model_number,
        subtype=subtype, description=description, tags=tags,
    )

    clone_from_revision_number = optional_int(raw.get("clone_from_revision_number"), "clone_from_revision_number")
    overrides = _scalar_overrides(raw)

    if clone_from_revision_number is not None:
        source = (
            await db.execute(
                select(CatalogModelRevision).where(
                    CatalogModelRevision.catalog_model_id == model.id,
                    CatalogModelRevision.revision_number == clone_from_revision_number,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise RowRejected(
                "clone_from_revision_number", f"No such revision number {clone_from_revision_number} to clone from."
            )
        try:
            revision = await clone_revision(db, source_revision_id=source.id, user_id=job.uploaded_by_user_id)
        except (NotFoundError, ConflictError) as exc:
            raise RowRejected("clone_from_revision_number", str(exc)) from exc
        for field_name, value in overrides.items():
            setattr(revision, field_name, value)
        await db.flush()
        action_code = "catalog.bulk_import.clone_revision"
    else:
        revision_number = await allocate_revision_number(db, catalog_model_id=model.id)
        revision = CatalogModelRevision(
            catalog_model_id=model.id, revision_number=revision_number, created_by_user_id=job.uploaded_by_user_id,
            **overrides,
        )
        db.add(revision)
        await db.flush()
        action_code = "catalog.bulk_import.create_revision"

    await write_audit_log(
        db, actor_user_id=job.uploaded_by_user_id, action=action_code, entity_type="catalog_model_revision",
        entity_id=revision.id, request_id=None, correlation_id=str(job.id),
        after={
            "catalog_model_id": str(model.id), "revision_number": revision.revision_number,
            "bulk_import_job_id": str(job.id),
        },
    )
    await write_outbox_event(
        db, event_type="CatalogModelRevisionDraftCreated", aggregate_type="catalog_model_revision",
        aggregate_id=revision.id, payload={"catalog_model_id": str(model.id), "bulk_import_job_id": str(job.id)},
        correlation_id=str(job.id),
    )

    return RowCommitResult(status="committed", target_catalog_model_id=model.id, target_catalog_revision_id=revision.id)
