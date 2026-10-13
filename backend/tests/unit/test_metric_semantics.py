"""Version-pinned semantics, contract identity and API boundary regressions."""
import uuid
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

import pytest

from app.api.v1.alarms import AlarmRuleIn, create_alarm_rule
from app.api.v1.telemetry import MetricMappingIn
from app.core.errors import ApiError
from app.domain.telemetry import registry
from app.domain.telemetry.contract import conversion_contract_hash, conversion_contract_text
from app.domain.telemetry.metric_semantics import ValueKind, ValueOutOfRange


@pytest.mark.parametrize('metric,value', [
    ('humidity_percent', '150'), ('temperature_c', '-500'), ('power_kw', '-3'),
    ('load_percent', '-5'), ('availability', '1.5'), ('availability', '.5'),
    ('airflow_m3_s', '-1'), ('airflow_velocity_m_s', '-1'), ('cooling_output_kw', '-1'),
])
def test_v2_rejects_impossible_values_v1_retains_existing_behavior(metric, value):
    unit = registry.METRIC_REGISTRY[metric].canonical_unit
    assert registry.convert_to_canonical(metric, Decimal(value), unit).value == Decimal(value)
    with pytest.raises(ValueOutOfRange):
        registry.convert_to_canonical(metric, Decimal(value), unit, registry_version='2')


@pytest.mark.parametrize('metric', registry.METRIC_REGISTRY)
def test_every_v2_metric_declares_immutable_kind_units_ranges_aggregations_and_rules(metric):
    definition = registry.registry_snapshot('2').metrics[metric]
    policy = definition.semantics
    assert policy.value_kind in ValueKind
    assert policy.allowed_source_units
    assert policy.aggregations == ('avg', 'min', 'max', 'last')
    for boundary in (policy.min_canonical, policy.max_canonical):
        if boundary is not None:
            registry.convert_to_canonical(metric, boundary, definition.canonical_unit, registry_version='2')
    with pytest.raises(TypeError):
        registry.registry_snapshot('2').metrics[metric] = definition


@pytest.mark.parametrize('metric,value,unit,expected', [
    ('availability','100','%','1'), ('availability','0','%','0'),
    ('humidity_percent','1','1','100'), ('temperature_c','0','K','-273.15'),
    ('load_percent','150','%','150'), ('differential_pressure_pa','-2','kPa','-2000'),
])
def test_conversion_and_approved_bounds(metric, value, unit, expected):
    assert registry.convert_to_canonical(metric, Decimal(value), unit, registry_version='2').value == Decimal(expected)


def test_range_is_applied_after_single_quantization():
    assert registry.convert_to_canonical('humidity_percent', Decimal('100.000000004'), '%', registry_version='2').value == 100
    with pytest.raises(ValueOutOfRange):
        registry.convert_to_canonical('humidity_percent', Decimal('100.000000006'), '%', registry_version='2')


def test_v2_contract_digest_includes_semantics_and_keeps_v1_golden():
    assert conversion_contract_hash('temperature_c','degF',Decimal(1),'1') == '86044c6d3f4ad702dfd5f5166342b1f57aa668050a9a1aaf547bfcf802ba320e'
    text = conversion_contract_text('humidity_percent','%',Decimal(1),'2')
    assert text.startswith('dcim.mapping-conversion.v2\n')
    assert f'registry_content_hash={registry.registry_snapshot("2").content_hash}' in text
    v2 = registry.registry_snapshot('2')
    metrics = dict(v2.metrics)
    metrics['humidity_percent'] = replace(metrics['humidity_percent'], semantics=replace(metrics['humidity_percent'].semantics, max_canonical=Decimal(101)))
    altered = registry.make_snapshot('2',v2.units,v2.aliases,metrics)
    assert altered.content_hash != v2.content_hash


def test_source_allowlist_is_enforced_independent_of_dimension(monkeypatch):
    v2 = registry.registry_snapshot('2')
    metrics = dict(v2.metrics)
    metrics['humidity_percent'] = replace(metrics['humidity_percent'], semantics=replace(metrics['humidity_percent'].semantics, allowed_source_units=('%',)))
    monkeypatch.setattr(registry,'REGISTRIES',MappingProxyType({'1':registry.registry_snapshot('1'), '2':registry.make_snapshot('2',v2.units,v2.aliases,metrics)}))
    with pytest.raises(registry.UnitDimensionMismatch):
        registry.validate_metric_unit('humidity_percent','1',registry_version='2')


def test_mapping_api_defaults_to_v1_and_accepts_explicit_version_only():
    fields = dict(integration_id=uuid.uuid4(),source_identifier='sensor',canonical_metric='humidity_percent',unit='%')
    assert MetricMappingIn(**fields).registry_version == '1'
    assert MetricMappingIn(**fields,registry_version='2').registry_version == '2'


@pytest.mark.parametrize('metric,type_,threshold', [
    ('humidity_percent','threshold_high',101), ('power_kw','threshold_low',-1),
    ('availability','threshold_high',1), ('temperature_c','availability_unavailable',None),
    ('humidity_percent','threshold_high',float('inf')),
])
async def test_alarm_api_rejects_v2_incompatible_semantics_before_db(metric,type_,threshold):
    body=AlarmRuleIn(integration_id=uuid.uuid4(),metric=metric,rule_type=type_,threshold=threshold,name='bad',registry_version='2')
    with pytest.raises(ApiError):
        await create_alarm_rule(body,db=None,ctx=None)


def test_threshold_authored_unit_converts_before_range_check():
    registry.validate_metric_rule('humidity_percent','threshold_high',Decimal('.9'),'1',registry_version='2')
    with pytest.raises(ValueOutOfRange):
        registry.validate_metric_rule('humidity_percent','threshold_high',Decimal('1.1'),'1',registry_version='2')
    registry.validate_metric_rule('availability','availability_unavailable',None,'1',registry_version='2')


def test_nonfinite_keeps_numeric_failure_class():
    with pytest.raises(ValueError) as error:
        registry.validate_metric_value('humidity_percent',Decimal('NaN'),registry_version='2')
    assert not isinstance(error.value,ValueOutOfRange)


async def test_registry_api_explicit_v2_and_unknown_version():
    from app.api.v1.telemetry import get_metric_registry
    response = await get_metric_registry(version='2',ctx=None)
    assert response.version == '2'
    assert response.metrics['availability']['value_kind'] == 'state'
    assert response.metrics['humidity_percent']['max_canonical'] == '100'
    with pytest.raises(ApiError):
        await get_metric_registry(version='99',ctx=None)


def test_unknown_version_fails_before_semantic_conversion():
    with pytest.raises(registry.UnknownRegistryVersion):
        registry.convert_to_canonical('humidity_percent',Decimal(1),'%',registry_version='99')


def test_v2_golden_hash_and_full_snapshot():
    from pathlib import Path
    snapshot = registry.registry_snapshot('2')
    assert snapshot.content_hash == registry.V2_CONTENT_HASH
    assert registry.snapshot_text(snapshot) == Path(registry.__file__).with_name('registry_v2.snapshot.txt').read_text()
