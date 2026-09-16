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


async def test_audit_log_cannot_be_truncated_by_the_application_role(db_session):
    """Finding C1 regression (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
    PHASE1_CORRECTION_REPORT.md): TRUNCATE is a distinct privilege from DELETE and was
    not revoked by the original migration — empirically, `dcim_app` could wipe the entire
    audit log with one statement. Both migration 0003 (REVOKE TRUNCATE) and
    scripts/bootstrap_privileged_roles.sql (ownership transfer to dcim_retention_admin,
    which alone makes the REVOKE meaningful rather than bypassable via re-GRANT-to-self)
    must be applied for this to pass — see README.md's setup steps."""
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="permission denied"):
        await db_session.execute(text("TRUNCATE TABLE audit_log"))
        await db_session.commit()
    await db_session.rollback()


async def test_audit_log_cannot_be_altered_or_dropped_by_the_application_role(db_session):
    """Finding C1 regression: ALTER/DROP are inherent to table ownership in PostgreSQL
    and cannot be taken away by REVOKE while the application role remains the owner — the
    only correct fix is transferring ownership to dcim_retention_admin (done in
    scripts/bootstrap_privileged_roles.sql), which this test verifies actually took
    effect by attempting the same DDL the red-team report used to demonstrate the bug."""
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="must be owner"):
        await db_session.execute(text("ALTER TABLE audit_log ADD COLUMN backdoor text"))
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="must be owner"):
        await db_session.execute(text("ALTER TABLE audit_log RENAME TO audit_log_renamed"))
        await db_session.commit()
    await db_session.rollback()


async def test_audit_log_partition_cannot_be_dropped_by_the_application_role(db_session):
    """Finding C1 regression, deeper layer: found only after the table-ownership-transfer
    fix above was already in place and re-verified via a genuine dcim_app login
    connection (not a superuser session with SET ROLE, which can mask this). PostgreSQL
    grants the *database owner* an implicit right to DROP TABLE any table in that
    database regardless of that table's own ownership — so with dcim_app as the database
    owner (this repository's original setup: `CREATE DATABASE dcim OWNER dcim_app`),
    dcim_app could still drop audit_log's partitions even though audit_log itself was
    correctly owned by dcim_retention_admin. The fix is that dcim_app must not own the
    database either (see README.md, backend/scripts/docker-initdb/01-create-app-role.sh,
    .github/workflows/ci.yml, and the defensive reassignment in
    scripts/bootstrap_privileged_roles.sql)."""
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="must be owner"):
        await db_session.execute(text("DROP TABLE audit_log_default"))
        await db_session.commit()
    await db_session.rollback()


async def test_dcim_app_does_not_own_the_database(db_session):
    """Regression guard for the deeper C1 layer above: this must hold for the DROP-TABLE
    protection to mean anything, since a database-owner bypass would otherwise silently
    defeat every table-ownership-based fix in this file."""
    owner = (
        await db_session.execute(
            text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
        )
    ).scalar_one()
    assert owner != "dcim_app", "dcim_app must never own the database it connects to (Finding C1)"


async def test_audit_log_insert_and_select_still_work_for_the_application_role(db_session):
    """The Finding C1 fix must not be a blanket lockout — dcim_app still needs to append
    and read audit history for the application to function at all."""
    await db_session.execute(
        text(
            "INSERT INTO audit_log (audit_id, timestamp, action, entity_type, source, result) "
            "VALUES (gen_random_uuid(), now(), 'legit.write', 'test_entity', 'system', 'success')"
        )
    )
    await db_session.commit()

    count = (
        await db_session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'legit.write'"))
    ).scalar_one()
    assert count == 1


