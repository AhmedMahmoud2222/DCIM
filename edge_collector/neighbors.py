"""LLDP / CDP neighbor collection adapters.

The adapters are table-driven: which OIDs to walk, which column sub-identifiers mean what
and how the row index is laid out all come from the resolved profile plan
(`GET /collectors/{id}/discovery-plan`, i.e. VendorProfile.neighbor_discovery), never from
constants in this module. What *is* encoded here is protocol semantics that no profile
should override: how IEEE LLDP-MIB / CISCO-CDP-MIB values are decoded (ID subtypes,
capability bit layouts, address encodings).

Robustness rules:

* Every device-supplied string is bounded, stripped of control characters and decoded with
  replacement, so no byte sequence can break JSON encoding, logs or the central database.
* A malformed row is skipped and counted; it never aborts the scan of its table, and a
  failing table/protocol never aborts the other protocol. The scan reports `complete` only
  when every walk finished untruncated, which is the only condition under which Central is
  allowed to treat unseen neighbors as gone.
* Only the exception class of a failed walk is reported, never exception text.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .queue import QueueRecord
from .snmp import SNMPError
from .snmp_wire import TAG_OCTET_STRING, Varbind, WalkResult, oid_in_subtree

MAX_TEXT = 255
MAX_ROWS = 4_096
MAX_RAW_FIELD = 128
_OID = re.compile(r"^[0-2](\.(0|[1-9][0-9]{0,9})){1,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

LLDP_CAPABILITY_BITS = ("other", "repeater", "bridge", "wlan_access_point", "router", "telephone", "docsis_cable_device", "station_only")
CDP_CAPABILITY_BITS = (
    (0x01, "router"), (0x02, "transparent_bridge"), (0x04, "source_route_bridge"), (0x08, "switch"), (0x10, "host"),
    (0x20, "igmp"), (0x40, "repeater"),
)
LLDP_CHASSIS_SUBTYPES = {
    1: "chassis_component", 2: "interface_alias", 3: "port_component", 4: "mac_address", 5: "network_address",
    6: "interface_name", 7: "local",
}
LLDP_PORT_SUBTYPES = {
    1: "interface_alias", 2: "port_component", 3: "mac_address", 4: "network_address", 5: "interface_name",
    6: "agent_circuit_id", 7: "local",
}
_LOCAL_PORT_PRIORITY = {
    "lldp_loc_port_id": ("port_id", "port_desc"),
    "if_name": ("if_name", "port_id", "port_desc"),
    "if_descr": ("if_descr", "port_desc", "port_id"),
}


class NeighborSpecError(ValueError):
    """The profile plan's neighbor section is unusable."""


class Walker(Protocol):
    def walk(self, base_oid: str, *, max_rows: int = ...) -> WalkResult: ...


@dataclass(frozen=True, slots=True)
class ProtocolSpec:
    table_oid: str
    columns: Mapping[str, int]
    index_fields: tuple[str, ...]
    local_port_table_oid: str | None = None
    local_port_columns: Mapping[str, int] = field(default_factory=dict)
    management_address_table_oid: str | None = None

    @classmethod
    def from_plan(cls, raw: object) -> ProtocolSpec:
        if not isinstance(raw, dict) or not raw.get("enabled"):
            raise NeighborSpecError("neighbor protocol is not enabled")
        table = raw.get("table_oid")
        if not isinstance(table, str) or not _OID.match(table):
            raise NeighborSpecError("table_oid is invalid")
        columns = _columns(raw.get("columns"))
        if not columns:
            raise NeighborSpecError("columns are required")
        index_fields = raw.get("index_fields") or []
        if not isinstance(index_fields, list) or not all(isinstance(i, str) and _NAME.match(i) for i in index_fields):
            raise NeighborSpecError("index_fields are invalid")
        optional = {}
        for key in ("local_port_table_oid", "management_address_table_oid"):
            value = raw.get(key)
            if value is not None and (not isinstance(value, str) or not _OID.match(value)):
                raise NeighborSpecError(f"{key} is invalid")
            optional[key] = value
        return cls(
            table_oid=table, columns=columns, index_fields=tuple(index_fields),
            local_port_table_oid=optional["local_port_table_oid"], local_port_columns=_columns(raw.get("local_port_columns")),
            management_address_table_oid=optional["management_address_table_oid"],
        )


