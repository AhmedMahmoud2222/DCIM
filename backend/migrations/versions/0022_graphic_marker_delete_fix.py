"""Phase 10A PR-5 correction: fn_validate_catalog_graphic_marker (migration
0019_catalog_graphics) unconditionally looks up its parent catalog_graphic row before
doing anything else, even for TG_OP = 'DELETE'. That is correct for a *standalone*
marker delete (the parent graphic still exists) but always fails for a marker delete
that is itself a cascade from its parent catalog_graphic being deleted
(catalog_graphic_marker.catalog_graphic_id is ON DELETE CASCADE): PostgreSQL's
referential-action cascade fires the child's row trigger after the parent row is already
gone, so `SELECT ... FROM catalog_graphic WHERE id = v_graphic_id` finds nothing and the
trigger raises 'parent catalog_graphic missing' -- rejecting the very cascade delete the
FK itself is supposed to perform automatically.

This went unnoticed until now because nothing before Phase 10A PR-5 ever deleted a
catalog_graphic row (PR-3's clone_revision explicitly refused to clone a revision with
graphics, and no upload endpoint existed yet to create one in the first place -- see that
function's own docstring).

Fix: return immediately for TG_OP = 'DELETE', before the parent-lookup block, instead of
after it. This does not weaken enforcement:
- A standalone marker delete (graphic still exists) is already gated by the API layer's
  `lock_draft_revision_for_edit` (app/api/v1/catalog_designer.py), which re-checks
  `lifecycle_status == 'draft'` before ever reaching the database -- the same
  defense-in-depth relationship this router's own module docstring describes for every
  other mutation.
- A cascade delete (graphic itself being deleted) was already gated by
  `trg_catalog_graphic_immutable` on catalog_graphic's own BEFORE DELETE, which enforces
  the identical "revision must still be draft" invariant before the cascade can ever
  begin.
- INSERT and UPDATE are completely unaffected: the parent-existence/draft-status/
  cross-revision-FK checks all still run exactly as before for those operations.

Revision ID: 0022_graphic_marker_delete_fix
Revises: 0021_catalog_rbac_seed
Create Date: 2026-09-24
"""

from alembic import op

revision = "0022_graphic_marker_delete_fix"
down_revision = "0021_catalog_rbac_seed"
branch_labels = None
depends_on = None

_NEW_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION fn_validate_catalog_graphic_marker() RETURNS trigger AS $$
DECLARE
  v_graphic_id UUID;
  v_revision_id UUID;
  v_status TEXT;
  v_port_revision_id UUID;
  v_psu_revision_id UUID;
BEGIN
  IF TG_OP = 'DELETE' THEN
    -- Never re-validated here: a standalone delete is already gated by the API layer's
    -- lock_draft_revision_for_edit, and a cascade delete (this marker's own
    -- catalog_graphic being deleted) was already gated by that delete's own
    -- trg_catalog_graphic_immutable check -- by the time a cascade reaches this row,
    -- the parent catalog_graphic is expected to be gone, not an error condition.
    RETURN OLD;
  END IF;

  v_graphic_id := NEW.catalog_graphic_id;
  IF TG_OP = 'UPDATE' AND NEW.catalog_graphic_id <> OLD.catalog_graphic_id THEN
    RAISE EXCEPTION 'moving a marker between graphics is forbidden';
  END IF;

  SELECT catalog_model_revision_id INTO v_revision_id FROM catalog_graphic WHERE id = v_graphic_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'parent catalog_graphic missing'; END IF;

  SELECT lifecycle_status INTO v_status FROM catalog_model_revision WHERE id = v_revision_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'parent revision missing'; END IF;
  IF v_status <> 'draft' THEN
    RAISE EXCEPTION 'catalog_model_revision % is not a draft (status=%): markers are immutable', v_revision_id, v_status
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF NEW.network_port_template_id IS NOT NULL THEN
    SELECT catalog_model_revision_id INTO v_port_revision_id
      FROM network_port_template WHERE id = NEW.network_port_template_id;
    IF v_port_revision_id IS DISTINCT FROM v_revision_id THEN
      RAISE EXCEPTION 'marker network_port_template_id % belongs to a different revision than its graphic',
        NEW.network_port_template_id
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  IF NEW.power_supply_template_id IS NOT NULL THEN
    SELECT catalog_model_revision_id INTO v_psu_revision_id
      FROM power_supply_template WHERE id = NEW.power_supply_template_id;
    IF v_psu_revision_id IS DISTINCT FROM v_revision_id THEN
      RAISE EXCEPTION 'marker power_supply_template_id % belongs to a different revision than its graphic',
        NEW.power_supply_template_id
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_OLD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION fn_validate_catalog_graphic_marker() RETURNS trigger AS $$
DECLARE
  v_graphic_id UUID;
  v_revision_id UUID;
  v_status TEXT;
  v_port_revision_id UUID;
  v_psu_revision_id UUID;
BEGIN
  v_graphic_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.catalog_graphic_id ELSE NEW.catalog_graphic_id END;
  IF TG_OP = 'UPDATE' AND NEW.catalog_graphic_id <> OLD.catalog_graphic_id THEN
    RAISE EXCEPTION 'moving a marker between graphics is forbidden';
  END IF;

  SELECT catalog_model_revision_id INTO v_revision_id FROM catalog_graphic WHERE id = v_graphic_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'parent catalog_graphic missing'; END IF;

  SELECT lifecycle_status INTO v_status FROM catalog_model_revision WHERE id = v_revision_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'parent revision missing'; END IF;
  IF v_status <> 'draft' THEN
    RAISE EXCEPTION 'catalog_model_revision % is not a draft (status=%): markers are immutable', v_revision_id, v_status
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;

  IF NEW.network_port_template_id IS NOT NULL THEN
    SELECT catalog_model_revision_id INTO v_port_revision_id
      FROM network_port_template WHERE id = NEW.network_port_template_id;
    IF v_port_revision_id IS DISTINCT FROM v_revision_id THEN
      RAISE EXCEPTION 'marker network_port_template_id % belongs to a different revision than its graphic',
        NEW.network_port_template_id
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  IF NEW.power_supply_template_id IS NOT NULL THEN
    SELECT catalog_model_revision_id INTO v_psu_revision_id
      FROM power_supply_template WHERE id = NEW.power_supply_template_id;
    IF v_psu_revision_id IS DISTINCT FROM v_revision_id THEN
      RAISE EXCEPTION 'marker power_supply_template_id % belongs to a different revision than its graphic',
        NEW.power_supply_template_id
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_NEW_FUNCTION_SQL)


def downgrade() -> None:
    op.execute(_OLD_FUNCTION_SQL)
