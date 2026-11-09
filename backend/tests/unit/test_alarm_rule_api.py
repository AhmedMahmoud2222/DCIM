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
                                          ("POST", "/alarms/rules"),
                                          ("PATCH", f"/alarms/rules/{uuid.uuid4()}"),
                                          ("POST", f"/alarms/rules/{uuid.uuid4()}/resolve-unit"),
                                          ("POST", f"/alarms/rules/{uuid.uuid4()}/disable")])
async def test_rule_routes_require_permission(method, path):
    response, db = await request(method, path, permissions=frozenset(),
                                 body={"unit": "W", "reason": "Evidence", "expected_updated_at": datetime.now(UTC).isoformat()})
    assert response.status_code == 403
    db.execute.assert_not_awaited()


async def test_create_rule_is_available_to_managers(monkeypatch):
    monkeypatch.setattr("app.api.v1.alarms.write_audit_log", AsyncMock())
    monkeypatch.setattr("app.api.v1.alarms.write_outbox_event", AsyncMock())
    response, db = await request(
        "POST", "/alarms/rules",
        body={"integration_id": str(uuid.uuid4()), "metric": "power_kw", "rule_type": "threshold_high", "threshold": 7, "name": "PDU load"},
    )
    assert response.status_code == 201
    assert db.add.call_args.args[0].name == "PDU load"
    assert db.add.call_args.args[0].unit == "kW"
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("threshold", ["NaN", "Infinity", "-Infinity", "10000000000", "1.123456789"])
async def test_create_rule_rejects_nonfinite_or_out_of_range_thresholds(threshold):
    response, db = await request(
        "POST", "/alarms/rules",
        body={"integration_id": str(uuid.uuid4()), "metric": "custom_metric", "rule_type": "threshold_high", "threshold": threshold, "name": "Invalid threshold"},
    )
    assert response.status_code == 422
    db.add.assert_not_called()
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("threshold", ["NaN", "Infinity", "-Infinity", "10000000000", "1.123456789"])
async def test_edit_rule_rejects_nonfinite_or_out_of_range_thresholds(threshold):
    row = legacy_rule()
    row.unit = "kW"
    row.registry_version = "1"
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"threshold": threshold, "reason": "Invalid threshold", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 422
    assert row.threshold == 1500
    db.execute.assert_not_awaited()


async def test_edit_rule_updates_name_and_threshold_with_audit(monkeypatch):
    row = legacy_rule()
    row.unit = "kW"
    row.registry_version = "1"
    audit = AsyncMock()
    outbox = AsyncMock()
    monkeypatch.setattr("app.application.alarm_service.write_audit_log", audit)
    monkeypatch.setattr("app.application.alarm_service.write_outbox_event", outbox)
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": "PDU load high", "threshold": 8.5, "reason": "Updated operating limit", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 200
    assert row.name == "PDU load high" and row.threshold == 8.5
    assert response.json()["unit"] == "kW"
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["before"] == {"name": "legacy", "threshold": "1500"}
    assert audit.await_args.kwargs["after"] == {"name": "PDU load high", "threshold": "8.5"}
    outbox.assert_awaited_once()
    assert outbox.await_args.kwargs["event_type"] == "AlarmRuleUpdated"
    assert db.execute.await_count == 1  # lock the rule only; historical alarms/readings are not replayed or mutated
    db.commit.assert_awaited_once()


async def test_edit_stale_timestamp_conflicts_without_mutating_rule():
    row = legacy_rule()
    original = (row.name, row.threshold, row.updated_at)
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": "Changed", "reason": "stale request", "expected_updated_at": (row.updated_at - timedelta(seconds=1)).isoformat()},
    )
    assert response.status_code == 409
    assert (row.name, row.threshold, row.updated_at) == original
    db.commit.assert_not_awaited()


async def test_edit_unresolved_threshold_conflicts_without_applying_name():
    row = legacy_rule()
    original = (row.name, row.threshold, row.updated_at)
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": "Changed", "threshold": 8, "reason": "unit unresolved", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 409
    assert (row.name, row.threshold, row.updated_at) == original
    db.commit.assert_not_awaited()


async def test_edit_rejects_noop_and_invalid_threshold_without_mutation():
    row = legacy_rule()
    row.unit = "%"
    row.metric = "humidity_percent"
    row.registry_version = "2"
    row.threshold = 50
    original = (row.name, row.threshold, row.updated_at)

    noop, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": row.name, "reason": "no change", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert noop.status_code == 422
    db.commit.assert_not_awaited()

    invalid, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": "Changed", "threshold": 101, "reason": "out of range", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert invalid.status_code == 422
    assert (row.name, row.threshold, row.updated_at) == original
    db.commit.assert_not_awaited()


async def test_unresolved_rule_allows_name_only_edit():
    row = legacy_rule()
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"name": "Clarified label", "reason": "Better label", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 200
    assert row.name == "Clarified label" and row.unit is None and row.threshold == 1500
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("threshold", ["Infinity", "NaN"])
async def test_edit_rejects_non_finite_threshold(threshold):
    row = legacy_rule()
    row.unit = "kW"
    row.registry_version = "1"
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"threshold": threshold, "reason": "finite threshold required", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 422
    assert row.threshold == 1500
    db.commit.assert_not_awaited()


async def test_edit_rejects_threshold_for_unavailable_rule():
    row = legacy_rule()
    row.metric = "availability"
    row.rule_type = "availability_unavailable"
    row.threshold = None
    response, db = await request(
        "PATCH", f"/alarms/rules/{row.id}", row=row,
        body={"threshold": 1, "reason": "threshold unsupported", "expected_updated_at": row.updated_at.isoformat()},
    )
    assert response.status_code == 422
    assert row.threshold is None
    db.commit.assert_not_awaited()


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
