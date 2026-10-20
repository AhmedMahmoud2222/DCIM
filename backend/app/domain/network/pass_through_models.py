"""Issue #101: authoritative pass-through relationships inside one piece of equipment.

A patch panel (or ODF, media converter, ...) does not terminate a signal: it passes it from
one of its ports to another (front 01 <-> rear 01). That fact is stored here, as a queryable
row, not in free-form cable metadata, so the trace engine can follow a path through any
number of such devices and the database can refuse inconsistent data.

* `PortPassThrough` - the relationship; it belongs to exactly one `Equipment`.
* `PortPassThroughMember` - its two ports. Modelled as rows so PostgreSQL can enforce, with
  an ordinary unique constraint, that a port belongs to at most one pass-through (a pair
  stored as two columns cannot express that across both columns).

Integrity enforced by the database:

* both member ports belong to the pass-through's equipment (composite foreign keys);
* a port is in at most one pass-through (`uq_port_pass_through_member_port`);
* a deferred constraint trigger requires exactly two members at commit;
* members cannot be re-pointed.

Cables and `PortConnection` are untouched: a pass-through only says how a signal continues
*inside* a device, between two ports that each keep their own cable.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class PortPassThrough(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "port_pass_through"
    __table_args__ = (
        UniqueConstraint("id", "equipment_id", name="uq_port_pass_through_id_equipment_id"),
        CheckConstraint("label IS NULL OR btrim(label) <> ''", name="label_not_blank"),
    )

    equipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PortPassThroughMember(Base, UUIDPkMixin):
    __tablename__ = "port_pass_through_member"
    __table_args__ = (
        ForeignKeyConstraint(
            ["pass_through_id", "equipment_id"], ["port_pass_through.id", "port_pass_through.equipment_id"],
            name="fk_port_pass_through_member_pass_through_same_equipment", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["equipment_port_id", "equipment_id"], ["equipment_port.id", "equipment_port.equipment_id"],
            name="fk_port_pass_through_member_port_same_equipment", ondelete="CASCADE",
        ),
        UniqueConstraint("equipment_port_id", name="uq_port_pass_through_member_port"),
        UniqueConstraint("pass_through_id", "equipment_port_id", name="uq_port_pass_through_member_pair_port"),
    )

    pass_through_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    equipment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    equipment_port_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