async def test_audit_log_partition_creation_still_works_through_the_scoped_function(db_session, _admin_engine):
    """Finding C1's fix removes dcim_app's direct ALTER rights on audit_log, which would
    otherwise also break the legitimate audit_partition_maintenance Celery task (creating
    a partition is, under the hood, an ALTER of the parent table). Verifies the
    dcim_create_audit_log_partition SECURITY DEFINER function (granted to dcim_app) still
    lets it happen without any direct ownership/ALTER right."""
    import uuid as _uuid

    partition_name = f"audit_log_test_{_uuid.uuid4().hex[:8]}"
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="invalid audit_log partition name"):
        # the function validates its own input — a name outside the expected pattern is
        # rejected even though it would otherwise be syntactically valid DDL.
        await db_session.execute(
            text("SELECT dcim_create_audit_log_partition(:name, '2099-01-01', '2099-02-01')"),
            {"name": partition_name},
        )
        await db_session.commit()
    await db_session.rollback()

    valid_name = "audit_log_2099_06"
    await db_session.execute(
        text("SELECT dcim_create_audit_log_partition(:name, '2099-06-01', '2099-07-01')"),
        {"name": valid_name},
    )
    await db_session.commit()
    owner = (
        await db_session.execute(
            text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = :name"), {"name": valid_name}
        )
    ).scalar_one_or_none()
    assert owner == "dcim_retention_admin", "the new partition must be owned by the privileged role, not dcim_app"

    # dcim_app cannot drop it (that's the point of the fix) — clean up via the
    # test-admin superuser connection instead.
    async with _admin_engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {valid_name}"))


async def test_managed_asset_cannot_replace_itself(db_session):
    """Finding M2 regression: replaces_asset_id previously had no constraint preventing
    an asset from claiming to replace itself."""
    asset = ManagedAsset(asset_type="rack", asset_tag="SELF-REPL-TEST")
    db_session.add(asset)
    await db_session.flush()

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.execute(
            text("UPDATE managed_asset SET replaces_asset_id = id WHERE id = :id"), {"id": asset.id}
        )
    await db_session.rollback()


async def test_managed_asset_replacement_cannot_form_a_two_cycle(db_session):
    """Finding M3 regression: A replaces B, then B replaces A previously succeeded,
    creating a mutual cycle with no valid interpretation."""
    a = ManagedAsset(asset_type="rack", asset_tag="CYCLE-A")
    b = ManagedAsset(asset_type="rack", asset_tag="CYCLE-B")
    db_session.add_all([a, b])
    await db_session.flush()

    await db_session.execute(text("UPDATE managed_asset SET replaces_asset_id = :b WHERE id = :a"), {"a": a.id, "b": b.id})
    await db_session.commit()

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.execute(
            text("UPDATE managed_asset SET replaces_asset_id = :a WHERE id = :b"), {"a": a.id, "b": b.id}
        )
    await db_session.rollback()


async def test_managed_asset_replacement_cannot_form_a_longer_cycle(db_session):
    """Finding M3 regression, extended: A->B->C->A must also be rejected, not just the
    direct 2-cycle case."""
    a = ManagedAsset(asset_type="rack", asset_tag="TRI-A")
    b = ManagedAsset(asset_type="rack", asset_tag="TRI-B")
    c = ManagedAsset(asset_type="rack", asset_tag="TRI-C")
    db_session.add_all([a, b, c])
    await db_session.flush()

    await db_session.execute(text("UPDATE managed_asset SET replaces_asset_id = :a WHERE id = :b"), {"a": a.id, "b": b.id})
    await db_session.commit()
    await db_session.execute(text("UPDATE managed_asset SET replaces_asset_id = :b WHERE id = :c"), {"b": b.id, "c": c.id})
    await db_session.commit()

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await db_session.execute(
            text("UPDATE managed_asset SET replaces_asset_id = :c WHERE id = :a"), {"c": c.id, "a": a.id}
        )
    await db_session.rollback()


async def test_managed_asset_non_cyclic_replacement_chain_still_works(db_session):
    """A legitimate, non-cyclic replacement chain (A replaced by B replaced by C) must
    not be rejected by the M3 cycle-prevention trigger."""
    a = ManagedAsset(asset_type="rack", asset_tag="CHAIN-A")
    db_session.add(a)
    await db_session.flush()

    b = ManagedAsset(asset_type="rack", asset_tag="CHAIN-B", replaces_asset_id=a.id)
    db_session.add(b)
    await db_session.commit()

    c = ManagedAsset(asset_type="rack", asset_tag="CHAIN-C", replaces_asset_id=b.id)
    db_session.add(c)
    await db_session.commit()

    assert c.replaces_asset_id == b.id
