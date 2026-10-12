"""Issue #145: `app.db.models` must register every ORM model on `Base.metadata`.

Alembic resolves FK targets from that registry at autogenerate time; when
`app.domain.integration.models` was omitted, `alembic check` failed with
`NoReferencedTableError` (`alarm.integration_id -> integration.id`) even though the
database was fully migrated and healthy. These tests are deterministic and need no
database: they walk every `app.domain` model module and fail if importing
`app.db.models` alone leaves any table (or any FK target) unregistered.
"""

import importlib
import pkgutil

import app.db.models  # noqa: F401 — the registry under test
import app.domain
from app.db.base import Base


def _model_module_names() -> list[str]:
    names = []
    for mod in pkgutil.walk_packages(app.domain.__path__, "app.domain."):
        leaf = mod.name.rsplit(".", 1)[-1]
        if leaf == "models" or leaf.endswith("_models") or leaf == "ports":
            names.append(mod.name)
    return sorted(names)


def test_every_domain_model_table_is_registered_by_app_db_models():
    registered = set(Base.metadata.tables)
    missed: dict[str, list[str]] = {}
    for name in _model_module_names():
        importlib.import_module(name)
        new_tables = set(Base.metadata.tables) - registered
        if new_tables:
            missed[name] = sorted(new_tables)
            registered |= new_tables
    assert not missed, (
        "model modules whose tables app.db.models does not register "
        f"(add the import there): {missed}"
    )


def test_every_foreign_key_target_resolves():
    # Accessing fk.column forces resolution and raises NoReferencedTableError when the
    # target table is not registered — exactly the failure Alembic hit in Issue #145.
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column is not None
