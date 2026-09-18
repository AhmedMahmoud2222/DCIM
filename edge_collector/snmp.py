from __future__ import annotations

import ipaddress
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass


class SNMPError(RuntimeError):
    """Base error for edge-side SNMP v2c acquisition failures."""


class SNMPTimeoutError(SNMPError):
    pass


class SNMPAuthenticationError(SNMPError):
    pass


class SNMPMalformedValueError(SNMPError):
    pass


class SNMPUnknownOIDError(SNMPError):
    pass


class SNMPTargetPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SNMPTarget:
    host: str
    port: int = 161


@dataclass(frozen=True, slots=True)
class SNMPTargetPolicy:
    """Explicit allow-list policy matching Central's safe outbound principles."""

    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    allowed_ports: frozenset[int] = frozenset({161})
    allow_loopback: bool = False


@dataclass(frozen=True, slots=True)
class SNMPMetric:
    source_identifier: str
    canonical_metric: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class _OIDMapping:
    canonical_metric: str
    unit: str
    scale: float


SUPPORTED_OIDS: Mapping[str, _OIDMapping] = {
    "1.3.6.1.2.1.1.3.0": _OIDMapping("uptime_seconds", "seconds", 0.01),
}


class SNMPv2cCollector:
    """Minimal real SNMP v2c GET transport for explicitly mapped scalar OIDs."""

    def __init__(self, target: SNMPTarget, *, policy: SNMPTargetPolicy | None = None, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.target = target
        self.policy = policy or SNMPTargetPolicy()
        self.timeout_seconds = timeout_seconds

    def get(self, *, oid: str, community: str) -> SNMPMetric:
        mapping = SUPPORTED_OIDS.get(oid)
        if mapping is None:
            raise SNMPUnknownOIDError("OID is not configured for edge collection")
        if not community:
            raise SNMPAuthenticationError("SNMP community is required")
        target_ip = _validate_target(self.target, self.policy)
        request_id = secrets.randbelow(2**31 - 1) + 1
        request = _build_get_request(request_id, community.encode("utf-8"), oid)
        try:
            with socket.socket(target_ip.version == 6 and socket.AF_INET6 or socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout_seconds)
                destination = (str(target_ip), self.target.port)
                sock.sendto(request, destination)
                response, _peer = sock.recvfrom(8192)
        except TimeoutError as error:
            raise SNMPTimeoutError("SNMP request timed out") from error
        except OSError as error:
            raise SNMPError("SNMP transport failure") from error
        value = _parse_response(response, request_id=request_id, expected_community=community.encode("utf-8"), expected_oid=oid)
        try:
            numeric_value = _numeric_value(value)
        except (TypeError, ValueError) as error:
            raise SNMPMalformedValueError("SNMP response value is not numeric") from error
        return SNMPMetric(oid, mapping.canonical_metric, numeric_value * mapping.scale, mapping.unit)


def _validate_target(target: SNMPTarget, policy: SNMPTargetPolicy) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not 1 <= target.port <= 65535 or target.port not in policy.allowed_ports:
        raise SNMPTargetPolicyError("SNMP port is not permitted")
    host = target.host.strip()
    if not host or any(ord(character) < 0x21 or ord(character) == 0x7F for character in host):
        raise SNMPTargetPolicyError("SNMP host is invalid")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        resolved = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, target.port, type=socket.SOCK_DGRAM)
        except OSError as error:
            raise SNMPTargetPolicyError("SNMP target DNS resolution failed") from error
        resolved = list({ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos})
    if not resolved:
        raise SNMPTargetPolicyError("SNMP target resolved to no addresses")
    for address in resolved:
        if address.is_loopback:
            if policy.allow_loopback:
                continue
            raise SNMPTargetPolicyError("SNMP target loopback is not explicitly enabled")
        if address.is_unspecified or address.is_multicast or address.is_reserved or address.is_link_local:
            raise SNMPTargetPolicyError("SNMP target is always denied by policy")
        if not any(network.version == address.version and address in network for network in policy.allowed_networks):
            raise SNMPTargetPolicyError("SNMP target is not within an allowed network")
    return resolved[0]


