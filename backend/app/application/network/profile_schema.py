"""Validated shapes for the JSON columns of `VendorProfile`/`DeviceProfile`.

Everything vendor-specific lives in data validated here, never in service code. The
models in this module are the only gate between an administrator-supplied document and
the JSONB column, so they are strict (`extra="forbid"`, bounded sizes, OID syntax) and
they refuse anything that looks like a secret.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OID_PATTERN = re.compile(r"^[0-2](\.(0|[1-9][0-9]{0,9})){1,127}$")
KNOWN_PROTOCOLS = ("snmp", "icmp", "rest")
NEIGHBOR_PROTOCOLS = ("lldp", "cdp")
SNMP_VERSIONS = ("v1", "v2c", "v3")
DISCOVERY_OID_KEYS = (
    "sys_object_id", "sys_descr", "sys_name", "sys_uptime", "sys_contact", "sys_location",
    "entity_model", "entity_serial", "entity_firmware",
)
MATCH_FIELDS = ("sys_object_id", "sys_descr", "model", "hardware_revision")
MATCH_OPS = ("equals", "prefix", "contains", "in")
_SECRET_NAME = re.compile(r"(secret|password|passwd|community|credential|token|privkey|authkey)", re.IGNORECASE)
_VERSION_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+){0,5}$")

MAX_ITEMS = 64


def validate_oid(value: str) -> str:
    if not isinstance(value, str) or len(value) > 255 or not OID_PATTERN.match(value):
        raise ValueError(f"{value!r} is not a valid dotted OID")
    return value


def oid_has_prefix(oid: str, prefix: str) -> bool:
    """Prefix match at arc boundaries: 1.3.6.1.4.1.9 matches 1.3.6.1.4.1.9.1.5 but not
    1.3.6.1.4.1.99.1."""
    return oid == prefix or oid.startswith(prefix + ".")


def parse_version(value: str) -> tuple[int, ...]:
    """Numeric dotted version -> comparable tuple; raises ValueError otherwise (an
    unparseable firmware string must never be compared as text)."""
    if not isinstance(value, str) or not _VERSION_PATTERN.match(value.strip()):
        raise ValueError(f"{value!r} is not a numeric dotted version")
    return tuple(int(part) for part in value.strip().split("."))


def compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ neighbor discovery
class NeighborProtocolSpec(_Strict):
    """How one neighbor protocol is walked: the table, its column sub-identifiers, and
    which index components the row OID suffix carries. All OIDs are data."""

    enabled: bool = False
    table_oid: str | None = None
    columns: dict[str, int] = Field(default_factory=dict, max_length=MAX_ITEMS)
    index_fields: list[str] = Field(default_factory=list, max_length=8)
    local_port_table_oid: str | None = None
    management_address_table_oid: str | None = None

    @field_validator("table_oid", "local_port_table_oid", "management_address_table_oid")
    @classmethod
    def _oids(cls, v: str | None) -> str | None:
        return v if v is None else validate_oid(v)

    @field_validator("columns")
    @classmethod
    def _columns(cls, v: dict[str, int]) -> dict[str, int]:
        for name, sub_id in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ValueError(f"column name {name!r} is invalid")
            if not 1 <= sub_id <= 255:
                raise ValueError(f"column {name!r} sub-identifier must be 1..255")
        return v

    @field_validator("index_fields")
    @classmethod
    def _index_fields(cls, v: list[str]) -> list[str]:
        for name in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ValueError(f"index field {name!r} is invalid")
        return v

    @model_validator(mode="after")
    def _enabled_needs_table(self) -> "NeighborProtocolSpec":
        if self.enabled and (self.table_oid is None or not self.columns):
            raise ValueError("an enabled neighbor protocol requires table_oid and columns")
        return self


class NeighborDiscoverySpec(_Strict):
    lldp: NeighborProtocolSpec | None = None
    cdp: NeighborProtocolSpec | None = None

    def enabled_protocols(self) -> list[str]:
        return [name for name in NEIGHBOR_PROTOCOLS if (spec := getattr(self, name)) is not None and spec.enabled]


# ------------------------------------------------------------------ vendor profile
class VendorProfileContent(_Strict):
    """The mutable content of a vendor profile (identity `code` is immutable)."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    sys_object_id_prefixes: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    supported_protocols: list[str] = Field(default_factory=list, max_length=len(KNOWN_PROTOCOLS))
    discovery_oids: dict[str, str] = Field(default_factory=dict)
    neighbor_discovery: NeighborDiscoverySpec = Field(default_factory=NeighborDiscoverySpec)

    @field_validator("sys_object_id_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        cleaned = [validate_oid(item) for item in v]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("sys_object_id_prefixes must not repeat an OID")
        return cleaned

    @field_validator("supported_protocols")
    @classmethod
    def _protocols(cls, v: list[str]) -> list[str]:
        for item in v:
            if item not in KNOWN_PROTOCOLS:
                raise ValueError(f"unsupported protocol {item!r}; expected one of {KNOWN_PROTOCOLS}")
        if len(set(v)) != len(v):
            raise ValueError("supported_protocols must not repeat a protocol")
        return v

    @field_validator("discovery_oids")
    @classmethod
    def _discovery_oids(cls, v: dict[str, str]) -> dict[str, str]:
        for key, oid in v.items():
            if key not in DISCOVERY_OID_KEYS:
                raise ValueError(f"unknown discovery OID key {key!r}; expected one of {DISCOVERY_OID_KEYS}")
            validate_oid(oid)
        return v

    @model_validator(mode="after")
    def _neighbor_protocols_need_snmp(self) -> "VendorProfileContent":
        if self.neighbor_discovery.enabled_protocols() and "snmp" not in self.supported_protocols:
            raise ValueError("neighbor discovery is walked over SNMP; 'snmp' must be in supported_protocols")
        return self


# ------------------------------------------------------------------ device profile
class MatchCriterion(_Strict):
    field: Literal["sys_object_id", "sys_descr", "model", "hardware_revision"]
    op: Literal["equals", "prefix", "contains", "in"]
    value: str | list[str]

    @model_validator(mode="after")
    def _shape(self) -> "MatchCriterion":
        values = self.value if isinstance(self.value, list) else [self.value]
        if self.op == "in":
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= MAX_ITEMS:
                raise ValueError("op 'in' requires a list of 1..64 values")
        elif isinstance(self.value, list):
            raise ValueError(f"op {self.op!r} requires a single string value")
        for item in values:
            if not isinstance(item, str) or not 1 <= len(item) <= 256:
                raise ValueError("match values must be 1..256 character strings")
        if self.field == "sys_object_id":
            if self.op == "contains":
                raise ValueError("sys_object_id supports equals, prefix and in only")
            for item in values:
                validate_oid(item)
        return self


class DeviceCapabilities(_Strict):
    metrics: bool = False
    interfaces: bool = False
    lldp: bool = False
    cdp: bool = False
    snmp_versions: list[str] = Field(default_factory=list, max_length=len(SNMP_VERSIONS))

    @field_validator("snmp_versions")
    @classmethod
    def _versions(cls, v: list[str]) -> list[str]:
        for item in v:
            if item not in SNMP_VERSIONS:
                raise ValueError(f"unsupported SNMP version {item!r}; expected one of {SNMP_VERSIONS}")
        if len(set(v)) != len(v):
            raise ValueError("snmp_versions must not repeat a version")
        return v


class InterfaceDiscoverySpec(_Strict):
    strategy: Literal["if_xtable", "if_table", "none"] = "none"
    name_source: Literal["if_name", "if_descr", "if_alias"] = "if_name"
    if_types: list[int] = Field(default_factory=list, max_length=MAX_ITEMS)
    oids: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("if_types")
    @classmethod
    def _if_types(cls, v: list[int]) -> list[int]:
        for item in v:
            if not 1 <= item <= 1000:
                raise ValueError("if_types entries must be IANAifType numbers 1..1000")
        return v

    @field_validator("oids")
    @classmethod
    def _oids(cls, v: dict[str, str]) -> dict[str, str]:
        for key, oid in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                raise ValueError(f"interface OID key {key!r} is invalid")
            validate_oid(oid)
        return v


class LldpBehavior(_Strict):
    enabled: bool = False
    local_port_source: Literal["lldp_loc_port_id", "if_name", "if_descr"] = "lldp_loc_port_id"
    management_address: bool = True


class CdpBehavior(_Strict):
    enabled: bool = False
    device_id_normalization: Literal["none", "strip_domain"] = "none"


class NeighborBehavior(_Strict):
    lldp: LldpBehavior = Field(default_factory=LldpBehavior)
    cdp: CdpBehavior = Field(default_factory=CdpBehavior)


class DeviceProfileContent(_Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    device_class: Literal["switch", "router", "firewall", "pdu", "ups", "server", "storage", "sensor", "generic"] = "generic"
    match_criteria: list[MatchCriterion] = Field(default_factory=list, max_length=16)
    firmware_min: str | None = Field(default=None, max_length=64)
    firmware_max: str | None = Field(default=None, max_length=64)
    priority: int = Field(default=0, ge=-1000, le=1000)
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)
    interface_discovery: InterfaceDiscoverySpec = Field(default_factory=InterfaceDiscoverySpec)
    neighbor_behavior: NeighborBehavior = Field(default_factory=NeighborBehavior)

    @field_validator("firmware_min", "firmware_max")
    @classmethod
    def _firmware(cls, v: str | None) -> str | None:
        if v is not None:
            parse_version(v)
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "DeviceProfileContent":
        if self.firmware_min and self.firmware_max:
            if compare_versions(parse_version(self.firmware_min), parse_version(self.firmware_max)) > 0:
                raise ValueError("firmware_min must not exceed firmware_max")
        if self.neighbor_behavior.lldp.enabled and not self.capabilities.lldp:
            raise ValueError("neighbor_behavior.lldp is enabled but capabilities.lldp is false")
        if self.neighbor_behavior.cdp.enabled and not self.capabilities.cdp:
            raise ValueError("neighbor_behavior.cdp is enabled but capabilities.cdp is false")
        return self


class MetricMappingContent(_Strict):
    oid: str
    canonical_metric: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=32)
    scale: float = Field(default=1.0, gt=0, le=1_000_000_000)
    value_type: Literal["gauge", "counter", "string"] = "gauge"
    description: str | None = Field(default=None, max_length=255)

    @field_validator("oid")
    @classmethod
    def _oid(cls, v: str) -> str:
        return validate_oid(v)


def reject_secret_like_keys(document: object, path: str = "") -> None:
    """Profiles never carry secrets: refuse any key that looks like one, at any depth."""
    if isinstance(document, dict):
        for key, value in document.items():
            if _SECRET_NAME.search(str(key)):
                raise ValueError(f"profile content must not contain secret-like key {path}{key!r}")
            reject_secret_like_keys(value, f"{path}{key}.")
    elif isinstance(document, list):
        for index, item in enumerate(document):
            reject_secret_like_keys(item, f"{path}{index}.")
