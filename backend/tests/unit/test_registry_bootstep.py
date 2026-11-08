"""Worker bootstep propagation and private-engine cleanup without PostgreSQL."""

from unittest.mock import AsyncMock

import pytest
from celery.bootsteps import symbol_by_name
from celery.worker.components import Pool
from sqlalchemy.pool import NullPool

from app.infrastructure import registry_bootstep
from app.infrastructure.celery_app import celery_app


@pytest.mark.parametrize("failure", [None, RuntimeError("missing ledger"), RuntimeError("snapshot mismatch")])
def test_bootstep_checks_before_consumption_and_disposes(monkeypatch, failure):
    engine = type("Engine", (), {"dispose": AsyncMock()})()
    created = []

    def create_engine(url, **kwargs):
        created.append((url, kwargs))
        return engine

    validator = AsyncMock(side_effect=failure)
    monkeypatch.setattr(registry_bootstep, "create_async_engine", create_engine)
    monkeypatch.setattr(registry_bootstep, "verify_registry_ledger", validator)
    step = registry_bootstep.RegistryLedgerGate(None)
    if failure is None:
        step.start(None)
    else:
        with pytest.raises(RuntimeError, match=str(failure)):
            step.start(None)
    validator.assert_awaited_once_with(engine)
    engine.dispose.assert_awaited_once()
    assert created == [(registry_bootstep.get_settings().database_url, {"poolclass": NullPool})]


def test_gate_is_registered_as_worker_bootstep():
    assert registry_bootstep.RegistryLedgerGate in celery_app.steps["worker"]
    # Celery's Blueprint._firstpass() resolves `requires` in place on the
    # bootstep class itself, so once any test in the process boots a worker
    # blueprint, RegistryLedgerGate.requires is permanently rewritten from the
    # declared "module:Class" string to the resolved step class. Resolving
    # both sides through symbol_by_name keeps the assertion correct
    # regardless of test ordering, instead of pinning one string spelling.
    resolved = [symbol_by_name(r) if isinstance(r, str) else r for r in registry_bootstep.RegistryLedgerGate.requires]
    assert Pool in resolved
