"""PR-1 schema and DB guards (docs/superpowers/specs/2026-09-23-phase-10a-asset-catalog-
designer-design.md §4/§4.7/§5.4, aligned plan §3.1). Runs against a real PostgreSQL 16
instance (see conftest.py) — these tests verify the database itself enforces the
invariants, matching this repo's own `test_db_constraints.py` convention, not just that
application code happens to behave.

No API routes, RBAC, import jobs, instance overrides, or installed-asset migration exist
yet (PR-1's own scope, per the aligned plan) — every row below is created directly via the
ORM/raw SQL, simulating what a future PR-3+ service will do."""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import (
    CatalogGraphic,
    CatalogGraphicMarker,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    MonitoringMetricTemplate,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.catalog.models import EquipmentModel, EquipmentModelRevision, RackModel, RackModelRevision


async def _make_user(db_session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"catalog-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Catalog Test User",
        password_hash=hash_password("correct horse battery staple"),
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_manufacturer(db_session, name: str = "Acme Networks") -> Manufacturer:
    manufacturer = Manufacturer(name=name)
    db_session.add(manufacturer)
    await db_session.flush()
    return manufacturer


async def _make_model(db_session, manufacturer_id, category: str = "equipment", model_name: str = "AC-4800") -> CatalogModel:
    model = CatalogModel(manufacturer_id=manufacturer_id, category=category, model_name=model_name)
    db_session.add(model)
    await db_session.flush()
    return model


async def _make_draft(db_session, catalog_model_id, user_id, revision_number: int = 1) -> CatalogModelRevision:
    revision = CatalogModelRevision(
        catalog_model_id=catalog_model_id, revision_number=revision_number, created_by_user_id=user_id
    )
    db_session.add(revision)
    await db_session.flush()
    return revision


async def _publish(db_session, revision: CatalogModelRevision, user_id) -> None:
    """Simulates the future PR-3 publish service's minimal write — this PR does not
    implement the legacy-bridge creation (that is application code, out of PR-1's scope),
    only sets the columns the DB trigger allows a draft -> published transition to set."""
    await db_session.execute(
        text(
            "UPDATE catalog_model_revision SET lifecycle_status = 'published', "
            "published_at = now(), published_by_user_id = :uid WHERE id = :id"
        ),
        {"uid": str(user_id), "id": str(revision.id)},
    )
    await db_session.commit()


# --------------------------------------------------------------------- CHECK/UNIQUE constraints


async def test_manufacturer_name_is_unique(db_session):
    await _make_manufacturer(db_session, name="Dup Co")
    await db_session.commit()
    db_session.add(Manufacturer(name="Dup Co"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_manufacturer_status_must_be_allowed(db_session):
    db_session.add(Manufacturer(name="Bad Status Co", status="bogus"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_model_category_must_be_allowed(db_session):
    manufacturer = await _make_manufacturer(db_session)
    await db_session.commit()
    db_session.add(CatalogModel(manufacturer_id=manufacturer.id, category="not-a-real-category", model_name="X"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_model_manufacturer_and_name_is_unique(db_session):
    manufacturer = await _make_manufacturer(db_session)
    await _make_model(db_session, manufacturer.id, model_name="Dup Model")
    await db_session.commit()
    db_session.add(CatalogModel(manufacturer_id=manufacturer.id, category="equipment", model_name="Dup Model"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_model_revision_number_is_unique_per_model(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    await _make_draft(db_session, model.id, user.id, revision_number=1)
    await db_session.commit()
    db_session.add(CatalogModelRevision(catalog_model_id=model.id, revision_number=1, created_by_user_id=user.id))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_model_revision_lifecycle_status_must_be_allowed(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    db_session.add(
        CatalogModelRevision(
            catalog_model_id=model.id, revision_number=1, created_by_user_id=user.id, lifecycle_status="bogus"
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_model_revision_rated_power_must_be_non_negative(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    db_session.add(
        CatalogModelRevision(
            catalog_model_id=model.id, revision_number=1, created_by_user_id=user.id, rated_power_w=-1
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_network_port_template_stable_key_unique_per_revision(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    db_session.add(
        NetworkPortTemplate(
            catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
            supported_speeds_mbps=[1000], connector_type="rj45", side="front",
        )
    )
    await db_session.commit()
    db_session.add(
        NetworkPortTemplate(
            catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0 dup", media_type="copper",
            supported_speeds_mbps=[1000], connector_type="rj45", side="front",
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_power_supply_template_quantity_must_be_positive(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    db_session.add(
        PowerSupplyTemplate(
            catalog_model_revision_id=revision.id, stable_key="psu-1", label="PSU 1", quantity=0, connector_type="C14"
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_monitoring_metric_template_snmp_requires_no_other_label(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    db_session.add(
        MonitoringMetricTemplate(
            catalog_model_revision_id=revision.id, stable_key="temp", protocol="snmp",
            protocol_other_label="should not be set", metric_name="Inlet Temp", value_type="float",
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_graphic_side_unique_per_revision(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    db_session.add(
        CatalogGraphic(
            catalog_model_revision_id=revision.id, side="front", storage_key="ab/abcd.png",
            original_filename="front.png", mime_type="image/png", file_size_bytes=100, width_px=10, height_px=10,
            uploaded_by_user_id=user.id, uploaded_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    db_session.add(
        CatalogGraphic(
            catalog_model_revision_id=revision.id, side="front", storage_key="cd/cdef.png",
            original_filename="front2.png", mime_type="image/png", file_size_bytes=100, width_px=10, height_px=10,
            uploaded_by_user_id=user.id, uploaded_at=datetime.now(UTC),
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_graphic_marker_coordinates_must_be_normalized(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    graphic = CatalogGraphic(
        catalog_model_revision_id=revision.id, side="front", storage_key="ab/abcd.png", original_filename="front.png",
        mime_type="image/png", file_size_bytes=100, width_px=10, height_px=10, uploaded_by_user_id=user.id,
        uploaded_at=datetime.now(UTC),
    )
    db_session.add(graphic)
    await db_session.flush()
    db_session.add(
        CatalogGraphicMarker(catalog_graphic_id=graphic.id, marker_type="other", label="oob", marker_x=1.5, marker_y=0.5)
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_catalog_graphic_marker_target_must_match_marker_type(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    graphic = CatalogGraphic(
        catalog_model_revision_id=revision.id, side="front", storage_key="ab/abcd.png", original_filename="front.png",
        mime_type="image/png", file_size_bytes=100, width_px=10, height_px=10, uploaded_by_user_id=user.id,
        uploaded_at=datetime.now(UTC),
    )
    db_session.add(graphic)
    port = NetworkPortTemplate(
        catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
        supported_speeds_mbps=[1000], connector_type="rj45", side="front",
    )
    db_session.add(port)
    await db_session.flush()
    # marker_type='other' but a network_port_template_id is set — violates the CHECK.
    db_session.add(
        CatalogGraphicMarker(
            catalog_graphic_id=graphic.id, marker_type="other", network_port_template_id=port.id, marker_x=0.1, marker_y=0.1
        )
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


# --------------------------------------------------------------------- Immutability trigger


async def test_published_revision_children_are_immutable(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    port = NetworkPortTemplate(
        catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
        supported_speeds_mbps=[1000], connector_type="rj45", side="front",
    )
    db_session.add(port)
    await db_session.commit()
    # Captured before any rollback below expires these ORM instances — an async session's
    # expired attributes require an awaited reload, which a bare `str(obj.id)` cannot do.
    port_id = str(port.id)
    revision_id = str(revision.id)

    await _publish(db_session, revision, user.id)

    # UPDATE an existing child of a now-published revision — rejected.
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="not a draft"):
        await db_session.execute(
            text("UPDATE network_port_template SET display_name = 'renamed' WHERE id = :id"), {"id": port_id}
        )
        await db_session.commit()
    await db_session.rollback()

    # INSERT a new child under a published revision — rejected.
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="not a draft"):
        await db_session.execute(
            text(
                "INSERT INTO network_port_template "
                "(id, catalog_model_revision_id, stable_key, display_name, media_type, "
                "supported_speeds_mbps, connector_type, side) "
                "VALUES (gen_random_uuid(), :rev, 'eth1', 'eth1', 'copper', '[1000]', 'rj45', 'front')"
            ),
            {"rev": revision_id},
        )
        await db_session.commit()
    await db_session.rollback()

    # DELETE an existing child of a published revision — rejected.
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="not a draft"):
        await db_session.execute(text("DELETE FROM network_port_template WHERE id = :id"), {"id": port_id})
        await db_session.commit()
    await db_session.rollback()


async def test_draft_revision_children_remain_mutable(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    port = NetworkPortTemplate(
        catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
        supported_speeds_mbps=[1000], connector_type="rj45", side="front",
    )
    db_session.add(port)
    await db_session.commit()

    port.display_name = "renamed while draft"
    await db_session.commit()

    await db_session.execute(text("DELETE FROM network_port_template WHERE id = :id"), {"id": str(port.id)})
    await db_session.commit()


async def test_cross_revision_marker_is_rejected(db_session):
    """§4.5: a marker on revision A's graphic may not target a component belonging to a
    different revision B, even though the FK to network_port_template.id is individually
    valid."""
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision_a = await _make_draft(db_session, model.id, user.id, revision_number=1)
    revision_b = await _make_draft(db_session, model.id, user.id, revision_number=2)

    graphic_a = CatalogGraphic(
        catalog_model_revision_id=revision_a.id, side="front", storage_key="a1/a1a1.png", original_filename="a.png",
        mime_type="image/png", file_size_bytes=1, width_px=1, height_px=1, uploaded_by_user_id=user.id,
        uploaded_at=datetime.now(UTC),
    )
    port_b = NetworkPortTemplate(
        catalog_model_revision_id=revision_b.id, stable_key="eth0", display_name="eth0", media_type="copper",
        supported_speeds_mbps=[1000], connector_type="rj45", side="front",
    )
    db_session.add_all([graphic_a, port_b])
    await db_session.flush()

    db_session.add(
        CatalogGraphicMarker(
            catalog_graphic_id=graphic_a.id, marker_type="network_port", network_port_template_id=port_b.id,
            marker_x=0.5, marker_y=0.5,
        )
    )
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="different revision"):
        await db_session.commit()
    await db_session.rollback()


async def test_marker_valid_for_its_own_revision_is_accepted(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    graphic = CatalogGraphic(
        catalog_model_revision_id=revision.id, side="front", storage_key="a1/a1a1.png", original_filename="a.png",
        mime_type="image/png", file_size_bytes=1, width_px=1, height_px=1, uploaded_by_user_id=user.id,
        uploaded_at=datetime.now(UTC),
    )
    port = NetworkPortTemplate(
        catalog_model_revision_id=revision.id, stable_key="eth0", display_name="eth0", media_type="copper",
        supported_speeds_mbps=[1000], connector_type="rj45", side="front",
    )
    db_session.add_all([graphic, port])
    await db_session.flush()
    db_session.add(
        CatalogGraphicMarker(
            catalog_graphic_id=graphic.id, marker_type="network_port", network_port_template_id=port.id,
            marker_x=0.5, marker_y=0.5,
        )
    )
    await db_session.commit()


# --------------------------------------------------------------------- Lifecycle trigger


async def test_publish_then_further_field_edit_is_rejected(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="rejects this column change"):
        await db_session.execute(
            text("UPDATE catalog_model_revision SET rated_power_w = 999 WHERE id = :id"), {"id": str(revision.id)}
        )
        await db_session.commit()
    await db_session.rollback()


async def test_publish_then_lifecycle_reversal_is_rejected(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="cannot transition"):
        await db_session.execute(
            text("UPDATE catalog_model_revision SET lifecycle_status = 'draft' WHERE id = :id"), {"id": str(revision.id)}
        )
        await db_session.commit()
    await db_session.rollback()


async def test_retire_published_revision_succeeds_and_locks_further_edits(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    await db_session.execute(
        text(
            "UPDATE catalog_model_revision SET lifecycle_status = 'retired', retired_at = now(), "
            "retired_by_user_id = :uid, retirement_reason = 'superseded' WHERE id = :id"
        ),
        {"uid": str(user.id), "id": str(revision.id)},
    )
    await db_session.commit()

    # Once retired, only allow_installation_when_retired may still change.
    await db_session.execute(
        text("UPDATE catalog_model_revision SET allow_installation_when_retired = true WHERE id = :id"),
        {"id": str(revision.id)},
    )
    await db_session.commit()

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="rejects this column change"):
        await db_session.execute(
            text("UPDATE catalog_model_revision SET retirement_reason = 'changed my mind' WHERE id = :id"),
            {"id": str(revision.id)},
        )
        await db_session.commit()
    await db_session.rollback()


async def test_draft_revision_scalar_fields_remain_mutable(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)

    revision.rated_power_w = 123.45
    await db_session.commit()
    assert revision.rated_power_w == pytest.approx(123.45)


# --------------------------------------------------------------------- Identity-lock triggers


async def test_catalog_model_identity_locked_once_published(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="identity fields are immutable"):
        await db_session.execute(
            text("UPDATE catalog_model SET model_name = 'renamed' WHERE id = :id"), {"id": str(model.id)}
        )
        await db_session.commit()
    await db_session.rollback()


async def test_catalog_model_editorial_fields_remain_mutable_after_publish(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    # description/tags/status are explicitly NOT identity fields — remain editable.
    await db_session.execute(
        text("UPDATE catalog_model SET description = 'updated blurb', status = 'deprecated' WHERE id = :id"),
        {"id": str(model.id)},
    )
    await db_session.commit()


async def test_catalog_model_identity_freely_editable_before_any_publish(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    await db_session.execute(
        text("UPDATE catalog_model SET model_name = 'still a draft, freely renamed' WHERE id = :id"),
        {"id": str(model.id)},
    )
    await db_session.commit()


async def test_manufacturer_name_locked_once_any_model_published(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)
    await _publish(db_session, revision, user.id)

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="name is immutable"):
        await db_session.execute(
            text("UPDATE manufacturer SET name = 'renamed' WHERE id = :id"), {"id": str(manufacturer.id)}
        )
        await db_session.commit()
    await db_session.rollback()


async def test_manufacturer_name_freely_editable_before_any_publish(db_session):
    manufacturer = await _make_manufacturer(db_session)
    await _make_model(db_session, manufacturer.id)
    await db_session.commit()
    await db_session.execute(
        text("UPDATE manufacturer SET name = 'renamed while unpublished' WHERE id = :id"), {"id": str(manufacturer.id)}
    )
    await db_session.commit()


# --------------------------------------------------------------------- Legacy bridge (§4.7)


async def test_legacy_bridge_xor_check_rejects_both_populated(db_session):
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id)
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)

    rack_model = RackModel(manufacturer="Acme Networks", model_name="Legacy Rack XOR")
    db_session.add(rack_model)
    await db_session.flush()
    rack_revision = RackModelRevision(rack_model_id=rack_model.id, height_u=42, width_mm=600, depth_mm=1000)
    db_session.add(rack_revision)
    await db_session.flush()

    equipment_model = EquipmentModel(manufacturer="Acme Networks", model_name="Legacy Equip XOR")
    db_session.add(equipment_model)
    await db_session.flush()
    equipment_revision = EquipmentModelRevision(equipment_model_id=equipment_model.id)
    db_session.add(equipment_revision)
    await db_session.flush()

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.execute(
            text(
                "UPDATE catalog_model_revision SET legacy_rack_model_revision_id = :r, "
                "legacy_equipment_model_revision_id = :e WHERE id = :id"
            ),
            {"r": str(rack_revision.id), "e": str(equipment_revision.id), "id": str(revision.id)},
        )
        await db_session.commit()
    await db_session.rollback()


async def test_bridged_legacy_row_is_immutable_but_unbridged_rows_remain_usable(db_session):
    """The exact distinction the task calls out: a row with
    `bridged_from_catalog_revision_id` set is frozen at the DB level; a row without it
    (every pre-Phase-10A row, and any future non-bridged legacy row) keeps its present,
    unrestricted behavior."""
    manufacturer = await _make_manufacturer(db_session)
    model = await _make_model(db_session, manufacturer.id, category="rack", model_name="Bridged Rack")
    user = await _make_user(db_session)
    revision = await _make_draft(db_session, model.id, user.id)

    # Simulate the future PR-3 publish-time bridge-creation service (out of PR-1's own
    # scope) by hand, via raw SQL, exactly as the task asks: "exercise constraints and
    # triggers through direct SQL."
    rack_model = RackModel(manufacturer="Acme Networks", model_name="Bridged Rack Legacy")
    db_session.add(rack_model)
    await db_session.flush()
    rack_revision = RackModelRevision(rack_model_id=rack_model.id, height_u=42, width_mm=600, depth_mm=1000)
    db_session.add(rack_revision)
    await db_session.flush()

    await db_session.execute(
        text(
            "UPDATE rack_model_revision SET bridged_from_catalog_revision_id = :rev WHERE id = :id"
        ),
        {"rev": str(revision.id), "id": str(rack_revision.id)},
    )
    await db_session.execute(
        text("UPDATE catalog_model_revision SET legacy_rack_model_revision_id = :legacy WHERE id = :id"),
        {"legacy": str(rack_revision.id), "id": str(revision.id)},
    )
    await db_session.commit()

    # Bridged row: any further UPDATE, even to an unrelated column, is rejected.
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="immutable"):
        await db_session.execute(
            text("UPDATE rack_model_revision SET height_u = 43 WHERE id = :id"), {"id": str(rack_revision.id)}
        )
        await db_session.commit()
    await db_session.rollback()

    # An entirely separate, never-bridged legacy row is untouched by this trigger —
    # confirms the distinction is per-row, not table-wide.
    plain_rack_model = RackModel(manufacturer="Legacy Only Co", model_name="Never Bridged")
    db_session.add(plain_rack_model)
    await db_session.flush()
    plain_revision = RackModelRevision(rack_model_id=plain_rack_model.id, height_u=42, width_mm=600, depth_mm=1000)
    db_session.add(plain_revision)
    await db_session.commit()

    await db_session.execute(
        text("UPDATE rack_model_revision SET height_u = 44 WHERE id = :id"), {"id": str(plain_revision.id)}
    )
    await db_session.commit()
    await db_session.refresh(plain_revision)
    assert plain_revision.height_u == 44


# --------------------------------------------------------------------- Concurrency (parent lock)


async def test_concurrent_publish_and_child_insert_serialize_via_parent_lock(db_engine):
    """§5.4: "Publication must serialize with child edits by locking the parent row with
    SELECT ... FOR UPDATE before validation; each child-write trigger obtains the same
    parent row lock before checking draft status." Verified here with two genuinely
    separate connections/transactions, not two ORM sessions sharing one connection —
    the child-insert transaction blocks until the publish transaction commits or rolls
    back, and then sees a consistent, already-published state (never a torn read)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as setup_session:
        manufacturer = await _make_manufacturer(setup_session)
        model = await _make_model(setup_session, manufacturer.id)
        user = await _make_user(setup_session)
        revision = await _make_draft(setup_session, model.id, user.id)
        await setup_session.commit()
        revision_id = revision.id
        user_id = user.id

    conn_a = await db_engine.connect()
    conn_b = await db_engine.connect()
    try:
        await conn_a.execution_options(isolation_level="READ COMMITTED")
        await conn_b.execution_options(isolation_level="READ COMMITTED")

        # Transaction A: begin publishing — takes the FOR UPDATE lock on the revision row
        # (inherent to the UPDATE statement itself) but does not commit yet.
        trans_a = await conn_a.begin()
        await conn_a.execute(
            text(
                "UPDATE catalog_model_revision SET lifecycle_status = 'published', published_at = now(), "
                "published_by_user_id = :uid WHERE id = :id"
            ),
            {"uid": str(user_id), "id": str(revision_id)},
        )

        # Transaction B: attempt a concurrent child INSERT under the same (not-yet-
        # committed) revision. Its own trigger's `SELECT ... FOR UPDATE` on the revision
        # row must block until transaction A finishes, per the locking discipline above —
        # verified by running it in a background task and confirming it has NOT completed
        # after a short wait, then completing only once A commits.
        import asyncio

        trans_b = await conn_b.begin()
        child_insert = asyncio.create_task(
            conn_b.execute(
                text(
                    "INSERT INTO network_port_template "
                    "(id, catalog_model_revision_id, stable_key, display_name, media_type, "
                    "supported_speeds_mbps, connector_type, side) "
                    "VALUES (gen_random_uuid(), :rev, 'eth0', 'eth0', 'copper', '[1000]', 'rj45', 'front')"
                ),
                {"rev": str(revision_id)},
            )
        )
        await asyncio.sleep(0.3)
        assert not child_insert.done(), "child insert should still be blocked on the parent row lock"

        await trans_a.commit()

        # Now that A has committed (revision is 'published'), B's blocked INSERT
        # proceeds and must itself be rejected — not by the lock (now released) but by
        # the immutability check B's own trigger performs once it acquires the lock.
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="not a draft"):
            await child_insert
            await trans_b.commit()
        await trans_b.rollback()
    finally:
        await conn_a.close()
        await conn_b.close()
