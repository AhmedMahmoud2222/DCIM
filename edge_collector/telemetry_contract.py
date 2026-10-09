"""Telemetry conversion-contract pinning for the Edge Collector (Issue #128 / G1).

Scope, stated precisely: the packaged collector today acquires and queues *discovery* observations only; it has no
telemetry acquisition or telemetry delivery loop, and this module does not add one. It defines and tests the
collector-side half of the wire contract that Central's `POST /collectors/{id}/telemetry` now enforces:

  1. `ContractBook` parses the authenticated `GET /collectors/{id}/telemetry-contracts` plan.
  2. `telemetry_record_payload` copies the revision a value was acquired under onto the record at acquisition,
     so the pin sits in the durable queue payload and is replayed unchanged, however long the record waits and
     whatever Central's mapping looks like on delivery. It refuses to build an unpinned record.
  3. `classify_telemetry_ack` says which Central rejections are final and which must stay queued.

A future telemetry acquisition loop plugs into these three functions; they are exercised end to end against Central
in backend/tests/api/test_telemetry_mapping_revisions_api.py.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_HASH = re.compile(r"^[0-9a-f]{64}$")

# Central keeps these records (hold table) and expects the collector to keep trying with its normal backoff.
RETRYABLE_TELEMETRY_ERRORS = frozenset({
    "AMBIGUOUS_MAPPING_CONTRACT",  # held for bounded retry, then operator resolution
    "CONVERSION_CONTRACT_DRIFT",   # needs a Central fix; the record itself is fine
    "UNKNOWN_METRIC_MAPPING",      # mapping not created yet
    "NOT_ASSIGNED",                # assignment not propagated yet
})
# Retrying can never succeed (or Central has stopped asking and retains the record itself).
PERMANENT_TELEMETRY_ERRORS = frozenset({
    "UNKNOWN_MAPPING_REVISION",
    "MAPPING_REVISION_MISMATCH",
    "CONTRACT_HOLD_EXPIRED",
    "INVALID_TELEMETRY_VALUE",
    "INCOMPATIBLE_TELEMETRY_UNITS",
})


class ContractPlanError(ValueError):
    """The central contract plan is malformed."""


class MissingContractError(LookupError):
    """No pinned contract is known for this source; the value must not be queued."""


@dataclass(frozen=True, slots=True)
class ContractPin:
    mapping_revision_id: str
    revision: int
    source_unit: str
    source_scale: str
    conversion_hash: str


@dataclass(frozen=True, slots=True)
class ContractBook:
    pins: Mapping[tuple[str, str], ContractPin]

    @classmethod
    def from_plan(cls, raw: object) -> ContractBook:
        if not isinstance(raw, list):
            raise ContractPlanError("contract plan must be a list")
        pins: dict[tuple[str, str], ContractPin] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ContractPlanError("contract plan entry must be an object")
            try:
                integration_id = str(uuid.UUID(str(item["integration_id"])))
                revision_id = str(uuid.UUID(str(item["mapping_revision_id"])))
                source_identifier = item["source_identifier"]
                revision = item["revision"]
                source_unit = item["source_unit"]
                source_scale = item["source_scale"]
                conversion_hash = item["conversion_hash"]
            except (KeyError, ValueError) as error:
                raise ContractPlanError(f"contract plan entry is invalid: {error!r}") from error
            if (
                not isinstance(source_identifier, str) or not source_identifier
                or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
                or not isinstance(source_unit, str) or not source_unit
                or not isinstance(source_scale, str)
                or not isinstance(conversion_hash, str) or not _HASH.match(conversion_hash)
            ):
                raise ContractPlanError("contract plan entry has an invalid field")
            key = (integration_id, source_identifier)
            if key in pins:
                raise ContractPlanError("contract plan lists a source twice")
            pins[key] = ContractPin(revision_id, revision, source_unit, source_scale, conversion_hash)
        return cls(pins)

    def pin_for(self, integration_id: str, source_identifier: str) -> ContractPin | None:
        return self.pins.get((str(uuid.UUID(str(integration_id))), source_identifier))


def telemetry_record_payload(
    book: ContractBook, *, integration_id: str, source_identifier: str, external_identifier: str,
    value: float, attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The queue payload of one acquired telemetry value, pinned to the contract known at acquisition time."""
    pin = book.pin_for(integration_id, source_identifier)
    if pin is None:
        raise MissingContractError(f"no conversion contract for source {source_identifier!r}")
    return {
        "integration_id": str(uuid.UUID(str(integration_id))),
        "source_identifier": source_identifier,
        "external_identifier": external_identifier,
        "value": value,
        "attributes": dict(attributes or {}),
        "mapping_revision_id": pin.mapping_revision_id,
        "source_unit": pin.source_unit,
        "source_scale": pin.source_scale,
    }


def classify_telemetry_ack(result: Mapping[str, Any]) -> Literal["acknowledge", "retry"]:
    """Whether the queue may drop a record after this per-record Central acknowledgement."""
    status = result.get("status")
    if status in {"accepted", "duplicate"}:
        return "acknowledge"
    if status != "rejected":
        return "retry"
    error = result.get("error")
    return "acknowledge" if error in PERMANENT_TELEMETRY_ERRORS else "retry"
