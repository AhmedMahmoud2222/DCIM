from types import SimpleNamespace

from app.application.alarm_service import condition_matches, subject_for


def test_threshold_and_availability_conditions_are_deterministic():
    assert condition_matches("threshold_high", 30, 30.1)
    assert not condition_matches("threshold_high", 30, 30)
    assert condition_matches("threshold_low", 20, 19.9)
    assert not condition_matches("threshold_low", 20, 20)
    assert condition_matches("availability_unavailable", None, 0)
    assert not condition_matches("availability_unavailable", None, 1)


def test_alarm_contract_has_single_open_subject_index_and_lifecycle_states():
    from app.domain.alarm.models import ALARM_STATUSES, Alarm

    assert ALARM_STATUSES == ("ACTIVE", "ACKNOWLEDGED", "CLEARED")
    assert any(index.name == "uq_alarm_open_rule_subject" and index.unique for index in Alarm.__table__.indexes)


def test_alarm_subject_key_uses_only_node_name_and_metric():
    first = SimpleNamespace(integration_id="integration-a", external_identifier="node-01", metric="temperature_c")
    second = SimpleNamespace(integration_id="integration-b", external_identifier="node-01", metric="temperature_c")

    assert subject_for(first) == "node-01:temperature_c"
    assert subject_for(second) == subject_for(first)
