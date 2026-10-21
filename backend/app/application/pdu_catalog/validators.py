"""Pure validation for PDU catalog data."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

PDU_PHASES_ALLOWED = ("single", "three")
PDU_MOUNTING_ORIENTATIONS_ALLOWED = ("horizontal", "vertical")
PDU_SIDES_ALLOWED = ("front", "rear")
PDU_OUTLET_TYPES_ALLOWED = ("iec_c13", "iec_c19", "iec_c39", "nema_5_15", "nema_5_20", "schuko", "other")

# Database-representable ceilings for the columns that carry these values, so an
# overflowing input produces a controlled 422 validation error instead of a PostgreSQL
# numeric_overflow 500 at flush/commit time.
#   Numeric(p, s) max = 10 ** (p - s) - 10 ** (-s)
PDU_INPUT_VOLTAGE_MAX = 10 ** (6 - 1) - 10 ** (-1)   # Numeric(6, 1) -> 99999.9
PDU_INPUT_CURRENT_MAX = 10 ** (6 - 2) - 10 ** (-2)   # Numeric(6, 2) -> 9999.99
PDU_RATED_CAPACITY_MAX = 10 ** (10 - 3) - 10 ** (-3)  # Numeric(10, 3) -> 9999999.999


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


def _check_numeric_bounds(value: float, *, maximum: float, field: str, code: str) -> ValidationIssue | None:
    """Reject non-finite or out-of-(database-representable)-range values before
    persistence so overflow never reaches PostgreSQL as a 500."""
    if not math.isfinite(value):
        return ValidationIssue(field=field, code=code, message=f"{field} must be a finite number.")
    if abs(value) > maximum:
        return ValidationIssue(field=field, code=code, message=f"{field} exceeds the allowed range (max {maximum}).")
    return None


def validate_pdu_revision(draft: dict[str, Any], outlets: list[dict[str, Any]]) -> ValidationSummary:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    pdu_input_voltage_v = _to_float(draft.get("pdu_input_voltage_v"))
    pdu_input_current_a = _to_float(draft.get("pdu_input_current_a"))
    pdu_phase = draft.get("pdu_phase")
    pdu_mouting = draft.get("pdu_mounting_orientation") or draft.get("mounting_orientation")
    mounting = draft.get("mounting_orientation")

    if pdu_input_voltage_v is not None:
        if pdu_input_voltage_v <= 0:
            errors.append(
                ValidationIssue(
                    field="pdu_input_voltage_v",
                    code="invalid_voltage",
                    message="Input voltage must be > 0.",
                )
            )
        else:
            issue = _check_numeric_bounds(
                pdu_input_voltage_v, maximum=PDU_INPUT_VOLTAGE_MAX, field="pdu_input_voltage_v", code="invalid_voltage"
            )
            if issue is not None:
                errors.append(issue)
    if pdu_input_current_a is not None:
        if pdu_input_current_a <= 0:
            errors.append(
                ValidationIssue(
                    field="pdu_input_current_a",
                    code="invalid_current",
                    message="Input current must be > 0.",
                )
            )
        else:
            issue = _check_numeric_bounds(
                pdu_input_current_a, maximum=PDU_INPUT_CURRENT_MAX, field="pdu_input_current_a", code="invalid_current"
            )
            if issue is not None:
                errors.append(issue)

    if pdu_phase is not None and pdu_phase not in PDU_PHASES_ALLOWED:
        errors.append(
            ValidationIssue(field="pdu_phase", code="invalid_phase", message=f"Phase must be one of {PDU_PHASES_ALLOWED}.")
        )

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
            if outlet_number <= 0:
                errors.append(
                    ValidationIssue(
                        field=f"outlets[{i}].outlet_number",
                        code="invalid_outlet_number",
                        message="outlet_number must be > 0.",
                    )
                )
            else:
                outlet_numbers.append(outlet_number)
        if outlet_type is not None and outlet_type not in PDU_OUTLET_TYPES_ALLOWED:
            errors.append(
                ValidationIssue(field=f"outlets[{i}].outlet_type", code="invalid_outlet_type", message="Invalid outlet type")
            )
        if phase is not None and phase not in PDU_PHASES_ALLOWED:
            errors.append(ValidationIssue(field=f"outlets[{i}].phase", code="invalid_phase", message="Invalid phase"))
        if side not in PDU_SIDES_ALLOWED:
            errors.append(ValidationIssue(field=f"outlets[{i}].side", code="invalid_side", message="Invalid side"))
        if rated_voltage is not None:
            if rated_voltage < 0:
                errors.append(
                    ValidationIssue(field=f"outlets[{i}].rated_voltage_v", code="invalid_voltage", message="Invalid voltage")
                )
            else:
                issue = _check_numeric_bounds(
                    rated_voltage,
                    maximum=PDU_INPUT_VOLTAGE_MAX,
                    field=f"outlets[{i}].rated_voltage_v",
                    code="invalid_voltage",
                )
                if issue is not None:
                    errors.append(issue)
        if rated_current is not None:
            if rated_current <= 0:
                errors.append(
                    ValidationIssue(field=f"outlets[{i}].rated_current_a", code="invalid_current", message="Invalid current")
                )
            else:
                issue = _check_numeric_bounds(
                    rated_current,
                    maximum=PDU_INPUT_CURRENT_MAX,
                    field=f"outlets[{i}].rated_current_a",
                    code="invalid_current",
                )
                if issue is not None:
                    errors.append(issue)
        try:
            if int(outlet_count) <= 0:
                errors.append(ValidationIssue(field=f"outlets[{i}].outlet_count", code="invalid_count", message="count > 0"))
        except Exception:
            errors.append(ValidationIssue(field=f"outlets[{i}].outlet_count", code="invalid_count", message="count > 0"))

    seen = set()
    for num in outlet_numbers:
        if num in seen:
            errors.append(
                ValidationIssue(field="outlets", code="duplicate_outlet_number", message=f"Duplicate outlet number {num}")
            )
            break
        seen.add(num)

    if pdu_mouting is not None and pdu_mouting not in PDU_MOUNTING_ORIENTATIONS_ALLOWED:
        errors.append(
            ValidationIssue(
                field="pdu_mounting_orientation", code="invalid_mounting", message="Invalid mounting orientation"
            )
        )
    if mounting is not None and mounting not in PDU_MOUNTING_ORIENTATIONS_ALLOWED:
        errors.append(
            ValidationIssue(
                field="mounting_orientation", code="invalid_mounting", message="Invalid mounting orientation"
            )
        )

    # R6c: the remaining PDU electrical/monitoring attributes flow through the same
    # draft validation, persistence and read surfaces as the core electrical spec.
    pdu_rated_capacity_kw = _to_float(draft.get("pdu_rated_capacity_kw"))
    pdu_plug_type = draft.get("pdu_plug_type")
    pdu_monitoring_protocol = draft.get("pdu_monitoring_protocol")
    pdu_monitoring_endpoint = draft.get("pdu_monitoring_endpoint")

    if pdu_rated_capacity_kw is not None:
        if pdu_rated_capacity_kw < 0:
            errors.append(
                ValidationIssue(
                    field="pdu_rated_capacity_kw",
                    code="invalid_rated_capacity",
                    message="Rated capacity must be >= 0.",
                )
            )
        else:
            issue = _check_numeric_bounds(
                pdu_rated_capacity_kw,
                maximum=PDU_RATED_CAPACITY_MAX,
                field="pdu_rated_capacity_kw",
                code="invalid_rated_capacity",
            )
            if issue is not None:
                errors.append(issue)
    if pdu_plug_type is not None and not str(pdu_plug_type).strip():
        errors.append(
            ValidationIssue(field="pdu_plug_type", code="invalid_plug_type", message="Plug type must not be empty.")
        )
    # Monitoring protocol and endpoint describe the catalog's monitoring capability; they
    # are configured together and are distinct from any runtime monitoring credentials.
    monitoring_configured = pdu_monitoring_protocol is not None or pdu_monitoring_endpoint is not None
    if monitoring_configured and not (pdu_monitoring_protocol and pdu_monitoring_endpoint):
        errors.append(
            ValidationIssue(
                field="pdu_monitoring_endpoint",
                code="invalid_monitoring",
                message="Monitoring protocol and endpoint must be configured together.",
            )
        )
    if pdu_monitoring_endpoint is not None and not str(pdu_monitoring_endpoint).strip():
        errors.append(
            ValidationIssue(
                field="pdu_monitoring_endpoint",
                code="invalid_monitoring",
                message="Monitoring endpoint must not be empty.",
            )
        )

    return ValidationSummary(valid=not errors, errors=errors, warnings=warnings)
