from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

import pytest

from edge_collector.snmp import (
    SNMPAuthenticationError,
    SNMPError,
    SNMPMalformedValueError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTimeoutError,
    SNMPUnknownOIDError,
    SNMPv2cCollector,
)

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_UPTIME_OID_BYTES = bytes.fromhex("2b06010201010300")

# --------------------------------------------------------------------------- BER codec
#
# The wire agent below answers a request by echoing fields out of it, so it has to locate
# those fields first. It does that by *decoding* the request — walking tag/length/value
# triples from the outermost SEQUENCE inward — rather than by searching for a byte that
# looks like a PDU tag or by slicing at an offset that happens to be right for one
# request shape.
#
# Neither shortcut survives contact with real BER. `_integer()` in edge_collector/snmp.py
# emits the request ID in the shortest form that fits, so its TLV is anywhere from 3 to 6
# bytes long and every field after it shifts accordingly — a fixed slice reads the wrong
# bytes for most of the request IDs the collector actually generates. Searching for the
# 0xA0 GetRequest tag is no better: 0xA0 is an ordinary byte, and a community string or a
# length prefix containing it is found first (test_community_containing_the_pdu_tag_byte
# below pins exactly that case).
#
# This decoder is deliberately written out here instead of importing snmp.py's own
# `_BERReader`. An agent that parsed requests with the same code under test would agree
# with the collector about a malformed packet by construction, and these tests would stop
# being able to see a BER bug at all.


class BERDecodeError(AssertionError):
    """The test agent could not decode a request the collector sent it."""


def _read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Decode one tag/length/value triple at `offset`; return (tag, value, next_offset)."""
    if offset + 2 > len(data):
        raise BERDecodeError(f"truncated TLV header at offset {offset}")
    tag = data[offset]
    first_length_byte = data[offset + 1]
    if first_length_byte < 0x80:
        length, value_start = first_length_byte, offset + 2
    else:
        count = first_length_byte & 0x7F
        if count == 0 or count > 4:
            raise BERDecodeError(f"unsupported long-form length ({count} bytes) at offset {offset}")
        if offset + 2 + count > len(data):
            raise BERDecodeError(f"truncated long-form length at offset {offset}")
        length = int.from_bytes(data[offset + 2 : offset + 2 + count], "big")
        value_start = offset + 2 + count
    value_end = value_start + length
    if value_end > len(data):
        raise BERDecodeError(f"TLV at offset {offset} claims {length} bytes past the packet end")
    return tag, data[value_start:value_end], value_end


def _expect_tlv(data: bytes, offset: int, expected_tag: int) -> tuple[bytes, int]:
    tag, value, next_offset = _read_tlv(data, offset)
    if tag != expected_tag:
        raise BERDecodeError(f"expected tag 0x{expected_tag:02x} at offset {offset}, found 0x{tag:02x}")
    return value, next_offset


def _raw_tlv(data: bytes, offset: int, expected_tag: int) -> tuple[bytes, int]:
    """The encoded bytes of one TLV, tag and length included — what the agent echoes back
    for the request ID so the response carries the collector's own encoding verbatim."""
    _, next_offset = _expect_tlv(data, offset, expected_tag)
    return data[offset:next_offset], next_offset


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_length(len(value)) + value


class DecodedRequest:
    """The fields the agent needs out of a GetRequest, located structurally."""

    def __init__(self, request: bytes) -> None:
        message, after_message = _expect_tlv(request, 0, 0x30)
        if after_message != len(request):
            raise BERDecodeError("trailing bytes after the outer SEQUENCE")
        version, offset = _expect_tlv(message, 0, 0x02)
        self.version = int.from_bytes(version, "big", signed=True)
        self.community, offset = _expect_tlv(message, offset, 0x04)
        pdu, offset = _expect_tlv(message, offset, 0xA0)
        if offset != len(message):
            raise BERDecodeError("trailing bytes after the GetRequest PDU")
        # The request ID's own encoded TLV — variable width, which is the whole point.
        self.request_id_tlv, pdu_offset = _raw_tlv(pdu, 0, 0x02)
        _, pdu_offset = _expect_tlv(pdu, pdu_offset, 0x02)  # error-status
        _, pdu_offset = _expect_tlv(pdu, pdu_offset, 0x02)  # error-index
        varbinds, pdu_offset = _expect_tlv(pdu, pdu_offset, 0x30)
        varbind, _ = _expect_tlv(varbinds, 0, 0x30)
        self.oid, _ = _expect_tlv(varbind, 0, 0x06)


