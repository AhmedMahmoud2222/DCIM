"""Shared row-level lookups used by both the validators (preview-time) and the commit
functions (commit-time re-resolution — see commit/rack.py's module docstring for why
resolution is deliberately re-run rather than trusted from the validate pass). Every
function here raises `RowRejected` naming the offending field/segment; it never mutates
the database and never auto-creates a legacy `RackModel`/`EquipmentModel`/
`*_model_revision` row — only looks them up, per the plan's explicit "never auto-create"
requirement for rack/equipment import."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Building, Floor, Room, Site
from app.domain.physical.models import Rack


class RowRejected(Exception):
    """A single row-level validation/resolution failure. `field` is the spreadsheet
    column (or a synthetic name like 'model') the problem is attributed to."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def cell_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def require_str(value: object, field: str) -> str:
    text = cell_str(value)
    if text is None:
        raise RowRejected(field, f"{field} is required.")
    return text


def optional_int(value: object, field: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        # xlsx numeric cells surface as float/int/str even for whole numbers.
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RowRejected(field, f"{field} must be a whole number, got {value!r}.") from exc


def require_int(value: object, field: str) -> int:
    result = optional_int(value, field)
    if result is None:
        raise RowRejected(field, f"{field} is required.")
    return result


def optional_float(value: object, field: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RowRejected(field, f"{field} must be a number, got {value!r}.") from exc


async def resolve_room(db: AsyncSession, *, site_code: str, building_code: str, floor_level: int, room_code: str) -> Room:
    site = (await db.execute(select(Site).where(Site.code == site_code))).scalar_one_or_none()
    if site is None:
        raise RowRejected("site_code", f"No such site with code {site_code!r}.")
    building = (
        await db.execute(select(Building).where(Building.site_id == site.id, Building.code == building_code))
    ).scalar_one_or_none()
    if building is None:
        raise RowRejected("building_code", f"No such building with code {building_code!r} in site {site_code!r}.")
    floor = (
        await db.execute(select(Floor).where(Floor.building_id == building.id, Floor.level_number == floor_level))
    ).scalar_one_or_none()
    if floor is None:
        raise RowRejected("floor_level", f"No such floor level {floor_level} in building {building_code!r}.")
    room = (await db.execute(select(Room).where(Room.floor_id == floor.id, Room.code == room_code))).scalar_one_or_none()
    if room is None:
        raise RowRejected("room_code", f"No such room with code {room_code!r} on floor level {floor_level}.")
    return room


async def _resolve_ordinal_revision(
    *, revisions: list, manufacturer: str, model_name: str, revision_number: int | None, entity_label: str,
):
    """Legacy `RackModelRevision`/`EquipmentModelRevision` rows carry no `revision_number`
    column of their own (they are immutable-by-convention, ordered only by creation —
    see app/domain/catalog/models.py). The spreadsheet's `revision_number` column is
    therefore interpreted as the 1-based ordinal position among that model's revisions,
    ordered exactly as `GET /rack-models/{id}/revisions`/`GET /equipment-models/{id}/
    revisions` already order them (`created_at` ascending, app/api/v1/catalog.py) — the
    only existing, stable notion of "revision N" for these legacy tables. A blank
    `revision_number` resolves to the latest (highest-ordinal) revision."""
    if not revisions:
        raise RowRejected("model", f"{entity_label} {manufacturer!r} / {model_name!r} has no revisions.")
    if revision_number is None:
        return revisions[-1]
    if not (1 <= revision_number <= len(revisions)):
        raise RowRejected(
            "revision_number",
            f"No such {entity_label.lower()} revision number {revision_number} for {manufacturer!r} / {model_name!r} "
            f"(this model has {len(revisions)} revision(s)).",
        )
    return revisions[revision_number - 1]


async def resolve_rack_model_revision(
    db: AsyncSession, *, manufacturer: str, model_name: str, revision_number: int | None
) -> RackModelRevision:
    rack_model = (
        await db.execute(select(RackModel).where(RackModel.manufacturer == manufacturer, RackModel.model_name == model_name))
    ).scalar_one_or_none()
    if rack_model is None:
        raise RowRejected("model_name", f"No such rack model for manufacturer {manufacturer!r}, model {model_name!r}.")
    revisions = list(
        (
            await db.execute(
                select(RackModelRevision)
                .where(RackModelRevision.rack_model_id == rack_model.id)
                .order_by(RackModelRevision.created_at)
            )
        ).scalars()
    )
    return await _resolve_ordinal_revision(
        revisions=revisions, manufacturer=manufacturer, model_name=model_name, revision_number=revision_number,
        entity_label="Rack model",
    )


async def resolve_equipment_model_revision(
    db: AsyncSession, *, manufacturer: str, model_name: str, revision_number: int | None
) -> EquipmentModelRevision:
    equipment_model = (
        await db.execute(
            select(EquipmentModel).where(EquipmentModel.manufacturer == manufacturer, EquipmentModel.model_name == model_name)
        )
    ).scalar_one_or_none()
    if equipment_model is None:
        raise RowRejected(
            "model_name", f"No such equipment model for manufacturer {manufacturer!r}, model {model_name!r}."
        )
    revisions = list(
        (
            await db.execute(
                select(EquipmentModelRevision)
                .where(EquipmentModelRevision.equipment_model_id == equipment_model.id)
                .order_by(EquipmentModelRevision.created_at)
            )
        ).scalars()
    )
    return await _resolve_ordinal_revision(
        revisions=revisions, manufacturer=manufacturer, model_name=model_name, revision_number=revision_number,
        entity_label="Equipment model",
    )


async def resolve_managed_asset_by_tag(db: AsyncSession, asset_tag: str) -> ManagedAsset | None:
    return (await db.execute(select(ManagedAsset).where(ManagedAsset.asset_tag == asset_tag))).scalar_one_or_none()


async def resolve_rack_by_asset_tag(db: AsyncSession, asset_tag: str) -> tuple[ManagedAsset, Rack]:
    asset = await resolve_managed_asset_by_tag(db, asset_tag)
    if asset is None:
        raise RowRejected("rack_asset_tag", f"No such rack with asset_tag {asset_tag!r}.")
    if asset.asset_type != "rack":
        raise RowRejected("rack_asset_tag", f"asset_tag {asset_tag!r} does not identify a rack.")
    rack = await db.get(Rack, asset.id)
    assert rack is not None
    return asset, rack


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
