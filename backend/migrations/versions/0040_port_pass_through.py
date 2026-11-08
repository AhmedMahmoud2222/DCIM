"""Issue #101: authoritative pass-through relationships (patch-panel front <-> rear).

Additive only. Existing cables, port connections and ports are neither read nor changed; the
only change to an existing table is a unique index on `equipment_port (id, equipment_id)`, which
exists so the new tables can reference it with a composite foreign key. Downgrade refuses to
discard recorded pass-throughs.

Revision ID: 0040_port_pass_through
Revises: 0039_cables
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0040_port_pass_through"
down_revision = "0039_cables"
branch_labels = None
depends_on = None

_TWO_MEMBERS = """
CREATE FUNCTION port_pass_through_require_two_members() RETURNS trigger AS $$
DECLARE
    target uuid;
    total int;
BEGIN
    IF TG_TABLE_NAME = 'port_pass_through' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.pass_through_id;
    ELSE
        target := NEW.pass_through_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM port_pass_through WHERE id = target) THEN
        RETURN NULL;  -- the pass-through itself was deleted in this transaction
    END IF;
    SELECT count(*) INTO total FROM port_pass_through_member WHERE pass_through_id = target;
    IF total <> 2 THEN
        RAISE EXCEPTION 'pass-through % must join exactly two ports', target USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_IMMUTABLE = """
CREATE FUNCTION port_pass_through_member_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.pass_through_id <> OLD.pass_through_id OR NEW.equipment_port_id <> OLD.equipment_port_id
       OR NEW.equipment_id <> OLD.equipment_id THEN
        RAISE EXCEPTION 'pass-through members cannot be re-pointed; delete the pass-through and record a new one'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_unique_constraint("uq_equipment_port_id_equipment_id", "equipment_port", ["id", "equipment_id"])
    op.create_table(
        "port_pass_through",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("equipment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(64)),
        sa.Column("created_by_user_id", UUID(as_uuid=True)),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_port_pass_through"),
        sa.UniqueConstraint("id", "equipment_id", name="uq_port_pass_through_id_equipment_id"),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"], name="fk_port_pass_through_equipment_id_equipment", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["app_user.id"], name="fk_port_pass_through_created_by_user_id_app_user", ondelete="SET NULL"
        ),
        sa.CheckConstraint("label IS NULL OR btrim(label) <> ''", name="label_not_blank"),
    )
    op.create_index("ix_port_pass_through_equipment_id", "port_pass_through", ["equipment_id"])
    op.create_table(
        "port_pass_through_member",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("pass_through_id", UUID(as_uuid=True), nullable=False),
        sa.Column("equipment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("equipment_port_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_port_pass_through_member"),
        sa.ForeignKeyConstraint(
            ["pass_through_id", "equipment_id"], ["port_pass_through.id", "port_pass_through.equipment_id"],
            name="fk_port_pass_through_member_pass_through_same_equipment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_port_id", "equipment_id"], ["equipment_port.id", "equipment_port.equipment_id"],
            name="fk_port_pass_through_member_port_same_equipment", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("equipment_port_id", name="uq_port_pass_through_member_port"),
        sa.UniqueConstraint("pass_through_id", "equipment_port_id", name="uq_port_pass_through_member_pair_port"),
    )
    op.execute(_TWO_MEMBERS)
    op.execute(_IMMUTABLE)
    op.execute(
        "CREATE CONSTRAINT TRIGGER port_pass_through_two_members AFTER INSERT ON port_pass_through "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION port_pass_through_require_two_members()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER port_pass_through_member_two_members AFTER INSERT OR UPDATE OR DELETE "
        "ON port_pass_through_member DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION port_pass_through_require_two_members()"
    )
    op.execute(
        "CREATE TRIGGER port_pass_through_member_immutable BEFORE UPDATE ON port_pass_through_member "
        "FOR EACH ROW EXECUTE FUNCTION port_pass_through_member_immutable()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM port_pass_through")):
        raise RuntimeError(
            "Refusing to drop pass-through relationships: recorded patch-panel topology exists. "
            "Export it deliberately before downgrading."
        )
    op.execute("DROP TRIGGER port_pass_through_member_immutable ON port_pass_through_member")
    op.execute("DROP TRIGGER port_pass_through_member_two_members ON port_pass_through_member")
    op.execute("DROP TRIGGER port_pass_through_two_members ON port_pass_through")
    op.drop_table("port_pass_through_member")
    op.drop_table("port_pass_through")
    op.execute("DROP FUNCTION port_pass_through_member_immutable()")
    op.execute("DROP FUNCTION port_pass_through_require_two_members()")
    op.drop_constraint("uq_equipment_port_id_equipment_id", "equipment_port", type_="unique")