def _response_for(request: bytes, *, community: bytes, value_tag: int, value: bytes) -> bytes:
    """A GetResponse echoing the decoded request's own request-ID TLV, whatever width the
    collector encoded it at."""
    decoded = DecodedRequest(request)
    varbind = _tlv(0x30, _tlv(0x06, decoded.oid) + _tlv(value_tag, value))
    pdu_body = decoded.request_id_tlv + _tlv(0x02, b"\x00") + _tlv(0x02, b"\x00") + _tlv(0x30, varbind)
    message = _tlv(0x02, b"\x01") + _tlv(0x04, community) + _tlv(0xA2, pdu_body)
    return _tlv(0x30, message)


# ------------------------------------------------------------------------- Test agent


@contextmanager
def wire_agent(
    *,
    community: bytes = b"private",
    value_tag: int = 0x43,
    value: bytes = b"\x00\x00\x00\x64",
    reply: bool = True,
    corrupt=None,
    captured: list | None = None,
):
    """A tiny real UDP SNMP v2c responder for protocol-level collector tests.

    `corrupt` receives the well-formed response and returns the bytes actually sent,
    which is how the malformed-packet tests put a broken frame on the wire without the
    agent itself having to be wrong. `captured` collects each decoded request so a test
    can assert on what the collector emitted."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(0.1)
    stopped = threading.Event()

    def run() -> None:
        while not stopped.is_set():
            try:
                request, peer = server.recvfrom(8192)
            except TimeoutError:
                continue
            if captured is not None:
                captured.append(DecodedRequest(request))
            if reply:
                response = _response_for(request, community=community, value_tag=value_tag, value=value)
                server.sendto(corrupt(response) if corrupt else response, peer)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stopped.set()
        thread.join(timeout=1)
        server.close()


def collector(port: int, *, timeout: float = 0.1) -> SNMPv2cCollector:
    return SNMPv2cCollector(
        SNMPTarget("127.0.0.1", port),
        policy=SNMPTargetPolicy(allow_loopback=True, allowed_ports=frozenset({port})),
        timeout_seconds=timeout,
    )


def _force_request_id(monkeypatch, request_id: int) -> None:
    """Pin the collector's own randomly generated request ID. `get()` computes it as
    `secrets.randbelow(2**31 - 1) + 1`, so the offset is undone here to land exactly on
    the value a test wants to exercise the encoding width of."""
    import edge_collector.snmp as snmp_module

    monkeypatch.setattr(snmp_module.secrets, "randbelow", lambda _upper: request_id - 1)


# ------------------------------------------------------------------- Behavioural tests


def test_real_udp_v2c_get_maps_sys_uptime_to_seconds():
    with wire_agent() as port:
        metric = collector(port).get(oid=SYS_UPTIME, community="private")

    assert metric.canonical_metric == "uptime_seconds"
    assert metric.value == 1.0
    assert metric.unit == "seconds"


def test_timeout_keeps_network_failure_distinct_from_bad_values():
    with wire_agent(reply=False) as port, pytest.raises(SNMPTimeoutError):
        collector(port).get(oid=SYS_UPTIME, community="private")


def test_wrong_community_response_is_an_authentication_failure():
    with wire_agent(community=b"different") as port, pytest.raises(SNMPAuthenticationError):
        collector(port).get(oid=SYS_UPTIME, community="private")


def test_malformed_numeric_value_is_rejected_not_coerced():
    with wire_agent(value_tag=0x04, value=b"not-a-number") as port, pytest.raises(SNMPMalformedValueError):
        collector(port).get(oid=SYS_UPTIME, community="private")


def test_unknown_oid_is_rejected_before_any_network_send():
    with wire_agent() as port, pytest.raises(SNMPUnknownOIDError):
        collector(port).get(oid="1.3.6.1.4.1.9999.1", community="private")


def test_default_policy_rejects_loopback_targets():
    with pytest.raises(ValueError, match="loopback"):
        SNMPv2cCollector(SNMPTarget("127.0.0.1", 161)).get(oid=SYS_UPTIME, community="private")


# ------------------------------------------------------- Request-ID encoding width
#
# `_integer()` emits the shortest encoding that fits and prepends a 0x00 sign byte when
# the top bit would otherwise read as negative, so the request-ID TLV is 3, 4, 5 or 6
# bytes wide depending on the value the collector drew. Each width has to round-trip.


@pytest.mark.parametrize(
    ("request_id", "expected_tlv_length"),
    [
        (0x01, 3),  # smallest possible: one content byte
        (0x7F, 3),  # largest single byte with the sign bit clear
        (0x80, 4),  # sign byte prepended -- the case a "fixed size" assumption misses
        (0x1234, 4),
        (0x00FFFF, 5),  # sign byte again, at two content bytes
        (0x123456, 5),
        (0x7FFFFFFF, 6),  # the largest value randbelow(2**31 - 1) + 1 can produce
    ],
)
def test_request_id_round_trips_at_every_encoding_width(monkeypatch, request_id, expected_tlv_length):
    _force_request_id(monkeypatch, request_id)
    captured: list = []
    with wire_agent(captured=captured) as port:
        metric = collector(port).get(oid=SYS_UPTIME, community="private")

    assert metric.value == 1.0
    [decoded] = captured
    assert len(decoded.request_id_tlv) == expected_tlv_length, decoded.request_id_tlv.hex()
    assert int.from_bytes(decoded.request_id_tlv[2:], "big", signed=True) == request_id


def test_mismatched_request_id_is_rejected():
    """The check the width tests above exist to keep meaningful: if the agent answers
    with a different request ID, the collector must refuse the response rather than
    accept whatever arrived."""

    def replace_request_id(response: bytes) -> bytes:
        message, _ = _expect_tlv(response, 0, 0x30)
        version, offset = _expect_tlv(message, 0, 0x02)
        community, offset = _expect_tlv(message, offset, 0x04)
        pdu, _ = _expect_tlv(message, offset, 0xA2)
        _, pdu_offset = _expect_tlv(pdu, 0, 0x02)
        rebuilt_pdu = _tlv(0x02, b"\x13\x37\x13\x37") + pdu[pdu_offset:]
        return _tlv(0x30, _tlv(0x02, version) + _tlv(0x04, community) + _tlv(0xA2, rebuilt_pdu))

    with wire_agent(corrupt=replace_request_id) as port, pytest.raises(SNMPError, match="request ID"):
        collector(port).get(oid=SYS_UPTIME, community="private")


# ----------------------------------------------------------------- Community strings


@pytest.mark.parametrize("community", ["pub\xa0lic", "\xa0secret", "se\xa0cr\xa0et"])
def test_community_encoding_to_the_pdu_tag_byte(community):
    """0xA0 is the GetRequest tag *and* an ordinary byte that appears inside a community
    string on the wire: `get()` encodes the community as UTF-8, and U+00A0 encodes to
    0xC2 0xA0. An agent that finds the PDU by searching for the first 0xA0 byte locates
    the community instead and answers with garbage; decoding the request structurally
    cannot be confused this way."""
    encoded = community.encode("utf-8")
    assert b"\xa0" in encoded, "this test is pointless unless the community really carries 0xA0"
    with wire_agent(community=encoded) as port:
        metric = collector(port).get(oid=SYS_UPTIME, community=community)
    assert metric.value == 1.0


# --------------------------------------------------------------------- Length forms


@pytest.mark.parametrize("community_length", [126, 127, 128, 255, 256, 400])
def test_long_form_lengths_round_trip(community_length):
    """A community long enough to push the message past 127 bytes forces BER's long-form
    length encoding (0x81 nn, then 0x82 nn nn past 255). Both the collector's encoder and
    its reader have to handle every boundary, not just the short form."""
    community = b"c" * community_length
    with wire_agent(community=community) as port:
        metric = collector(port).get(oid=SYS_UPTIME, community=community.decode("ascii"))
    assert metric.value == 1.0


def test_long_form_length_is_actually_exercised():
    """Guards the test above from silently degrading: assert the encoder really emitted a
    long-form length rather than trusting that 400 bytes was enough to trigger one."""
    from edge_collector.snmp import _build_get_request

    request = _build_get_request(0x7FFFFFFF, b"c" * 400, SYS_UPTIME)
    assert request[1] & 0x80, "expected a long-form length on the outer SEQUENCE"
    assert request[1] & 0x7F == 2, "expected a two-byte long-form length past 255 bytes"


# ------------------------------------------------------------------ Malformed packets


def _truncate_to(count: int):
    return lambda response: response[:count]


@pytest.mark.parametrize(
    ("name", "corrupt"),
    [
        ("empty datagram", lambda response: b""),
        ("tag only", _truncate_to(1)),
        ("header without a body", _truncate_to(2)),
        ("body cut mid-PDU", lambda response: response[: len(response) // 2]),
        ("last byte removed", lambda response: response[:-1]),
        ("outer length overstates the packet", lambda response: bytes([response[0], 0x7E]) + response[2:]),
        ("outer tag is not a SEQUENCE", lambda response: b"\x31" + response[1:]),
        ("trailing garbage after a valid frame", lambda response: response + b"\xff\xff\xff\xff"),
        ("long-form length claiming 5 bytes", lambda response: bytes([response[0], 0x85]) + response[1:]),
        ("indefinite length", lambda response: bytes([response[0], 0x80]) + response[2:]),
    ],
)
def test_malformed_response_raises_a_clean_snmp_error(name, corrupt):
    """Every one of these must surface as an SNMPError — never an IndexError, a struct
    error, a hang, or a fabricated metric from a partially parsed frame."""
    with wire_agent(corrupt=corrupt) as port, pytest.raises(SNMPError):
        collector(port).get(oid=SYS_UPTIME, community="private")


def test_response_for_a_different_oid_is_rejected():
    def swap_oid(response: bytes) -> bytes:
        return response.replace(SYS_UPTIME_OID_BYTES, bytes.fromhex("2b06010201010100"))

    with wire_agent(corrupt=swap_oid) as port, pytest.raises(SNMPError, match="OID"):
        collector(port).get(oid=SYS_UPTIME, community="private")


def test_agent_error_status_is_surfaced_not_ignored():
    def set_error_status(response: bytes) -> bytes:
        message, _ = _expect_tlv(response, 0, 0x30)
        version, offset = _expect_tlv(message, 0, 0x02)
        community, offset = _expect_tlv(message, offset, 0x04)
        pdu, _ = _expect_tlv(message, offset, 0xA2)
        request_id_tlv, pdu_offset = _raw_tlv(pdu, 0, 0x02)
        _, pdu_offset = _expect_tlv(pdu, pdu_offset, 0x02)
        rebuilt_pdu = request_id_tlv + _tlv(0x02, b"\x02") + pdu[pdu_offset:]
        return _tlv(0x30, _tlv(0x02, version) + _tlv(0x04, community) + _tlv(0xA2, rebuilt_pdu))

    with wire_agent(corrupt=set_error_status) as port, pytest.raises(SNMPError, match="error status"):
        collector(port).get(oid=SYS_UPTIME, community="private")
