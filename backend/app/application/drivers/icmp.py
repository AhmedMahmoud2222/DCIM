"""Real ICMP echo (ping) driver -- not a shell-out to a `ping` binary (which may not
even be installed; this repo's own sandbox container has no `ping` executable), a
genuine raw ICMP socket built with the stdlib `socket` module.

Requires `CAP_NET_RAW` (or root) to open `SOCK_RAW`/`IPPROTO_ICMP` -- a real
operational requirement for wherever this driver actually runs (the central collector
process, or eventually an edge collector), documented in
`PHASE8_EDGE_COLLECTOR_CONTRACT.md` rather than silently assumed. When the raw socket
cannot be opened (`PermissionError`), this driver raises `DriverConnectionError` with
that reason explicitly named -- it does NOT silently fall back to a different signal
and call it "ICMP" (that would misrepresent what was actually measured); a genuinely
different check (e.g. TCP-connect reachability) would be a different driver, not this
one degrading quietly.

A raw ICMP socket receives EVERY ICMP packet arriving at the host, not just replies to
this driver's own request -- `poll()` filters by embedding this process's PID as the
ICMP identifier and a per-call sequence number, discarding any reply that doesn't
match (someone else's echo reply, an unrelated ICMP message) rather than mistaking it
for this poll's own answer."""

import asyncio
import os
import socket
import struct
import time
import uuid

from app.application.drivers.base import AcquisitionResult, DriverConnectionError, ProtocolDriver, utcnow

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0
_DEFAULT_TIMEOUT_SECONDS = 2.0


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum((data[i] << 8) + data[i + 1] for i in range(0, len(data), 2))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def _build_echo_packet(identifier: int, sequence: int) -> bytes:
    header = struct.pack("!BBHHH", _ICMP_ECHO_REQUEST, 0, 0, identifier, sequence)
    payload = struct.pack("!d", time.time()) + b"dcim-icmp-driver"
    checksum = _checksum(header + payload)
    header = struct.pack("!BBHHH", _ICMP_ECHO_REQUEST, 0, checksum, identifier, sequence)
    return header + payload


def _parse_reply(packet: bytes) -> tuple[int, int, int] | None:
    """Returns (type, identifier, sequence) from the ICMP header inside an IPv4
    packet's payload, or None if the packet is too short to contain one."""
    if len(packet) < 20 + 8:
        return None
    icmp = packet[20:28]
    icmp_type, _code, _chk, identifier, sequence = struct.unpack("!BBHHH", icmp)
    return icmp_type, identifier, sequence


class ICMPDriver(ProtocolDriver):
    protocol_code = "icmp"

    def __init__(self, integration_id: uuid.UUID, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.integration_id = integration_id
        self.timeout_seconds = timeout_seconds
        self._target_host: str | None = None
        self._sock: socket.socket | None = None

    async def connect(self, *, target_host: str, target_port: int | None, config: dict, credential: str | None) -> None:
        self._target_host = target_host
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except PermissionError as exc:
            raise DriverConnectionError(
                integration_id=self.integration_id,
                reason=(
                    "Raw ICMP socket requires CAP_NET_RAW or root; this process does not have it. "
                    "See PHASE8_EDGE_COLLECTOR_CONTRACT.md."
                ),
            ) from exc
        except OSError as exc:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"Could not open ICMP socket: {exc}") from exc
        self._sock.setblocking(False)

    async def poll(self) -> AcquisitionResult:
        if self._sock is None or self._target_host is None:
            raise DriverConnectionError(integration_id=self.integration_id, reason="poll() called before connect().")

        identifier = os.getpid() & 0xFFFF
        sequence = 1
        packet = _build_echo_packet(identifier, sequence)
        try:
            self._sock.sendto(packet, (self._target_host, 0))
        except OSError as exc:
            raise DriverConnectionError(integration_id=self.integration_id, reason=f"Could not send ICMP echo: {exc}") from exc

        loop = asyncio.get_event_loop()
        start = time.monotonic()
        deadline = start + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverConnectionError(
                    integration_id=self.integration_id, reason=f"No ICMP echo reply from {self._target_host} within timeout."
                )
            try:
                reply = await asyncio.wait_for(loop.sock_recv(self._sock, 1024), timeout=remaining)
            except TimeoutError as exc:
                raise DriverConnectionError(
                    integration_id=self.integration_id, reason=f"No ICMP echo reply from {self._target_host} within timeout."
                ) from exc
            parsed = _parse_reply(reply)
            if parsed is None:
                continue
            icmp_type, reply_identifier, reply_sequence = parsed
            if icmp_type == _ICMP_ECHO_REPLY and reply_identifier == identifier and reply_sequence == sequence:
                break
            # Not our reply (someone else's ping, or a stray ICMP message) -- keep
            # waiting until our own reply arrives or the deadline passes.

        round_trip_ms = round((time.monotonic() - start) * 1000, 3)
        return AcquisitionResult(
            external_identifier=self._target_host,
            observed_at=utcnow(),
            raw_attributes={"protocol": "icmp", "target_host": self._target_host},
            metrics={"round_trip_ms": round_trip_ms, "reachable": True},
        )

    async def disconnect(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
