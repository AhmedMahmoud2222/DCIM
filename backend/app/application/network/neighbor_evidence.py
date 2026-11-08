"""Validation and normalization of a collector-reported neighbor observation.

The collector already sanitizes what it reads from the network; Central does not trust that.
Every field is re-validated, bounded and normalized here, and the deterministic
`identity_key` (which makes ingestion idempotent) is derived from the normalized values
only. A payload that cannot be identified (no local port, no remote chassis/port) raises
`InvalidNeighborPayload`, which the ingest endpoint reports as a permanent `INVALID_PAYLOAD`
rejection rather than storing something ambiguous.
"""

import hashlib
import ipaddress
import re
import uuid
from dataclasses import dataclass

NEIGHBOR_PROTOCOLS = ("lldp", "cdp")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAC = re.compile(r"^(?:[0-9a-f]{2}[:\-.]?){5}[0-9a-f]{2}$|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.IGNORECASE)
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RAW_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_RAW_BYTES = 4_096
MAX_CAPABILITIES = 16


class InvalidNeighborPayload(ValueError):
    """The observation is unusable. The message is fixed text, never device-supplied data."""


def clean(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidNeighborPayload("text field has the wrong type")
    text = _CONTROL.sub("", value).strip()
    return text[:limit] or None


def normalize_mac(value: str) -> str | None:
    """aa:bb:cc:dd:ee:ff, from any common MAC notation; None when it is not a MAC."""
    if not _MAC.match(value):
        return None
    digits = re.sub(r"[^0-9a-f]", "", value.lower())
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)) if len(digits) == 12 else None


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold()).rstrip(".")


def normalize_address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


@dataclass(frozen=True)
class NeighborEvidence:
    protocol: str
    local_port_name: str | None
    local_port_ref: str | None
    remote_chassis_ident: str
    remote_chassis_subtype: str | None
    remote_port_ident: str
    remote_port_subtype: str | None
    remote_port_description: str | None
    remote_system_name: str | None
    remote_system_description: str | None
    remote_platform: str | None
    remote_management_address: str | None
    capabilities: list[str]
    native_vlan: int | None
    ttl_seconds: int | None
    raw: dict

    def identity_key(self, integration_id: uuid.UUID) -> str:
        local = normalize_name(self.local_port_name) if self.local_port_name else f"ref:{self.local_port_ref}"
        chassis = normalize_mac(self.remote_chassis_ident) or normalize_name(self.remote_chassis_ident)
        port = normalize_mac(self.remote_port_ident) or normalize_name(self.remote_port_ident)
        material = "\x1f".join((str(integration_id), self.protocol, local, chassis, port))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _bounded_int(value: object, low: int, high: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None


def parse_neighbor(raw_attributes: dict) -> NeighborEvidence:
    """`raw_attributes` is the ingest record's attribute document: {"scan_id", "neighbor": {...}}."""
    neighbor = raw_attributes.get("neighbor")
    if not isinstance(neighbor, dict):
        raise InvalidNeighborPayload("neighbor document is missing")
    protocol = neighbor.get("protocol")
    if protocol not in NEIGHBOR_PROTOCOLS:
        raise InvalidNeighborPayload("unsupported neighbor protocol")
    local = neighbor.get("local_port")
    remote = neighbor.get("remote")
    if not isinstance(local, dict) or not isinstance(remote, dict):
        raise InvalidNeighborPayload("local_port and remote documents are required")

    local_name = clean(local.get("name"), 255)
    local_ref = clean(local.get("ref"), 64)
    if local_name is None and local_ref is None:
        raise InvalidNeighborPayload("the local port is not identified")
    chassis = clean(remote.get("chassis_id"), 255)
    port = clean(remote.get("port_id"), 255)
    if chassis is None or port is None:
        raise InvalidNeighborPayload("the remote chassis and port are required")

    subtype_chassis = clean(remote.get("chassis_id_subtype"), 32)
    subtype_port = clean(remote.get("port_id_subtype"), 32)
    capabilities_in = neighbor.get("capabilities")
    capabilities_in = [] if capabilities_in is None else capabilities_in
    if not isinstance(capabilities_in, list):
        raise InvalidNeighborPayload("capabilities must be a list")
    capabilities = sorted({c for c in capabilities_in[:64] if isinstance(c, str) and _TOKEN.match(c)})[:MAX_CAPABILITIES]

    raw_in = neighbor.get("raw")
    raw_in = {} if raw_in is None else raw_in
    if not isinstance(raw_in, dict):
        raise InvalidNeighborPayload("raw evidence must be an object")
    raw: dict = {}
    for key, value in list(raw_in.items())[:64]:
        if isinstance(key, str) and _RAW_KEY.match(key):
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                raw[key] = value
            elif isinstance(value, str):
                raw[key] = (clean(value, 256) or "")
    while len(str(raw)) > MAX_RAW_BYTES and raw:
        raw.pop(max(raw, key=lambda k: len(str(raw[k]))))

    return NeighborEvidence(
        protocol=protocol, local_port_name=local_name, local_port_ref=local_ref, remote_chassis_ident=chassis,
        remote_chassis_subtype=subtype_chassis, remote_port_ident=port, remote_port_subtype=subtype_port,
        remote_port_description=clean(remote.get("port_description"), 255),
        remote_system_name=clean(remote.get("system_name"), 255),
        remote_system_description=clean(remote.get("system_description"), 512),
        remote_platform=clean(remote.get("platform"), 255),
        remote_management_address=normalize_address(remote.get("management_address")),
        capabilities=capabilities, native_vlan=_bounded_int(neighbor.get("native_vlan"), 1, 4094),
        ttl_seconds=_bounded_int(neighbor.get("ttl_seconds"), 0, 65_535), raw=raw,
    )
