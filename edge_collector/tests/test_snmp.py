from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

import pytest

from edge_collector.snmp import (
    SNMPAuthenticationError,
    SNMPMalformedValueError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTimeoutError,
    SNMPUnknownOIDError,
    SNMPv2cCollector,
)

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"


@contextmanager
def wire_agent(*, community: bytes = b"private", value_tag: int = 0x43, value: bytes = b"\x00\x00\x00\x64", reply: bool = True):
    """A tiny real UDP SNMP v2c responder for protocol-level collector tests."""
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
            if reply:
                server.sendto(_response_for(request, community=community, value_tag=value_tag, value=value), peer)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stopped.set()
        thread.join(timeout=1)
        server.close()


def _response_for(request: bytes, *, community: bytes, value_tag: int, value: bytes) -> bytes:
    request_id = request[18:22]  # fixed-size request ID emitted by the edge collector
    oid = bytes.fromhex("2b06010201010300")
    varbind = b"\x30" + _length(len(b"\x06" + _length(len(oid)) + oid + bytes([value_tag]) + _length(len(value)) + value))
    varbind += b"\x06" + _length(len(oid)) + oid + bytes([value_tag]) + _length(len(value)) + value
    varbinds = b"\x30" + _length(len(varbind)) + varbind
    pdu_body = b"\x02\x04" + request_id + b"\x02\x01\x00\x02\x01\x00" + varbinds
    message = b"\x02\x01\x01\x04" + _length(len(community)) + community + b"\xa2" + _length(len(pdu_body)) + pdu_body
    return b"\x30" + _length(len(message)) + message


def _length(length: int) -> bytes:
    return bytes([length]) if length < 128 else b"\x81" + bytes([length])


def collector(port: int, *, timeout: float = 0.1) -> SNMPv2cCollector:
    return SNMPv2cCollector(
        SNMPTarget("127.0.0.1", port),
        policy=SNMPTargetPolicy(allow_loopback=True, allowed_ports=frozenset({port})),
        timeout_seconds=timeout,
    )


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
