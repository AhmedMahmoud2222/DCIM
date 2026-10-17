import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.telemetry.lineage import evidence_class, lineage_coverage, summarize_fresh_readings


def reading(revision, evidence, occurred, scale=Decimal(1)):
    return SimpleNamespace(mapping_revision_id=revision, contract_evidence=evidence,
                           raw_unit="degC", source_scale=scale, occurred_at=occurred,
                           raw_value_text="25.000000001", attributes={"private": "not copied"})


def test_distinct_revisions_and_evidence_remain_separate_with_exact_counts():
    first, second = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    summaries = summarize_fresh_readings([
        reading(first, "pinned", now), reading(first, "pinned", now - timedelta(seconds=1)),
        reading(second, "pinned", now), reading(first, "operator_resolved", now),
    ])
    assert sorted(row.sample_count for row in summaries) == [1, 1, 2]
    assert summaries[0].first_occurred_at == now - timedelta(seconds=1)
    assert summaries[0].last_occurred_at == now
    assert evidence_class(row.key for row in summaries) == "mixed"
    assert all(not hasattr(row, "raw_value_text") and not hasattr(row, "attributes") for row in summaries)


def test_no_backfill_or_complete_coverage_claim_for_prelineage_samples():
    assert lineage_coverage(100, 0) == "none"
    assert lineage_coverage(101, 1) == "partial"
    assert lineage_coverage(101, 101) == "complete"
    with pytest.raises(ValueError):
        lineage_coverage(100, 101)


def test_missing_revision_cannot_claim_pinned_evidence():
    summaries = summarize_fresh_readings([reading(None, "pinned", datetime.now(UTC))])
    assert evidence_class(row.key for row in summaries) == "unverified"


def test_null_contract_and_scale_stay_null():
    summaries = summarize_fresh_readings([reading(None, None, datetime.now(UTC), None)])
    assert summaries[0].key.source_scale is None
    assert summaries[0].key.mapping_revision_id is None


def test_contract_identity_distinguishes_null_empty_and_normalizes_scale():
    from app.application.telemetry_lineage import contract_key
    from app.domain.telemetry.lineage import LineageKey
    key = LineageKey(None, None, None, Decimal("1"))
    empty = LineageKey(None, None, "", Decimal("1"))
    same = LineageKey(None, None, None, Decimal("1.00000000"))
    assert contract_key(key) != contract_key(empty)
    assert contract_key(key) == contract_key(same)


@pytest.mark.parametrize('sample_count,lineage_count', [(0, 0), (1, -1), (2, 3)])
def test_coverage_rejects_impossible_counts(sample_count, lineage_count):
    with pytest.raises(ValueError, match='contradict'):
        lineage_coverage(sample_count, lineage_count)


def test_grouping_keeps_distinct_source_units_and_scales_and_normalizes_event_time():
    revision = uuid.uuid4()
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    first = reading(revision, 'pinned', instant, Decimal('1'))
    same = reading(revision, 'pinned', instant.astimezone(timezone(timedelta(hours=4))), Decimal('1.00000000'))
    other_scale = reading(revision, 'pinned', instant, Decimal('0.001'))
    other_unit = reading(revision, 'pinned', instant)
    other_unit.raw_unit = 'degF'
    summaries = summarize_fresh_readings([first, same, other_scale, other_unit])
    assert sorted(row.sample_count for row in summaries) == [1, 1, 2]
    assert {row.key.source_scale for row in summaries} == {Decimal('1'), Decimal('0.001')}
    assert {row.key.raw_unit for row in summaries} == {'degC', 'degF'}
    assert all(row.first_occurred_at == row.last_occurred_at == instant for row in summaries)


@pytest.mark.parametrize('evidence,expected', [
    ('pinned', 'pinned'), ('inferred_single_revision', 'inferred'), ('operator_resolved', 'operator_resolved'),
    (None, 'unverified'), ('unknown', 'unverified'),
])
def test_evidence_class_never_upgrades_contract_evidence(evidence, expected):
    summaries = summarize_fresh_readings([reading(uuid.uuid4(), evidence, datetime.now(UTC))])
    assert evidence_class(row.key for row in summaries) == expected
