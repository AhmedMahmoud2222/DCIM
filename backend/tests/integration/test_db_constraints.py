"""Runs against a real PostgreSQL 16 instance (see conftest.py) — these tests verify the
database itself enforces the invariants, not just application code (§30 of the Phase 1
prompt: "an invariant should not exist only in prose when the database can enforce it")."""

import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.domain.identity.models import ManagedAsset
from app.domain.location.models import Country


async def test_managed_asset_tag_uniqueness_is_db_enforced(db_session):
    db_session.add(ManagedAsset(asset_type="rack", asset_tag="RACK-DUP"))
    await db_session.commit()

    db_session.add(ManagedAsset(asset_type="rack", asset_tag="RACK-DUP"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_managed_asset_type_must_be_in_allowed_set(db_session):
    db_session.add(ManagedAsset(asset_type="not-a-real-type", asset_tag="X-1"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_country_requires_existing_organization(db_session):
    db_session.add(Country(organization_id=uuid.uuid4(), name="Nowhere"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_asset_replacement_identity_never_collides(db_session):
    """C1's invariant: replacing an asset creates a new ManagedAsset.id, and at most one
    new asset may claim to replace a given old one (uq_managed_asset_replaces_asset_id)."""
    old = ManagedAsset(asset_type="rack", asset_tag="RACK-OLD")
    db_session.add(old)
    await db_session.flush()

    new_1 = ManagedAsset(asset_type="rack", asset_tag="RACK-NEW-1", replaces_asset_id=old.id)
    db_session.add(new_1)
    await db_session.commit()
    assert new_1.id != old.id

    new_2 = ManagedAsset(asset_type="rack", asset_tag="RACK-NEW-2", replaces_asset_id=old.id)
    db_session.add(new_2)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.commit()


async def test_audit_log_is_append_only_at_the_database_level(db_session):
    """§30/§30a: the application's own DB role must not be able to UPDATE or DELETE
    audit_log, even via a raw statement that bypasses the ORM entirely."""
    await db_session.execute(
        text(
            "INSERT INTO audit_log (audit_id, timestamp, action, entity_type, source, result) "
            "VALUES (gen_random_uuid(), now(), 'test.action', 'test_entity', 'system', 'success')"
        )
    )
    await db_session.commit()

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.execute(text("DELETE FROM audit_log WHERE action = 'test.action'"))
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.execute(text("UPDATE audit_log SET action = 'tampered' WHERE action = 'test.action'"))
        await db_session.commit()
    await db_session.rollback()
