"""Isolated ASGI route tests; no auth/database fixtures or external services."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.api.v1.alarms import router
from app.application.rbac import get_auth_context
from app.core.errors import register_exception_handlers
from app.domain.alarm.models import AlarmRule


async def request(method, path, *, body=None, permissions=frozenset({"alarm:read", "alarm:manage"}), row=None):
    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    ctx = SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()), has_permission=lambda code: code in permissions)
    ctx.for_permission = lambda code: ctx
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value.all.return_value = [row] if row else []
    result.scalar_one.return_value = 3
    db.execute.return_value = result
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_auth_context] = lambda: ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=body)
    return response, db


def legacy_rule():
    now = datetime.now(UTC)
    return AlarmRule(id=uuid.uuid4(), integration_id=uuid.uuid4(), metric="power_kw", unit=None,
                     registry_version=None, threshold=1500, rule_type="threshold_high", enabled=True,
                     name="legacy", created_at=now, updated_at=now, blocked_notified_at=now)


async def test_attention_list_exposes_unresolved_contract_and_timestamp():
    row = legacy_rule()
    response, db = await request("GET", "/alarms/rules?needs_attention=true", row=row)
    assert response.status_code == 200
    data = response.json()[0]
    assert data["unit_status"] == "unresolved"
    assert data["needs_attention"] is True
    assert data["updated_at"]
    assert "alarm_rule.unit IS NULL" in str(db.execute.call_args.args[0])


@pytest.mark.parametrize("method,path", [("GET", "/alarms/rules"),
                                          ("POST", f"/alarms/rules/{uuid.uuid4()}/resolve-unit"),
                                          ("POST", f"/alarms/rules/{uuid.uuid4()}/disable")])
async def test_rule_routes_require_permission(method, path):
    response, db = await request(method, path, permissions=frozenset(),
                                 body={"unit": "W", "reason": "Evidence", "expected_updated_at": datetime.now(UTC).isoformat()})
    assert response.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("operation", ["resolve-unit", "disable"])
async def test_missing_rule_returns_404(operation):
    response, _ = await request("POST", f"/alarms/rules/{uuid.uuid4()}/{operation}",
                                body={"unit": "W", "reason": "Evidence", "expected_updated_at": datetime.now(UTC).isoformat()})
    assert response.status_code == 404


async def test_resolution_reports_retained_window_count_and_does_not_replay():
    row = legacy_rule()
    response, db = await request("POST", f"/alarms/rules/{row.id}/resolve-unit", row=row,
                                 body={"unit": "W", "reason": "Source documentation", "expected_updated_at": row.updated_at.isoformat()})
    assert response.status_code == 200
    assert response.json()["threshold"] == 1500
    assert response.json()["retained_blocked_window_readings"] == 3
    assert response.json()["retroactive_evaluation"] is False
    assert response.json()["blocked_window_count_complete"] is False
    assert "FOR UPDATE" in str(db.execute.call_args_list[0].args[0])
    db.commit.assert_awaited_once()


async def test_resolution_stale_timestamp_conflicts_before_commit():
    row = legacy_rule()
    response, db = await request("POST", f"/alarms/rules/{row.id}/resolve-unit", row=row,
                                 body={"unit": "W", "reason": "Evidence", "expected_updated_at": (row.updated_at - timedelta(seconds=1)).isoformat()})
    assert response.status_code == 409
    assert row.unit is None
    db.commit.assert_not_awaited()