def _columns(raw: object) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 64:
        raise NeighborSpecError("columns are invalid")
    out: dict[str, int] = {}
    for name, sub_id in raw.items():
        if not isinstance(name, str) or not _NAME.match(name) or not isinstance(sub_id, int) or not 1 <= sub_id <= 255:
            raise NeighborSpecError("columns are invalid")
        out[name] = sub_id
    return out


# --------------------------------------------------------------------------- sanitizers
def clean_text(raw: bytes | str, limit: int = MAX_TEXT) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return _CONTROL.sub("", text).strip()[:limit]


def _hex(raw: bytes) -> str:
    return raw[:64].hex()


def format_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def render_network_address(raw: bytes) -> str | None:
    """IANA address-family-prefixed address (LLDP networkAddress)."""
    try:
        if len(raw) == 5 and raw[0] == 1:
            return str(ipaddress.IPv4Address(raw[1:]))
        if len(raw) == 17 and raw[0] == 2:
            return str(ipaddress.IPv6Address(raw[1:]))
    except ValueError:
        return None
    return None


def render_lldp_id(subtype: int | None, raw: bytes, names: Mapping[int, str]) -> tuple[str, str]:
    kind = names.get(subtype, "unknown") if subtype is not None else "unknown"
    if kind == "mac_address" and len(raw) == 6:
        return format_mac(raw), kind
    if kind == "network_address":
        address = render_network_address(raw)
        if address:
            return address, kind
    if kind in ("interface_alias", "interface_name", "local", "port_component", "chassis_component", "agent_circuit_id"):
        try:
            text = clean_text(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _hex(raw), kind
        if text and text.isprintable():
            return text, kind
    return _hex(raw), kind if kind != "unknown" else "unknown"


def lldp_capabilities(raw: bytes) -> list[str]:
    if not raw:
        return []
    first = raw[0]
    return [name for bit, name in enumerate(LLDP_CAPABILITY_BITS) if first & (0x80 >> bit)]


def cdp_capabilities(raw: bytes) -> list[str]:
    if not raw or len(raw) > 4:
        return []
    value = int.from_bytes(raw, "big")
    return [name for mask, name in CDP_CAPABILITY_BITS if value & mask]


def render_cdp_address(address_type: int | None, raw: bytes) -> str | None:
    try:
        if len(raw) == 4:
            return str(ipaddress.IPv4Address(raw))
        if len(raw) == 16:
            return str(ipaddress.IPv6Address(raw))
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- scanning
@dataclass(frozen=True, slots=True)
class NeighborScan:
    protocol: str
    observations: tuple[dict[str, Any], ...]
    malformed_rows: int
    complete: bool
    failure: str | None = None


def _suffix(oid: str, base: str) -> list[int] | None:
    if not oid_in_subtree(oid, base) or oid == base:
        return None
    try:
        return [int(part) for part in oid[len(base) + 1:].split(".")]
    except ValueError:
        return None


def _group_rows(
    rows: tuple[Varbind, ...], spec_table: str, columns: Mapping[str, int], index_length: int,
) -> tuple[dict[tuple[int, ...], dict[str, Varbind]], int]:
    by_subid = {sub_id: name for name, sub_id in columns.items()}
    grouped: dict[tuple[int, ...], dict[str, Varbind]] = {}
    malformed = 0
    for binding in rows:
        parts = _suffix(binding.oid, spec_table)
        if parts is None or len(parts) != 1 + index_length:
            malformed += 1
            continue
        name = by_subid.get(parts[0])
        if name is None:
            continue
        grouped.setdefault(tuple(parts[1:]), {})[name] = binding
    return grouped, malformed


def _octets(binding: Varbind | None) -> bytes | None:
    if binding is None or binding.is_exception or binding.tag != TAG_OCTET_STRING:
        return None
    return binding.value


def _int(binding: Varbind | None) -> int | None:
    if binding is None or binding.is_exception:
        return None
    try:
        return binding.as_int()
    except SNMPError:
        return None


def _local_ports(walker: Walker, spec: ProtocolSpec) -> dict[int, dict[str, str]]:
    if not spec.local_port_table_oid or not spec.local_port_columns:
        return {}
    result = walker.walk(spec.local_port_table_oid, max_rows=MAX_ROWS)
    grouped, _bad = _group_rows(result.rows, spec.local_port_table_oid, spec.local_port_columns, 1)
    ports: dict[int, dict[str, str]] = {}
    for (index,), columns in grouped.items():
        entry: dict[str, str] = {}
        for name, binding in columns.items():
            raw = _octets(binding)
            if raw is None:
                continue
            if name == "port_id" and "port_id_subtype" in columns:
                text = render_lldp_id(_int(columns["port_id_subtype"]), raw, LLDP_PORT_SUBTYPES)[0]
            else:
                text = clean_text(raw)
            if text:
                entry[name] = text
        if entry:
            ports[index] = entry
    return ports


def _pick_local_name(entry: Mapping[str, str], priority: tuple[str, ...]) -> str | None:
    return next((entry[name] for name in priority if name in entry), None)


def scan_lldp(walker: Walker, plan: Mapping[str, Any]) -> NeighborScan:
    try:
        spec = ProtocolSpec.from_plan((plan.get("neighbor_discovery") or {}).get("lldp"))
    except NeighborSpecError:
        return NeighborScan("lldp", (), 0, False, failure="NeighborSpecError")
    behavior = ((plan.get("neighbor_behavior") or {}).get("lldp")) or {}
    priority = _LOCAL_PORT_PRIORITY.get(behavior.get("local_port_source", "lldp_loc_port_id"), _LOCAL_PORT_PRIORITY["lldp_loc_port_id"])
    try:
        main = walker.walk(spec.table_oid, max_rows=MAX_ROWS)
        local_ports = _local_ports(walker, spec)
        addresses = _lldp_management_addresses(walker, spec) if behavior.get("management_address", True) else {}
    except SNMPError as error:
        return NeighborScan("lldp", (), 0, False, failure=type(error).__name__)

    grouped, malformed = _group_rows(main.rows, spec.table_oid, spec.columns, 3)
    complete = not main.truncated
    observations: list[dict[str, Any]] = []
    for (time_mark, local_num, rem_index), columns in grouped.items():
        chassis_raw, port_raw = _octets(columns.get("chassis_id")), _octets(columns.get("port_id"))
        if not chassis_raw or not port_raw:
            malformed += 1
            continue
        chassis, chassis_kind = render_lldp_id(_int(columns.get("chassis_id_subtype")), chassis_raw, LLDP_CHASSIS_SUBTYPES)
        port, port_kind = render_lldp_id(_int(columns.get("port_id_subtype")), port_raw, LLDP_PORT_SUBTYPES)
        local_name = _pick_local_name(local_ports.get(local_num, {}), priority)
        observations.append({
            "protocol": "lldp",
            "local_port": {"name": local_name, "ref": str(local_num)},
            "remote": {
                "chassis_id": chassis, "chassis_id_subtype": chassis_kind, "port_id": port, "port_id_subtype": port_kind,
                "port_description": _text(columns.get("port_desc")), "system_name": _text(columns.get("sys_name")),
                "system_description": _text(columns.get("sys_desc"), 512), "platform": None,
                "management_address": addresses.get((time_mark, local_num, rem_index)),
            },
            "capabilities": lldp_capabilities(_octets(columns.get("sys_cap_enabled")) or b""),
            "native_vlan": None,
            "ttl_seconds": None,
            "raw": _raw(columns),
        })
    return NeighborScan("lldp", tuple(observations), malformed, complete)


def _lldp_management_addresses(walker: Walker, spec: ProtocolSpec) -> dict[tuple[int, int, int], str]:
    if not spec.management_address_table_oid:
        return {}
    result = walker.walk(spec.management_address_table_oid, max_rows=MAX_ROWS)
    found: dict[tuple[int, int, int], str] = {}
    for binding in result.rows:
        parts = _suffix(binding.oid, spec.management_address_table_oid)
        # column . timeMark . localPort . remIndex . addrSubtype . addrLen . address octets...
        if parts is None or len(parts) < 6:
            continue
        key, subtype, length = (parts[1], parts[2], parts[3]), parts[4], parts[5]
        octets = parts[6:]
        if len(octets) != length or any(not 0 <= o <= 255 for o in octets):
            continue
        address = render_network_address(bytes([subtype]) + bytes(octets))
        if address and (key not in found or ":" in found[key]):  # prefer IPv4 over IPv6
            found[key] = address
    return found


def scan_cdp(walker: Walker, plan: Mapping[str, Any]) -> NeighborScan:
    try:
        spec = ProtocolSpec.from_plan((plan.get("neighbor_discovery") or {}).get("cdp"))
    except NeighborSpecError:
        return NeighborScan("cdp", (), 0, False, failure="NeighborSpecError")
    behavior = ((plan.get("neighbor_behavior") or {}).get("cdp")) or {}
    priority = ("if_name", "if_alias")
    try:
        main = walker.walk(spec.table_oid, max_rows=MAX_ROWS)
        local_ports = _local_ports(walker, spec)
    except SNMPError as error:
        return NeighborScan("cdp", (), 0, False, failure=type(error).__name__)

    grouped, malformed = _group_rows(main.rows, spec.table_oid, spec.columns, 2)
    complete = not main.truncated
    observations: list[dict[str, Any]] = []
    for (if_index, _device_index), columns in grouped.items():
        device_raw, port_raw = _octets(columns.get("device_id")), _octets(columns.get("device_port"))
        device_id = clean_text(device_raw) if device_raw else ""
        remote_port = clean_text(port_raw) if port_raw else ""
        if not device_id or not remote_port:
            malformed += 1
            continue
        if behavior.get("device_id_normalization") == "strip_domain" and "." in device_id and not _looks_like_address(device_id):
            device_id = device_id.split(".", 1)[0]
        address_raw = _octets(columns.get("address"))
        local_name = _pick_local_name(local_ports.get(if_index, {}), priority)
        observations.append({
            "protocol": "cdp",
            "local_port": {"name": local_name, "ref": str(if_index)},
            "remote": {
                "chassis_id": device_id, "chassis_id_subtype": "cdp_device_id", "port_id": remote_port,
                "port_id_subtype": "interface_name", "port_description": None, "system_name": device_id,
                "system_description": _text(columns.get("version"), 512), "platform": _text(columns.get("platform")),
                "management_address": render_cdp_address(_int(columns.get("address_type")), address_raw) if address_raw else None,
            },
            "capabilities": cdp_capabilities(_octets(columns.get("capabilities")) or b""),
            "native_vlan": _vlan(_int(columns.get("native_vlan"))),
            "ttl_seconds": None,
            "raw": _raw(columns),
        })
    return NeighborScan("cdp", tuple(observations), malformed, complete)


def _looks_like_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _vlan(value: int | None) -> int | None:
    return value if value is not None and 1 <= value <= 4094 else None


def _text(binding: Varbind | None, limit: int = MAX_TEXT) -> str | None:
    raw = _octets(binding)
    if raw is None:
        return None
    return clean_text(raw, limit) or None


MAX_RAW_BYTES = 3_000


def _raw(columns: Mapping[str, Varbind]) -> dict[str, str | int]:
    """Bounded, printable-or-hex copy of the protocol columns kept as evidence."""
    out: dict[str, str | int] = {}
    for name, binding in sorted(columns.items()):
        if binding.is_exception:
            continue
        if binding.tag == TAG_OCTET_STRING:
            text = clean_text(binding.value, MAX_RAW_FIELD)
            out[name] = text if text and text.isprintable() and "�" not in text else "hex:" + _hex(binding.value[:MAX_RAW_FIELD])
        else:
            value = _int(binding)
            if value is not None:
                out[name] = value
    while len(json.dumps(out, sort_keys=True)) > MAX_RAW_BYTES and out:
        out.pop(max(out, key=lambda key: len(str(out[key]))))
    return out


def collect_neighbors(walker: Walker, plan: Mapping[str, Any]) -> list[NeighborScan]:
    """Scan every neighbor protocol the plan enables. One protocol failing is isolated."""
    scans: list[NeighborScan] = []
    behavior = plan.get("neighbor_behavior") or {}
    for protocol, scanner in (("lldp", scan_lldp), ("cdp", scan_cdp)):
        if not (behavior.get(protocol) or {}).get("enabled"):
            continue
        try:
            scans.append(scanner(walker, plan))
        except Exception as error:  # noqa: BLE001 - a parser bug must not stop the other protocol or the poll
            scans.append(NeighborScan(protocol, (), 0, False, failure=type(error).__name__))
    return scans


# Central rejects a `raw_attributes` blob whose default `json.dumps` form exceeds 8192 bytes. Hostile
# peers can inflate the form with astral or undecodable characters (6-12 escaped bytes each), so the
# edge enforces the bound on exactly that serialization, with margin, before anything is queued.
MAX_ATTRIBUTES_BYTES = 7_000
_SHRINK_STEPS: tuple[tuple[str, ...], ...] = (
    ("raw",), ("system_description",), ("port_description",), ("platform",), ("system_name",),
)


def _attributes_size(attributes: Mapping[str, Any]) -> int:
    return len(json.dumps(attributes))


def fit_observation(observation: Mapping[str, Any], *, scan_id: str) -> dict[str, Any] | None:
    """Return the `raw_attributes` for one neighbor, shrunk to the serialized bound.

    Evidence is dropped in order of least value (raw columns, then free text). Identity
    fields are never altered, so a neighbor that still does not fit returns None and the caller
    counts it as malformed instead of emitting a record Central would refuse.
    """
    candidate: dict[str, Any] = {**observation, "remote": dict(observation["remote"])}
    attributes = {"scan_id": scan_id, "neighbor": candidate}
    for step in ((), *_SHRINK_STEPS):
        for key in step:
            if key == "raw":
                candidate["raw"] = {}
            else:
                candidate["remote"][key] = None
        if _attributes_size(attributes) <= MAX_ATTRIBUTES_BYTES:
            return attributes
    return None


def build_queue_records(
    scans: list[NeighborScan], *, integration_id: str, external_identifier: str, scan_id: str,
    scan_started_at: datetime, scan_finished_at: datetime,
) -> list[QueueRecord]:
    """One queue record per neighbor plus one marker per successfully walked protocol.

    Neighbor records carry the scan start as `occurred_at`; the marker carries the finish
    time, so the queue's `(occurred_at, record_id)` ordering delivers neighbors first. The
    marker's `complete` flag is what lets Central age out neighbors this scan did not see.
    """
    records: list[QueueRecord] = []
    for scan in scans:
        if scan.failure is not None:
            continue
        oversize = 0
        for observation in scan.observations:
            attributes = fit_observation(observation, scan_id=scan_id)
            if attributes is None:
                oversize += 1
                continue
            identity = "|".join((
                integration_id, scan_id, scan.protocol, str(observation["local_port"].get("ref")),
                observation["remote"]["chassis_id"], observation["remote"]["port_id"],
            ))
            records.append(QueueRecord(
                record_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                occurred_at=scan_started_at,
                payload={
                    "integration_id": integration_id, "external_identifier": external_identifier,
                    "record_type": "neighbor", "raw_attributes": attributes,
                },
            ))
        records.append(QueueRecord(
            record_id=hashlib.sha256(f"{integration_id}|{scan_id}|{scan.protocol}|marker".encode()).hexdigest()[:32],
            occurred_at=scan_finished_at,
            payload={
                "integration_id": integration_id, "external_identifier": external_identifier,
                "record_type": "neighbor_scan",
                "raw_attributes": {
                    "scan_id": scan_id, "protocol": scan.protocol, "scan_started_at": scan_started_at.isoformat(),
                    "complete": scan.complete and not oversize, "observed_count": len(scan.observations) - oversize,
                    "malformed_rows": scan.malformed_rows + oversize,
                },
            },
        ))
    return records