def _build_get_request(request_id: int, community: bytes, oid: str) -> bytes:
    oid_value = _encode_oid(oid)
    varbind = _tlv(0x30, _tlv(0x06, oid_value) + _tlv(0x05, b""))
    pdu = _tlv(0xA0, _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, varbind))
    return _tlv(0x30, _integer(1) + _tlv(0x04, community) + pdu)


def _parse_response(response: bytes, *, request_id: int, expected_community: bytes, expected_oid: str) -> tuple[int, bytes]:
    message = _BERReader(response).read_constructed(0x30)
    if message.read_integer() != 1:
        raise SNMPError("SNMP response is not v2c")
    if message.read_tlv(0x04) != expected_community:
        raise SNMPAuthenticationError("SNMP response community did not match request")
    pdu = _BERReader(message.read_tlv(0xA2))
    if pdu.read_integer() != request_id:
        raise SNMPError("SNMP response request ID did not match")
    error_status = pdu.read_integer()
    pdu.read_integer()  # error index is meaningful only when error_status is non-zero
    if error_status:
        raise SNMPError("SNMP agent returned an error status")
    bindings = _BERReader(pdu.read_tlv(0x30))
    binding = _BERReader(bindings.read_tlv(0x30))
    if _decode_oid(binding.read_tlv(0x06)) != expected_oid:
        raise SNMPError("SNMP response OID did not match request")
    tag, value = binding.read_any_tlv()
    if not binding.exhausted or not bindings.exhausted or not pdu.exhausted or not message.exhausted:
        raise SNMPError("SNMP response has trailing data")
    return tag, value


def _numeric_value(value: tuple[int, bytes]) -> float:
    tag, raw = value
    if tag in {0x02, 0x41, 0x42, 0x43}:
        return float(int.from_bytes(raw, byteorder="big", signed=tag == 0x02))
    if tag == 0x04:
        return float(raw.decode("ascii"))
    raise ValueError("unsupported SNMP value type")


class _BERReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    @property
    def exhausted(self) -> bool:
        return self._offset == len(self._data)

    def read_constructed(self, tag: int) -> _BERReader:
        return _BERReader(self.read_tlv(tag))

    def read_integer(self) -> int:
        return int.from_bytes(self.read_tlv(0x02), byteorder="big", signed=True)

    def read_tlv(self, expected_tag: int) -> bytes:
        tag, value = self.read_any_tlv()
        if tag != expected_tag:
            raise SNMPError("SNMP response has an unexpected BER tag")
        return value

    def read_any_tlv(self) -> tuple[int, bytes]:
        if self._offset >= len(self._data):
            raise SNMPError("SNMP response ended unexpectedly")
        tag = self._data[self._offset]
        self._offset += 1
        length = self._read_length()
        end = self._offset + length
        if end > len(self._data):
            raise SNMPError("SNMP response BER length exceeds packet")
        value = self._data[self._offset:end]
        self._offset = end
        return tag, value

    def _read_length(self) -> int:
        if self._offset >= len(self._data):
            raise SNMPError("SNMP response BER length is missing")
        first = self._data[self._offset]
        self._offset += 1
        if first < 0x80:
            return first
        count = first & 0x7F
        if count == 0 or count > 4 or self._offset + count > len(self._data):
            raise SNMPError("SNMP response BER length is invalid")
        length = int.from_bytes(self._data[self._offset:self._offset + count], "big")
        self._offset += count
        return length


def _integer(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _tlv(0x02, raw)


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_length(len(value)) + value


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _encode_oid(oid: str) -> bytes:
    parts = [int(part) for part in oid.split(".")]
    if len(parts) < 2 or parts[0] not in (0, 1, 2) or parts[1] < 0:
        raise ValueError("OID is invalid")
    encoded = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        if part < 0:
            raise ValueError("OID is invalid")
        chunks = [part & 0x7F]
        part >>= 7
        while part:
            chunks.append(0x80 | (part & 0x7F))
            part >>= 7
        encoded.extend(reversed(chunks))
    return bytes(encoded)


def _decode_oid(raw: bytes) -> str:
    if not raw:
        raise SNMPError("SNMP response OID is empty")
    first = raw[0]
    parts = [min(first // 40, 2), first % 40 if first < 80 else first - 80]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    if value:
        raise SNMPError("SNMP response OID is malformed")
    return ".".join(str(part) for part in parts)
