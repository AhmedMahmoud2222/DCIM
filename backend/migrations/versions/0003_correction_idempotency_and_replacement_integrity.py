"""Phase 1 targeted correction: idempotency claim/completion state (Finding H1),
managed_asset replacement self-reference + cycle prevention (Findings M2/M3), and
defense-in-depth TRUNCATE revocation on audit_log (Finding C1 — the authoritative fix,
ownership transfer, lives in scripts/bootstrap_privileged_roles.sql since dcim_app,
which runs this migration, cannot re-own a table to a role it isn't a member of).

See PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md and PHASE1_CORRECTION_REPORT.md.

Revision ID: 0003_correction
Revises: 0002_seed
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_correction"
down_revision = "0002_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Finding H1: idempotency claim state --------------------------------------
    op.add_column(
        "idempotency_key",
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
    )
    op.create_check_constraint(
        "ck_idempotency_key_status_allowed", "idempotency_key", "status IN ('processing', 'completed')"
    )
    op.alter_column("idempotency_key", "response_status", nullable=True)
    op.alter_column("idempotency_key", "response_body", nullable=True)

    # --- Findings M2/M3: managed_asset replacement integrity ----------------------
    op.create_check_constraint(
        "ck_managed_asset_no_self_replacement",
        "managed_asset",
        "replaces_asset_id IS NULL OR replaces_asset_id != id",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_managed_asset_replacement_acyclic() RETURNS trigger AS $$
        DECLARE
            cycle_found boolean;
        BEGIN
            IF NEW.replaces_asset_id IS NULL THEN
                RETURN NEW;
            END IF;

            IF NEW.replaces_asset_id = NEW.id THEN
                RAISE EXCEPTION 'managed_asset % cannot replace itself', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;

            -- Walk the replacement chain starting at the asset NEW claims to replace;
            -- if NEW.id appears anywhere in that chain, linking NEW would close a cycle
            -- (direct 2-cycle A<->B, or any longer chain, e.g. A->B->C->A).
            WITH RECURSIVE chain AS (
                SELECT id, replaces_asset_id FROM managed_asset WHERE id = NEW.replaces_asset_id
                UNION ALL
                SELECT m.id, m.replaces_asset_id
                FROM managed_asset m
                JOIN chain c ON m.id = c.replaces_asset_id
            )
            SELECT EXISTS (SELECT 1 FROM chain WHERE id = NEW.id) INTO cycle_found;

            IF cycle_found THEN
                RAISE EXCEPTION 'managed_asset % replacement would create a cycle', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_managed_asset_replacement_acyclic
        BEFORE INSERT OR UPDATE OF replaces_asset_id ON managed_asset
        FOR EACH ROW EXECUTE FUNCTION check_managed_asset_replacement_acyclic();
        """
    )

    # --- Finding C1: defense-in-depth ----------------------------------------------
    # The authoritative fix (ownership transfer to dcim_retention_admin, which alone
    # closes ALTER/DROP — inherent to ownership and not revocable this way) lives in
    # scripts/bootstrap_privileged_roles.sql, run separately by a superuser after this
    # migration. This REVOKE is effective immediately for TRUNCATE specifically (unlike
    # ALTER/DROP, TRUNCATE is an ordinary grantable ACL privilege that Postgres does
    # honor a REVOKE of even against the current owner — empirically verified during
    # this correction), and remains harmless/idempotent once ownership is transferred.
    op.execute("REVOKE TRUNCATE ON audit_log FROM dcim_app")
    op.execute("REVOKE TRUNCATE ON audit_log FROM PUBLIC")


def downgrade() -> None:
    # NOTE: if scripts/bootstrap_privileged_roles.sql has already run in this
    # environment, audit_log is owned by dcim_retention_admin, not dcim_app — the GRANT
    # below (attempted as dcim_app, which runs Alembic) will then fail with "must be
    # owner of relation audit_log". A superuser must run
    # `ALTER TABLE audit_log OWNER TO dcim_app;` (and each partition) first. This mirrors
    # the pre-existing, documented limitation that dcim_retention_admin's own creation is
    # likewise outside Alembic's downgrade path (PHASE1_IMPLEMENTATION.md §14).
    op.execute("GRANT TRUNCATE ON audit_log TO dcim_app")

    op.execute("DROP TRIGGER IF EXISTS trg_managed_asset_replacement_acyclic ON managed_asset")
    op.execute("DROP FUNCTION IF EXISTS check_managed_asset_replacement_acyclic()")
    op.drop_constraint("ck_managed_asset_no_self_replacement", "managed_asset", type_="check")

    op.alter_column("idempotency_key", "response_body", nullable=False)
    op.alter_column("idempotency_key", "response_status", nullable=False)
    op.drop_constraint("ck_idempotency_key_status_allowed", "idempotency_key", type_="check")
    op.drop_column("idempotency_key", "status")
