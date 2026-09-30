"""DCIM01 PDF datasheet import, PR-A: `catalog_document` and `catalog_revision_document`,
plus the `catalog:document_download` permission
(docs/plans/DCIM01_PDF_DATASHEET_IMPORT_PLAN_v2.md sections 4.2, 4.7 and 9).

Additive only: no existing catalog, physical-measurement or legacy-bridge column is touched
(the database unit audit is unresolved).

Datasheet versioning: a revised datasheet is a new `catalog_document` row in the same
`document_group_id` with `version_number + 1`. `catalog_document` rows are immutable except
for the one-time NULL -> value assignment of `catalog_model_id` (a staged upload gets its
model when it is first attached). `catalog_revision_document` reuses migration 0017's
`fn_reject_write_on_non_draft_revision()`, so links on a published or retired revision cannot
be inserted, changed or removed: a newer datasheet never alters an existing published
revision or the assets instantiated from it. A second trigger requires the linked document
to belong to the revision's own model and to have passed scanning.

Revision ID: 0031_catalog_documents
Revises: 0030_bulk_import_attempts
Create Date: 2026-09-29
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0031_catalog_documents"
down_revision = "0030_bulk_import_attempts"
branch_labels = None
depends_on = None

_PERMISSION = ("catalog", "document_download")
_GRANTED_ROLES = ("Administrator", "DCIM Manager", "Engineer")


def upgrade() -> None:
    op.create_table(
        "catalog_document",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_group_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_document_id", sa.Uuid(), nullable=True),
        sa.Column("catalog_model_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), server_default="datasheet", nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=32), server_default="application/pdf", nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("scan_status", sa.String(length=16), nullable=False),
        sa.Column("scan_engine", sa.String(length=64), nullable=True),
        sa.Column("scan_detail", sa.String(length=128), nullable=True),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('datasheet')", name=op.f("ck_catalog_document_kind_allowed")),
        sa.CheckConstraint(
            "scan_status IN ('clean', 'skipped', 'infected', 'error')", name=op.f("ck_catalog_document_scan_status_allowed")
        ),
        sa.CheckConstraint("mime_type = 'application/pdf'", name=op.f("ck_catalog_document_mime_type_pdf")),
        sa.CheckConstraint("version_number >= 1", name=op.f("ck_catalog_document_version_number_positive")),
        sa.CheckConstraint("page_count >= 1", name=op.f("ck_catalog_document_page_count_positive")),
        sa.CheckConstraint("file_size_bytes > 0", name=op.f("ck_catalog_document_file_size_positive")),
        sa.CheckConstraint("char_length(sha256) = 64", name=op.f("ck_catalog_document_sha256_length")),
        sa.CheckConstraint(
            "supersedes_document_id IS NULL OR version_number > 1",
            name=op.f("ck_catalog_document_supersedes_requires_later_version"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_document_id"], ["catalog_document.id"],
            name=op.f("fk_catalog_document_supersedes_document_id_catalog_document"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_id"], ["catalog_model.id"],
            name=op.f("fk_catalog_document_catalog_model_id_catalog_model"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["app_user.id"],
            name=op.f("fk_catalog_document_uploaded_by_user_id_app_user"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_document")),
        sa.UniqueConstraint("document_group_id", "version_number", name="uq_catalog_document_group_version"),
        sa.UniqueConstraint("supersedes_document_id", name=op.f("uq_catalog_document_supersedes_document_id")),
    )
    op.create_index("ix_catalog_document_document_group_id", "catalog_document", ["document_group_id"])
    op.create_index("ix_catalog_document_catalog_model_id", "catalog_document", ["catalog_model_id"])
    op.create_index(
        "uq_catalog_document_model_sha256", "catalog_document", ["catalog_model_id", "sha256"], unique=True,
        postgresql_where=sa.text("catalog_model_id IS NOT NULL"),
    )

    op.create_table(
        "catalog_revision_document",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_document_id", sa.Uuid(), nullable=False),
        sa.Column("attached_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("attached_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_model_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_catalog_revision_document_catalog_model_revision_id_catalog_model_revision"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_document_id"], ["catalog_document.id"],
            name=op.f("fk_catalog_revision_document_catalog_document_id_catalog_document"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attached_by_user_id"], ["app_user.id"],
            name=op.f("fk_catalog_revision_document_attached_by_user_id_app_user"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_revision_document")),
        sa.UniqueConstraint(
            "catalog_model_revision_id", "catalog_document_id", name="uq_catalog_revision_document_pair"
        ),
    )
    op.create_index(
        "ix_catalog_revision_document_catalog_model_revision_id", "catalog_revision_document", ["catalog_model_revision_id"]
    )
    op.create_index("ix_catalog_revision_document_catalog_document_id", "catalog_revision_document", ["catalog_document_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_guard_catalog_document() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - 'catalog_model_id' - 'updated_at') IS DISTINCT FROM
             (to_jsonb(OLD) - 'catalog_model_id' - 'updated_at') THEN
            RAISE EXCEPTION 'catalog_document % is immutable', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF NEW.catalog_model_id IS DISTINCT FROM OLD.catalog_model_id AND OLD.catalog_model_id IS NOT NULL THEN
            RAISE EXCEPTION 'catalog_document % is already assigned to a catalog model', OLD.id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_document_immutable BEFORE UPDATE ON catalog_document "
        "FOR EACH ROW EXECUTE FUNCTION fn_guard_catalog_document();"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_validate_catalog_revision_document() RETURNS trigger AS $$
        DECLARE
          v_revision_model UUID;
          v_document_model UUID;
          v_scan_status TEXT;
        BEGIN
          SELECT catalog_model_id INTO v_revision_model FROM catalog_model_revision
            WHERE id = NEW.catalog_model_revision_id;
          SELECT catalog_model_id, scan_status INTO v_document_model, v_scan_status FROM catalog_document
            WHERE id = NEW.catalog_document_id;
          IF v_document_model IS DISTINCT FROM v_revision_model THEN
            RAISE EXCEPTION 'catalog_document % does not belong to the catalog model of revision %',
              NEW.catalog_document_id, NEW.catalog_model_revision_id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF v_scan_status NOT IN ('clean', 'skipped') THEN
            RAISE EXCEPTION 'catalog_document % has not passed malware scanning', NEW.catalog_document_id
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_revision_document_immutable BEFORE INSERT OR UPDATE OR DELETE ON catalog_revision_document "
        "FOR EACH ROW EXECUTE FUNCTION fn_reject_write_on_non_draft_revision();"
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_revision_document_validate BEFORE INSERT OR UPDATE ON catalog_revision_document "
        "FOR EACH ROW EXECUTE FUNCTION fn_validate_catalog_revision_document();"
    )

    permission_table = sa.table(
        "permission", sa.column("id", UUID), sa.column("resource", sa.String), sa.column("action", sa.String),
        sa.column("description", sa.String),
    )
    role_permission_table = sa.table("role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID))
    role_table = sa.table("role", sa.column("id", UUID), sa.column("name", sa.String))
    resource, action = _PERMISSION
    permission_id = _uuid.uuid4()
    op.bulk_insert(
        permission_table,
        [{"id": permission_id, "resource": resource, "action": action, "description": f"{action} on {resource}"}],
    )
    connection = op.get_bind()
    role_ids = {
        name: role_id
        for name, role_id in connection.execute(sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(_GRANTED_ROLES)))
    }
    missing = set(_GRANTED_ROLES) - set(role_ids)
    if missing:
        raise RuntimeError(f"catalog document permission seed expects roles {sorted(_GRANTED_ROLES)}; missing: {sorted(missing)}")
    op.bulk_insert(
        role_permission_table, [{"role_id": role_ids[name], "permission_id": permission_id} for name in _GRANTED_ROLES]
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM role_permission WHERE permission_id IN "
            "(SELECT id FROM permission WHERE resource = 'catalog' AND action = 'document_download')"
        )
    )
    connection.execute(sa.text("DELETE FROM permission WHERE resource = 'catalog' AND action = 'document_download'"))
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_revision_document_validate ON catalog_revision_document")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_revision_document_immutable ON catalog_revision_document")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_catalog_revision_document()")
    op.drop_table("catalog_revision_document")
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_document_immutable ON catalog_document")
    op.execute("DROP FUNCTION IF EXISTS fn_guard_catalog_document()")
    op.drop_table("catalog_document")
