"""Shared SNMP wire helpers for the v2c and v3 sessions: PDU/varbind codec, bounded UDP
exchange with retries, and a loop-safe subtree walk.

Reuses the BER primitives of `snmp.py` (the original v2c GET collector stays untouched so
its behaviour and tests are unaffected). Everything here treats the network as hostile:
lengths are bounded, trailing bytes are rejected, and a datagram that fails validation is
dropped instead of aborting the request, so one spoofed packet cannot cancel a poll.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .snmp import (
    SNMPError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTimeoutError,
    _BERReader,
    _decode_oid,
    _encode_oid,
    _integer,
    _tlv,
    _validate_target,
)

# PDU tags (RFC 3416)
GET_REQUEST = 0xA0
GET_NEXT_REQUEST = 0xA1
GET_RESPONSE = 0xA2
GET_BULK_REQUEST = 0xA5
REPORT = 0xA8

# Value tags
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_COUNTER64 = 0x46
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82
_UNSIGNED_TAGS = {TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64}

MAX_DATAGRAM = 65_507
MAX_VARBINDS = 64
DEFAULT_WALK_ROWS = 4_096
DEFAULT_MAX_REPETITIONS = 10


@dataclass(frozen=True, slots=True)
class Varbind:
    oid: str
    tag: int
    value: bytes

    @property
    def is_exception(self) -> bool:
        return self.tag in (TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB_VIEW)

    def as_int(self) -> int:
        if self.tag == TAG_INTEGER:
            return int.from_bytes(self.value, "big", signed=True)
        if self.tag in _UNSIGNED_TAGS:
            return int.from_bytes(self.value, "big", signed=False)
        raise SNMPError("SNMP value is not an integer")

    def as_bytes(self) -> bytes:
        if self.tag not in (TAG_OCTET_STRING, TAG_IPADDRESS):
            raise SNMPError("SNMP value is not an octet string")
        return self.value

    def as_oid(self) -> str:
        if self.tag != TAG_OID:
            raise SNMPError("SNMP value is not an OID")
        return _decode_oid(self.value)


@dataclass(frozen=True, slots=True)
class DecodedPdu:
    pdu_tag: int
    request_id: int
    error_status: int
    error_index: int
    varbinds: tuple[Varbind, ...]


def oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.split("."))


def oid_in_subtree(oid: str, base: str) -> bool:
    return oid == base or oid.startswith(base + ".")


def encode_pdu(pdu_tag: int, request_id: int, field_one: int, field_two: int, oids: list[str]) -> bytes:
    """`field_one/field_two` are error-status/error-index, or non-repeaters/max-repetitions for GETBULK."""
    if not 1 <= len(oids) <= MAX_VARBINDS:
        raise ValueError("a request needs between 1 and 64 OIDs")
    varbinds = b"".join(_tlv(0x30, _tlv(TAG_OID, _encode_oid(oid)) + _tlv(TAG_NULL, b"")) for oid in oids)
    return _tlv(pdu_tag, _integer(request_id) + _integer(field_one) + _integer(field_two) + _tlv(0x30, varbinds))


def decode_pdu(tag: int, body: bytes) -> DecodedPdu:
    reader = _BERReader(body)
    request_id = reader.read_integer()
    error_status = reader.read_integer()
    error_index = reader.read_integer()
    bindings = _BERReader(reader.read_tlv(0x30))
    if not reader.exhausted:
        raise SNMPError("SNMP PDU has trailing data")
    varbinds: list[Varbind] = []
    while not bindings.exhausted:
        if len(varbinds) >= MAX_VARBINDS * 4:
            raise SNMPError("SNMP response carries too many varbinds")
        binding = _BERReader(bindings.read_tlv(0x30))
        oid = _decode_oid(binding.read_tlv(TAG_OID))
        value_tag, value = binding.read_any_tlv()
        if not binding.exhausted:
            raise SNMPError("SNMP varbind has trailing data")
        varbinds.append(Varbind(oid, value_tag, value))
    return DecodedPdu(tag, request_id, error_status, error_index, tuple(varbinds))


_T = TypeVar("_T")


def udp_exchange(
    *,
    target: SNMPTarget,
    policy: SNMPTargetPolicy,
    datagram: bytes,
    accept: Callable[[bytes], _T | None],
    timeout_seconds: float,
    retries: int,
) -> _T:
    """Send `datagram`, return the first reply `accept` returns non-None for.

    Each attempt waits at most `timeout_seconds` in total; datagrams from another source,
    or that `accept` rejects (it returns None), are dropped and the wait continues for the
    remainder of that attempt. After `retries` additional attempts a timeout is raised, so
    a poll is always bounded by `(retries + 1) * timeout_seconds` and never retried
    forever. A resolved target is validated against the policy before any packet is sent.
    """
    if retries < 0:
        raise ValueError("retries must not be negative")
    target_ip = _validate_target(target, policy)
    family = socket.AF_INET6 if target_ip.version == 6 else socket.AF_INET
    destination = (str(target_ip), target.port)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            for _attempt in range(retries + 1):
                sock.sendto(datagram, destination)
                deadline = time.monotonic() + timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        response, peer = sock.recvfrom(MAX_DATAGRAM)
                    except TimeoutError:
                        break
                    if peer[0] != destination[0] or peer[1] != destination[1]:
                        continue
                    accepted = accept(response)
                    if accepted is not None:
                        return accepted
    except OSError as error:
        raise SNMPError("SNMP transport failure") from error
    raise SNMPTimeoutError("SNMP request timed out")


@dataclass(frozen=True, slots=True)
class WalkResult:
    rows: tuple[Varbind, ...]
    truncated: bool


def walk(
    bulk: Callable[[str, int], list[Varbind]], base_oid: str, *, max_rows: int = DEFAULT_WALK_ROWS,
    max_repetitions: int = DEFAULT_MAX_REPETITIONS,
) -> WalkResult:
    """Walk `base_oid` with GETBULK. Stops at the end of the subtree or MIB view, refuses
    an agent whose OIDs do not strictly increase (a loop), and bounds the row count."""
    rows: list[Varbind] = []
    cursor = base_oid
    last: tuple[int, ...] = oid_tuple(base_oid)
    while True:
        batch = bulk(cursor, max_repetitions)
        if not batch:
            return WalkResult(tuple(rows), False)
        for binding in batch:
            if binding.tag == TAG_END_OF_MIB_VIEW or not oid_in_subtree(binding.oid, base_oid):
                return WalkResult(tuple(rows), False)
            current = oid_tuple(binding.oid)
            if current <= last:
                raise SNMPError("SNMP agent returned non-increasing OIDs")
            last = current
            if binding.is_exception:
                continue
            if len(rows) >= max_rows:
                return WalkResult(tuple(rows), True)
            rows.append(binding)
        cursor = batch[-1].oid
