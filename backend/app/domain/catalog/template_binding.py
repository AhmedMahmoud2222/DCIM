"""Optional, version-pinned monitoring templates. No conversion or seeding here.

Scale denotes sensor encoding only; registry owns physical-unit conversion.
Unbound templates retain legacy free-text behavior.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.telemetry.numeric import ensure_storable
from app.domain.telemetry.registry import validate_metric_unit


@dataclass(frozen=True)
class TemplateBinding:
    registry_metric: str | None
    registry_version: str | None
    threshold_unit: str | None


def validate_template_binding(
    binding: TemplateBinding, *, unit: str | None, scale: Decimal, offset: Decimal,
    transform: str | None,
) -> None:
    """Validate authored metadata without converting values or rewriting units.

    Bound templates always carry a threshold unit, including templates without
    suggestions, so adding a suggestion later cannot silently inherit a unit.
    """
    if binding.registry_metric is None:
        if binding.registry_version is not None or binding.threshold_unit is not None:
            raise ValueError("Unbound templates cannot carry a registry version or threshold unit")
        return
    if binding.registry_version is None or unit is None:
        raise ValueError("Bound templates require explicit registry version and source unit")
    validate_metric_unit(binding.registry_metric, unit, registry_version=binding.registry_version)
    if not offset.is_finite() or offset != 0 or transform != "none":
        raise ValueError("Bound templates cannot apply an offset or additional conversion transform")
    ensure_storable(scale, "template source scale")
    if binding.threshold_unit is None:
        raise ValueError("Bound templates require an authored threshold unit")
    validate_metric_unit(binding.registry_metric, binding.threshold_unit, registry_version=binding.registry_version)
