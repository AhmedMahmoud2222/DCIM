"""Check visibility ordering independently from HTTP/DB regressions."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application import user_admin_service as service
from app.application.access_control import AccessScope, EffectiveAccess
from app.core.errors import ForbiddenError, NotFoundError


def _scope(site):
    return AccessScope(False, frozenset({site}), frozenset({site}), frozenset())


def _actor(site):
    return SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()), scope=_scope(site), permission_codes=frozenset({"user:manage", "group:manage"}), role_names=frozenset())


@pytest.mark.parametrize("mixed", [False, True])
async def test_hidden_users_checked_before_permission_comparisons(monkeypatch, mixed):
    a, b = uuid.uuid4(), uuid.uuid4()
    actor = _actor(a)
    peer = EffectiveAccess(uuid.uuid4(), actor.permission_codes, frozenset(), _scope(a))
    hidden = EffectiveAccess(uuid.uuid4(), frozenset({"equipment:read"}), frozenset(), _scope(b))
    targets = [peer, hidden] if mixed else [hidden]
    monkeypatch.setattr(service, "load_effective_access", AsyncMock(return_value={u.user_id: u for u in targets}))
    comparison = AsyncMock(side_effect=AssertionError("authority inspected before visibility"))
    monkeypatch.setattr(service, "actor_strictly_outranks", comparison)
    with pytest.raises(NotFoundError, match="The requested resource was not found"):
        await service.assert_actor_outranks_users(None, actor, {u.user_id for u in targets})
    comparison.assert_not_awaited()


async def test_hidden_group_grants_are_never_read(monkeypatch):
    a, b, gid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(service, "load_group_scopes", AsyncMock(return_value={gid: _scope(b)}))
    grants = AsyncMock(side_effect=AssertionError("hidden grants were read"))
    monkeypatch.setattr(service, "group_grants", grants)
    with pytest.raises(NotFoundError, match="The requested resource was not found"):
        await service.assert_can_assign_group(None, _actor(a), gid)
    grants.assert_not_awaited()


@pytest.mark.parametrize("peer", [False, True])
async def test_visible_users_keep_strict_outranking(monkeypatch, peer):
    a = uuid.uuid4()
    actor = _actor(a)
    target = EffectiveAccess(uuid.uuid4(), actor.permission_codes if peer else frozenset(), frozenset(), _scope(a))
    monkeypatch.setattr(service, "load_effective_access", AsyncMock(return_value={target.user_id: target}))
    if peer:
        with pytest.raises(ForbiddenError):
            await service.assert_actor_outranks_users(None, actor, {target.user_id})
    else:
        await service.assert_actor_outranks_users(None, actor, {target.user_id})
