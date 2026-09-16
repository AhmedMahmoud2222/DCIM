from app.domain.identity.models import ALLOWED_LIFECYCLE_TRANSITIONS, LIFECYCLE_STATUSES


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_LIFECYCLE_TRANSITIONS.keys()) == set(LIFECYCLE_STATUSES)


def test_removed_is_terminal():
    assert ALLOWED_LIFECYCLE_TRANSITIONS["removed"] == set()


def test_planned_cannot_jump_directly_to_active():
    assert "active" not in ALLOWED_LIFECYCLE_TRANSITIONS["planned"]


def test_active_can_reach_decommissioned_via_maintenance_or_directly():
    assert "decommissioned" in ALLOWED_LIFECYCLE_TRANSITIONS["active"]


def test_decommissioned_can_only_reach_removed():
    assert ALLOWED_LIFECYCLE_TRANSITIONS["decommissioned"] == {"removed"}
