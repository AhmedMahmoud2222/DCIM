from decimal import Decimal

import pytest

from app.domain.catalog.template_binding import TemplateBinding, validate_template_binding


def validate(binding, **changes):
    values = dict(unit="degF", scale=Decimal("0.1"), offset=Decimal(0),
                  transform="none")
    validate_template_binding(binding, **(values | changes))


def test_bound_source_scaling_does_not_repeat_registry_conversion():
    validate(TemplateBinding("temperature_c", "1", "degF"))


@pytest.mark.parametrize("changes", [
    {"offset": Decimal(32)}, {"transform": "fahrenheit_to_celsius"},
    {"unit": "W"}, {"scale": Decimal("NaN")}, {"scale": Decimal("Infinity")},
    {"scale": Decimal("1e10")}, {"offset": Decimal("NaN")}, {"transform": None},
])
def test_ambiguous_transform_or_incompatible_unit_is_rejected(changes):
    with pytest.raises(ValueError):
        validate(TemplateBinding("temperature_c", "1", "degF"), **changes)


def test_suggested_threshold_requires_authored_compatible_unit():
    with pytest.raises(ValueError):
        validate(TemplateBinding("temperature_c", "1", None))
    with pytest.raises(ValueError):
        validate(TemplateBinding("temperature_c", "1", "kW"))


def test_unbound_legacy_template_keeps_free_text_and_transform():
    validate(TemplateBinding(None, None, None), unit="vendor-status", offset=Decimal(32), transform="vendor-defined")


def test_partial_binding_cannot_inherit_current_registry_implicitly():
    with pytest.raises(ValueError):
        validate(TemplateBinding("temperature_c", None, "degF"))
    with pytest.raises(ValueError):
        validate(TemplateBinding(None, "1", None))


@pytest.mark.parametrize("version", ["1", "2"])
def test_authored_source_encoding_and_threshold_are_converted_once(version):
    from app.domain.telemetry.registry import convert_to_canonical, convert_value

    binding = TemplateBinding("temperature_c", version, "degF")
    validate(binding)
    # 950 tenths Fahrenheit is 95 F. The template does not preconvert it;
    # the pinned registry performs the sole physical-unit conversion.
    sample = convert_to_canonical("temperature_c", Decimal(950), "degF", source_scale=Decimal("0.1"),
                          registry_version=version)
    assert sample.value == Decimal("35")
    assert sample.raw_value == Decimal(950) and sample.raw_unit == "degF"
    assert binding.threshold_unit is not None
    assert convert_value(Decimal(95), binding.threshold_unit, sample.unit, registry_version=version) == sample.value


@pytest.mark.parametrize("binding", [
    TemplateBinding("does-not-exist", "2", "degF"),
    TemplateBinding("temperature_c", "current", "degF"),
    TemplateBinding(None, None, "degF"),
])
def test_unknown_and_partial_bindings_fail_closed(binding):
    with pytest.raises(ValueError):
        validate(binding)


@pytest.mark.parametrize("version", ["1", "2"])
def test_binding_retains_authored_alias_instead_of_rewriting(version):
    binding = TemplateBinding("temperature_c", version, "fahrenheit")
    validate(binding, unit="fahrenheit")
    assert binding.threshold_unit == "fahrenheit"
