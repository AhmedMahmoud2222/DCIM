"""Opt-in G9a equipment projection. Integer columns are not authored measurements.

The policy is a DRAFT prototype: half-up to mm/kg and a 1 kg minimum for positive
equipment mass. It is NOT approved for rack load capacity or physical fit guarantees.
Registry v1 owns conversion factors; its default version is the historical v1.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.domain.telemetry.registry import convert_value

_MAX_INTEGER = 2_147_483_647  # PostgreSQL Integer columns in legacy catalog revisions


class PhysicalRevision(Protocol):
    @property
    def rack_unit_height(self) -> int | None: ...

    @property
    def width_value(self) -> Decimal | float | None: ...

    @property
    def depth_value(self) -> Decimal | float | None: ...

    @property
    def weight_value(self) -> Decimal | float | None: ...

    @property
    def dimension_unit(self) -> str | None: ...

    @property
    def weight_unit(self) -> str | None: ...


def _project(value: Decimal | float | None, source: str | None, target: str, *, positive_floor: bool = False) -> int | None:
    if value is None:
        return None
    authored = Decimal(str(value))
    if not authored.is_finite() or authored < 0:
        raise ValueError("Physical projections require finite, nonnegative authored values.")
    if source is None:
        raise ValueError("Physical projection requires the authored unit.")
    # Existing convert_value defaults to registry v1. G10 freezes that snapshot;
    # this independently reviewable consumer does not require the new signature.
    projected = int(convert_value(authored, source, target).to_integral_value(rounding=ROUND_HALF_UP))
    if positive_floor and authored > 0:
        projected = max(1, projected)
    if projected > _MAX_INTEGER:
        raise ValueError("Physical projection exceeds the legacy PostgreSQL integer column range.")
    return projected


def project_physical(revision: PhysicalRevision) -> tuple[int | None, int | None, int | None, int | None]:
    """Project new equipment bridge values without modifying the authored revision.

    U count passes through. Registry conversion first uses its existing eight-place
    Decimal quantization; the declared whole-unit projection is then half-up.
    Never use the mass floor to project a rack's available load capacity.
    """
    return (
        revision.rack_unit_height,
        _project(revision.width_value, revision.dimension_unit, "mm"),
        _project(revision.depth_value, revision.dimension_unit, "mm"),
        _project(revision.weight_value, revision.weight_unit, "kg", positive_floor=True),
    )
