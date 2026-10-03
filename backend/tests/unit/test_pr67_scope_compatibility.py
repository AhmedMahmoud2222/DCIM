"""The strict-authority guard must preserve the parent's per-site rack grants."""

import uuid

from app.application.user_admin_service import scope_contains, scope_from_entries


def test_delegated_scope_retains_selected_racks_under_their_granted_site():
    a, b, rack = (uuid.uuid4() for _ in range(3))
    scope = scope_from_entries([(a, "selected", [rack]), (b, "selected", [])])
    assert scope.selected_racks_by_site == {a: frozenset({rack}), b: frozenset()}


async def test_the_same_rack_granted_under_another_site_is_not_contained():
    a, b, rack = (uuid.uuid4() for _ in range(3))
    actor = scope_from_entries([(a, "selected", [rack]), (b, "selected", [])])
    target = scope_from_entries([(a, "selected", []), (b, "selected", [rack])])
    assert not await scope_contains(None, actor, target)
    assert not await scope_contains(None, target, actor)


async def test_all_racks_in_one_site_do_not_cover_a_selected_grant_in_another_site():
    a, b, rack = (uuid.uuid4() for _ in range(3))
    actor = scope_from_entries([(a, "all", []), (b, "selected", [])])
    target = scope_from_entries([(b, "selected", [rack])])
    assert not await scope_contains(None, actor, target)


async def test_per_site_scope_still_allows_equal_and_subset_grant_comparisons():
    a, r1, r2 = (uuid.uuid4() for _ in range(3))
    actor = scope_from_entries([(a, "selected", [r1, r2])])
    subset = scope_from_entries([(a, "selected", [r1])])
    assert await scope_contains(None, actor, subset)
    assert await scope_contains(None, actor, actor)
    assert not await scope_contains(None, subset, actor)
