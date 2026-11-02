"""G9a publication gate and compatibility; pure tests, no database required."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.application import catalog_designer_service as service
from app.application.catalog_designer_service import _convert_physical
from app.core.config import Settings
from app.core.errors import ConflictError
from app.domain.catalog.physical_projection import project_physical


def revision(value="7.5", weight="0.5", dimension_unit="in", weight_unit="lb"):
    return SimpleNamespace(
        rack_unit_height=2, width_value=Decimal(value) if value is not None else None,
        depth_value=Decimal(value) if value is not None else None,
        weight_value=Decimal(weight) if weight is not None else None,
        dimension_unit=dimension_unit, weight_unit=weight_unit,
    )


@pytest.mark.parametrize("value,weight,expected", [
    ("19", "1000.1", (2, 483, 483, 454)),
    ("7.5", "0.5", (2, 191, 191, 1)),
    ("2.5", "1.1", (2, 64, 64, 1)),
    ("2.5", "2.2046", (2, 64, 64, 1)),
])
def test_decimal_projection_vectors(value, weight, expected):
    assert project_physical(revision(value, weight)) == expected


def test_passthrough_ties_nulls_zero_and_authored_precision():
    draft = revision("100.5000", "0.0001", "mm", "kg")
    before = vars(draft).copy()
    assert project_physical(draft) == (2, 101, 101, 1)
    assert vars(draft) == before
    assert project_physical(revision(None, None)) == (2, None, None, None)
    assert project_physical(revision("0", "0", "mm", "kg")) == (2, 0, 0, 0)


@pytest.mark.parametrize("value,weight,dim,unit", [
    ("7.5", "0.5", "in", "lb"), ("2.5", "1.1", "in", "lb"),
    ("19", "1000.1", "in", "lb"), ("100.5", "2.5", "mm", "kg"),
])
def test_default_preserves_legacy_float_half_even(value, weight, dim, unit):
    draft = revision(value, weight, dim, unit)
    expected = (
        2, round(float(value) * (25.4 if dim == "in" else 1)),
        round(float(value) * (25.4 if dim == "in" else 1)),
        round(float(weight) * (0.45359237 if unit == "lb" else 1)),
    )
    assert _convert_physical(draft) == expected


def test_gate_changes_equipment_only_rack_capacity_stays_legacy():
    draft = revision()
    assert _convert_physical(draft, category="equipment", decimal_projection=True) == (2, 191, 191, 1)
    assert _convert_physical(draft, category="rack", decimal_projection=True) == (2, 190, 190, 0)


def test_configuration_gate_defaults_off():
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://unused/unused", redis_url="redis://unused",
        jwt_secret_key="x" * 32, credential_encryption_key="x" * 32,
    )
    assert settings.catalog_decimal_publication_projection is False


@pytest.mark.parametrize("value,weight,dim,unit", [
    ("NaN", "1", "mm", "kg"), ("Infinity", "1", "mm", "kg"),
    ("-1", "1", "mm", "kg"), ("1", "-1", "mm", "kg"),
    ("2147483648", "1", "mm", "kg"), ("1", "2147483648", "mm", "kg"),
    ("1", "1", "kg", "kg"), ("1", "1", "mm", "mm"),
])
def test_prototype_rejects_invalid_physical_values(value, weight, dim, unit):
    with pytest.raises(ValueError):
        project_physical(revision(value, weight, dim, unit))


def test_integer_boundary_is_storable():
    assert project_physical(revision("2147483647", "2147483647", "mm", "kg")) == (
        2, 2147483647, 2147483647, 2147483647,
    )


def publish_session(draft, category="equipment"):
    """In-memory session double; exercise orchestration without any SQL connection."""
    empty = SimpleNamespace(scalars=lambda: [])
    result = SimpleNamespace(scalar_one_or_none=lambda: draft)
    model = SimpleNamespace(category=category, manufacturer_id=uuid4(), model_name="test")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[result, empty, empty, empty, empty]),
        get=AsyncMock(side_effect=[model, SimpleNamespace(name="test")]),
        flush=AsyncMock(), added=[],
    )
    db.add = db.added.append
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,expected", [(False, (190, 0)), (True, (191, 1))])
async def test_publish_uses_configuration_without_modifying_authored_values(monkeypatch, enabled, expected):
    draft = revision()
    draft.id, draft.catalog_model_id, draft.lifecycle_status = uuid4(), uuid4(), "draft"
    draft.supported_placement_types = ["rack_mounted"]
    draft.height_value = Decimal("3.5")
    draft.rated_power_w = draft.typical_power_w = draft.max_power_w = None
    before = draft.width_value, draft.depth_value, draft.weight_value
    db = publish_session(draft)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(catalog_decimal_publication_projection=enabled))
    monkeypatch.setattr(service, "_find_or_create_equipment_model", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "EquipmentModelRevision", lambda **kwargs: SimpleNamespace(id=uuid4(), **kwargs))
    await service.publish_revision(db, revision_id=draft.id, user_id=uuid4())
    assert (db.added[0].width_mm, db.added[0].weight_kg) == expected
    assert (draft.width_value, draft.depth_value, draft.weight_value) == before


@pytest.mark.asyncio
async def test_published_record_is_never_reprojected(monkeypatch):
    draft = revision()
    draft.id, draft.lifecycle_status = uuid4(), "published"
    db = publish_session(draft)
    monkeypatch.setattr(service, "get_settings", lambda: pytest.fail("published revisions must not consult the new gate"))
    with pytest.raises(ConflictError):
        await service.publish_revision(db, revision_id=draft.id, user_id=uuid4())
    assert not db.added
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_projection_stops_before_legacy_insert(monkeypatch):
    draft = revision("2147483648", "1", "mm", "kg")
    draft.id, draft.catalog_model_id, draft.lifecycle_status = uuid4(), uuid4(), "draft"
    draft.supported_placement_types = ["rack_mounted"]
    draft.height_value = Decimal("3.5")
    draft.rated_power_w = draft.typical_power_w = draft.max_power_w = None
    db = publish_session(draft)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(catalog_decimal_publication_projection=True))
    with pytest.raises(service.ValidationFailed) as caught:
        await service.publish_revision(db, revision_id=draft.id, user_id=uuid4())
    assert caught.value.summary.errors[0].code == "invalid_physical_projection"
    assert draft.lifecycle_status == "draft"
    assert not db.added
    db.flush.assert_not_awaited()
