"""Deterministic profile matching.

Pure functions over plain views of the stored profiles, so the same rules run in the API
(dry-run `POST /network-profiles/match`), in discovery ingestion and in unit tests without
a database.

Rules, in order:

1. Vendor: every active vendor whose `sys_object_id_prefixes` contain an arc-boundary
   prefix of the device's sysObjectID is a candidate; the longest matching prefix wins.
   Two different vendors tying on the longest prefix is `ambiguous` -- no vendor is chosen.
2. Device profile: among active profiles of the chosen vendor, a profile is eligible when
   every `match_criteria` entry holds and the firmware bounds (if any) contain the
   firmware fact. A firmware bound with an unknown/unparseable firmware fact makes the
   profile ineligible; it is never compared as text.
3. Ranking: `(number of criteria, longest sys_object_id specificity, priority)`. A unique
   top rank wins. Equal top ranks are `ambiguous`: all tied candidates are returned and
   none is selected. An administrator breaks the tie by adjusting `priority`/criteria or by
   binding a profile to the integration explicitly.
4. No eligible device profile for a matched vendor is `vendor_only`; nothing matched is
   `no_match`.
"""

import uuid
from dataclasses import dataclass, field

from app.application.network.profile_schema import compare_versions, oid_has_prefix, parse_version

MATCHED = "matched"
VENDOR_ONLY = "vendor_only"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no_match"
MATCH_STATES = (MATCHED, VENDOR_ONLY, AMBIGUOUS, NO_MATCH)


@dataclass(frozen=True)
class DeviceFacts:
    """What was observed about a device. Absent facts are None, never guessed."""

    sys_object_id: str | None = None
    sys_descr: str | None = None
    model: str | None = None
    hardware_revision: str | None = None
    firmware: str | None = None

    def get(self, name: str) -> str | None:
        return getattr(self, name, None)


@dataclass(frozen=True)
class VendorView:
    id: uuid.UUID
    code: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class DeviceView:
    id: uuid.UUID
    vendor_profile_id: uuid.UUID
    code: str
    criteria: tuple[tuple[str, str, str | tuple[str, ...]], ...]  # (field, op, value)
    firmware_min: str | None
    firmware_max: str | None
    priority: int


@dataclass(frozen=True)
class MatchResult:
    state: str
    vendor_id: uuid.UUID | None = None
    device_profile_id: uuid.UUID | None = None
    candidate_vendor_ids: tuple[uuid.UUID, ...] = ()
    candidate_device_profile_ids: tuple[uuid.UUID, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "vendor_profile_id": str(self.vendor_id) if self.vendor_id else None,
            "device_profile_id": str(self.device_profile_id) if self.device_profile_id else None,
            "candidate_vendor_profile_ids": [str(i) for i in self.candidate_vendor_ids],
            "candidate_device_profile_ids": [str(i) for i in self.candidate_device_profile_ids],
            "reasons": list(self.reasons),
        }


def match_vendor(facts: DeviceFacts, vendors: list[VendorView]) -> tuple[list[VendorView], int]:
    """Returns (vendors tied on the longest matching prefix, that prefix's arc count)."""
    sys_object_id = facts.sys_object_id
    if not sys_object_id:
        return [], 0
    best: dict[uuid.UUID, tuple[VendorView, int]] = {}
    for vendor in vendors:
        for prefix in vendor.prefixes:
            if oid_has_prefix(sys_object_id, prefix):
                arcs = prefix.count(".") + 1
                if vendor.id not in best or arcs > best[vendor.id][1]:
                    best[vendor.id] = (vendor, arcs)
    if not best:
        return [], 0
    longest = max(arcs for _vendor, arcs in best.values())
    return [vendor for vendor, arcs in best.values() if arcs == longest], longest


def _criterion_holds(facts: DeviceFacts, field_name: str, op: str, value: str | tuple[str, ...]) -> bool:
    observed = facts.get(field_name)
    if observed is None:
        return False
    values = list(value) if isinstance(value, tuple | list) else [value]
    if field_name == "sys_object_id":
        if op == "equals":
            return observed == values[0]
        if op == "prefix":
            return oid_has_prefix(observed, values[0])
        if op == "in":
            return observed in values
        return False
    observed_folded = observed.casefold()
    folded = [item.casefold() for item in values]
    if op == "equals":
        return observed_folded == folded[0]
    if op == "prefix":
        return observed_folded.startswith(folded[0])
    if op == "contains":
        return folded[0] in observed_folded
    if op == "in":
        return observed_folded in folded
    return False


def _firmware_ok(facts: DeviceFacts, device: DeviceView) -> bool:
    if device.firmware_min is None and device.firmware_max is None:
        return True
    if facts.firmware is None:
        return False
    try:
        observed = parse_version(facts.firmware)
        if device.firmware_min is not None and compare_versions(observed, parse_version(device.firmware_min)) < 0:
            return False
        if device.firmware_max is not None and compare_versions(observed, parse_version(device.firmware_max)) > 0:
            return False
    except ValueError:
        return False
    return True


def _specificity(device: DeviceView) -> int:
    longest = 0
    for field_name, op, value in device.criteria:
        if field_name == "sys_object_id":
            values = list(value) if isinstance(value, tuple | list) else [value]
            longest = max(longest, max(item.count(".") + 1 for item in values))
            if op == "equals":
                longest += 1000  # an exact sysObjectID outranks any prefix
    return longest


def match_device_profiles(facts: DeviceFacts, devices: list[DeviceView]) -> list[tuple[DeviceView, tuple[int, int, int]]]:
    eligible: list[tuple[DeviceView, tuple[int, int, int]]] = []
    for device in devices:
        if not all(_criterion_holds(facts, f, op, v) for f, op, v in device.criteria):
            continue
        if not _firmware_ok(facts, device):
            continue
        eligible.append((device, (len(device.criteria), _specificity(device), device.priority)))
    eligible.sort(key=lambda item: item[1], reverse=True)
    return eligible


def resolve(facts: DeviceFacts, vendors: list[VendorView], devices: list[DeviceView]) -> MatchResult:
    tied_vendors, _arcs = match_vendor(facts, vendors)
    if not tied_vendors:
        return MatchResult(NO_MATCH, reasons=("no vendor profile matches the sysObjectID",))
    if len(tied_vendors) > 1:
        return MatchResult(
            AMBIGUOUS, candidate_vendor_ids=tuple(v.id for v in tied_vendors),
            reasons=("multiple vendor profiles match the sysObjectID with equal specificity",),
        )
    vendor = tied_vendors[0]
    eligible = match_device_profiles(facts, [d for d in devices if d.vendor_profile_id == vendor.id])
    if not eligible:
        return MatchResult(VENDOR_ONLY, vendor_id=vendor.id, reasons=("no device profile is eligible for this vendor",))
    top_rank = eligible[0][1]
    tied = [device for device, rank in eligible if rank == top_rank]
    if len(tied) > 1:
        return MatchResult(
            AMBIGUOUS, vendor_id=vendor.id, candidate_device_profile_ids=tuple(d.id for d in tied),
            reasons=("multiple device profiles match with equal rank; adjust priority or bind a profile explicitly",),
        )
    return MatchResult(MATCHED, vendor_id=vendor.id, device_profile_id=tied[0].id)
