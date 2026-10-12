"""Worker bootstep propagation and private-engine cleanup without PostgreSQL."""

from unittest.mock import AsyncMock

import pytest
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
    required_steps = {step if isinstance(step, str) else step.name for step in registry_bootstep.RegistryLedgerGate.requires}
    assert "celery.worker.components:Pool" in required_steps
