"""Issue #105 (review blocker B5): the rectangle CHECK on `thermal_zone` must not pass on NULL dimensions.

PostgreSQL accepts a row when a CHECK evaluates to NULL, and `width_mm > 0` is NULL when width_mm is NULL. The ORM
constraint and the (unmerged, therefore amended) migration must therefore both state `IS NOT NULL` explicitly and must
carry the same predicate. The raw-SQL attacks against the migrated schema live in
tests/integration/test_cooling_thermal_migration.py."""

import re
from pathlib import Path

from sqlalchemy import CheckConstraint

from app.domain.cooling.models import ThermalZone

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0045_cooling_thermal.py"


def orm_predicate() -> str:
    constraint = next(c for c in ThermalZone.__table__.constraints if isinstance(c, CheckConstraint) and c.name and c.name.endswith("geometry_shape_consistent"))
    return str(constraint.sqltext)


def migration_predicate() -> str:
    source = MIGRATION.read_text()
    block = source[source.index('name=op.f("ck_thermal_zone_geometry_shape_consistent")') - 900 : source.index('name=op.f("ck_thermal_zone_geometry_shape_consistent")')]
    pieces = re.findall(r'"([^"\n]*)"', block)
    return "".join(pieces[-3:]) if len(pieces) >= 3 else ""


def test_every_mandatory_rectangle_component_is_explicitly_not_null_in_the_orm():
    predicate = orm_predicate()
    for column in ("x_mm", "y_mm", "width_mm", "height_mm"):
        assert f"{column} IS NOT NULL" in predicate, column
    assert "width_mm > 0" in predicate and "height_mm > 0" in predicate


def test_the_migration_and_the_orm_state_the_same_predicate():
    assert " ".join(migration_predicate().split()) == " ".join(orm_predicate().split())
