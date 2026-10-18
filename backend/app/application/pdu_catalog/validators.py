"""Pure validation for PDU catalog data."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

PDU_PHASES_ALLOWED = ("single", "three")
PDU_MOUNTING_ORIENTATIONS_ALLOWED = ("horizontal", "vertical")
PDU_SIDES_ALLOWED = ("front", "rear")
PDU_OUTLET_TYPES_ALLOWED = ("iec_c13", "iec_c19", "iec_c39", "nema_5_15", "nema_5_20", "schuko", "other")


class ValidationIssue(BaseModel):
    field: str
    code: str
    message: str


class ValidationSummary(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_pdu_revision(draft: dict[str, Any], outlets: list[dict[str, Any]]) -> ValidationSummary:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    pdu_input_voltage_v = _to_float(draft.get("pdu_input_voltage_v"))
    pdu_input_current_a = _to_float(draft.get("pdu_input_current_a"))
    pdu_phase = draft.get("pdu_phase")
    pdu_mouting = draft.get("pdu_mounting_orientation") or draft.get("mounting_orientation")
    mounting = draft.get("mounting_orientation")

    if pdu_input_voltage_v is not None and pdu_input_voltage_v < 0:
        errors.append(ValidationIssue(field="pdu_input_voltage_v", code="invalid_voltage", message="Input voltage must be >= 0."))
    if pdu_input_current_a is not None and pdu_input_current_a <= 0:
        errors.append(ValidationIssue(field="pdu_input_current_a", code="invalid_current", message="Input current must be > 0."))

    if pdu_phase is not None and pdu_phase not in PDU_PHASES_ALLOWED:
        errors.append(ValidationIssue(field="pdu_phase", code="invalid_phase", message=f"Phase must be one of {PDU_PHASES_ALLOWED}."))

    outlet_numbers = []
    for i, o in enumerate(outlets):
        outlet_number = o.get("outlet_number")
        rated_voltage = _to_float(o.get("rated_voltage_v"))
        rated_current = _to_float(o.get("rated_current_a"))
        outlet_type = o.get("outlet_type")
        phase = o.get("phase")
        side = o.get("side", "front")
        outlet_count = o.get("outlet_count", 1)
        stable_key = o.get("stable_key")
        if stable_key is None:
            errors.append(ValidationIssue(field=f"outlets[{i}].stable_key", code="required", message="stable_key required"))
        if outlet_number is None:
            errors.append(ValidationIssue(field=f"outlets[{i}].outlet_number", code="required", message="outlet_number required"))
        else:
            outlet_numbers.append(outlet_number)
        if outlet_type is not None and outlet_type not in PDU_OUTLET_TYPES_ALLOWED:
            errors.append(ValidationIssue(field=f"outlets[{i}].outlet_type", code="invalid_outlet_type", message="Invalid outlet type"))
        if phase is not None and phase not in PDU_PHASES_ALLOWED:
            errors.append(ValidationIssue(field=f"outlets[{i}].phase", code="invalid_phase", message="Invalid phase"))
        if side not in PDU_SIDES_ALLOWED:
            errors.append(ValidationIssue(field=f"outlets[{i}].side", code="invalid_side", message="Invalid side"))
        if rated_voltage is not None and rated_voltage < 0:
            errors.append(ValidationIssue(field=f"outlets[{i}].rated_voltage_v", code="invalid_voltage", message="Invalid voltage"))
        if rated_current is not None and rated_current <= 0:
            errors.append(ValidationIssue(field=f"outlets[{i}].rated_current_a", code="invalid_current", message="Invalid current"))
        try:
            if int(outlet_count) <= 0:
                errors.append(ValidationIssue(field=f"outlets[{i}].outlet_count", code="invalid_count", message="count > 0"))
        except Exception:
            errors.append(ValidationIssue(field=f"outlets[{i}].outlet_count", code="invalid_count", message="count > 0"))

    seen = set()
    for num in outlet_numbers:
        if num in seen:
            errors.append(ValidationIssue(field="outlets", code="duplicate_outlet_number", message=f"Duplicate outlet number {num}"))
            break
        seen.add(num)

    if pdu_mouting is not None and pdu_mouting not in PDU_MOUNTING_ORIENTATIONS_ALLOWED:
        errors.append(ValidationIssue(field="pdu_mounting_orientation", code="invalid_mounting", message="Invalid mounting orientation"))
    if mounting is not None and mounting not in PDU_MOUNTING_ORIENTATIONS_ALLOWED:
        errors.append(ValidationIssue(field="mounting_orientation", code="invalid_mounting", message="Invalid mounting orientation"))

    return ValidationSummary(valid=not errors, errors=errors, warnings=warnings)
