"""SNMPv3 authPriv session over UDP (RFC 3412/3414/3826).

One `SNMPv3Session` talks to one agent. Authoritative-engine discovery, key localization,
time-window synchronisation, authentication, encryption and retransmission are handled
here; callers see `get`, `get_next`, `get_bulk` and `walk`.

Failure model:

* A datagram that is not a valid, correctly authenticated answer to *this* request (wrong
  msgID, bad MAC, outside the time window, malformed BER, wrong source) is dropped and the
  wait continues, so an attacker who can spray packets cannot abort a poll, only delay it
  until the normal timeout.
* A genuine authentication failure reported by the agent (unknown user, wrong digest,
  unsupported security level, decryption error) raises `SNMPAuthenticationError` carrying
  a coarse `reason` code. The message never names the user and never includes secrets.
* Timeouts raise `SNMPTimeoutError` after `(retries + 1) * timeout_seconds`.

Nothing in this module logs.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .snmp import (
    SNMPAuthenticationError,
    SNMPError,
    SNMPMalformedValueError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPUnknownOIDError,
    _integer,
    _tlv,
)
from .snmp_usm import (
    SNMPv3Credentials,
    UsmKeys,
    compute_mac,
    decrypt_scoped_pdu,
    derive_keys,
    encrypt_scoped_pdu,
    new_salt,
    verify_mac,
)
from .snmp_wire import (
    DEFAULT_MAX_REPETITIONS,
    DEFAULT_WALK_ROWS,
    GET_BULK_REQUEST,
    GET_NEXT_REQUEST,
    GET_REQUEST,
    GET_RESPONSE,
    REPORT,
    DecodedPdu,
    Varbind,
    WalkResult,
    decode_pdu,
    encode_pdu,
    udp_exchange,
    walk,
)

TIME_WINDOW_SECONDS = 150
MAX_MESSAGE_SIZE = 65_507
FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 0x01, 0x02, 0x04
SECURITY_MODEL_USM = 3

# usmStats* counters carried by Report PDUs (RFC 3414 §5)
_REPORT_REASONS = {
    "1.3.6.1.6.3.15.1.1.1.0": "unsupported_security_level",
    "1.3.6.1.6.3.15.1.1.2.0": "not_in_time_window",
    "1.3.6.1.6.3.15.1.1.3.0": "unknown_user",
    "1.3.6.1.6.3.15.1.1.4.0": "unknown_engine_id",
    "1.3.6.1.6.3.15.1.1.5.0": "wrong_digest",
    "1.3.6.1.6.3.15.1.1.6.0": "decryption_error",
}
_AUTH_FAILURE_REASONS = frozenset(
    {"unsupported_security_level", "unknown_user", "wrong_digest", "decryption_error"}
)


class SNMPv3AuthenticationError(SNMPAuthenticationError):
    """The agent rejected the credentials. `reason` is a coarse category only."""

    def __init__(self, reason: str) -> None:
        super().__init__("SNMPv3 authentication failed")
        self.reason = reason


# ----------------------------------------------------------------------------- BER cursor
class _Cursor:
    """Offset-tracking BER reader. Lengths are strict (definite, at most 4 length bytes)
    and every element must lie inside its parent."""

    def __init__(self, data: bytes, start: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = start
        self.end = len(data) if end is None else end

    @property
    def done(self) -> bool:
        return self.pos == self.end

    def next(self) -> tuple[int, int, int]:
        """Returns (tag, value_start, value_end) and advances past the element."""
        data, pos = self.data, self.pos
        if pos + 2 > self.end:
            raise SNMPError("SNMPv3 message ended unexpectedly")
        tag, first = data[pos], data[pos + 1]
        pos += 2
        if first < 0x80:
            length = first
        else:
            count = first & 0x7F
            if count == 0 or count > 4 or pos + count > self.end:
                raise SNMPError("SNMPv3 message BER length is invalid")
            length = int.from_bytes(data[pos:pos + count], "big")
            pos += count
        if pos + length > self.end:
            raise SNMPError("SNMPv3 message BER length exceeds the packet")
        self.pos = pos + length
        return tag, pos, pos + length

    def expect(self, tag: int) -> tuple[int, int]:
        found, start, end = self.next()
        if found != tag:
            raise SNMPError("SNMPv3 message has an unexpected BER tag")
        return start, end

    def integer(self) -> int:
        start, end = self.expect(0x02)
        return int.from_bytes(self.data[start:end], "big", signed=True)

    def octets(self) -> bytes:
        start, end = self.expect(0x04)
        return self.data[start:end]

    def sequence(self) -> _Cursor:
        start, end = self.expect(0x30)
        return _Cursor(self.data, start, end)


@dataclass(frozen=True, slots=True)
class _Message:
    msg_id: int
    flags: int
    engine_id: bytes
    boots: int
    time: int
    user: bytes
    auth_params: bytes
    auth_span: tuple[int, int]
    priv_params: bytes
    scoped: bytes  # ciphertext when FLAG_PRIV is set, plaintext ScopedPDU otherwise


def _parse_message(datagram: bytes) -> _Message:
    outer = _Cursor(datagram)
    message = outer.sequence()
    if not outer.done:
        raise SNMPError("SNMPv3 message has trailing data")
    if message.integer() != 3:
        raise SNMPError("not an SNMPv3 message")
    header = message.sequence()
    msg_id = header.integer()
    header.integer()  # msgMaxSize: the agent's limit; irrelevant to a response
    flags_raw = header.octets()
    security_model = header.integer()
    if len(flags_raw) != 1 or security_model != SECURITY_MODEL_USM or not header.done:
        raise SNMPError("unsupported SNMPv3 header")
    sp_start, sp_end = message.expect(0x04)
    usm = _Cursor(datagram, sp_start, sp_end).sequence()
    engine_id = usm.octets()
    boots, engine_time = usm.integer(), usm.integer()
    user = usm.octets()
    auth_start, auth_end = usm.expect(0x04)
    priv_params = usm.octets()
    if not usm.done:
        raise SNMPError("SNMPv3 security parameters have trailing data")
    element_start = message.pos
    tag, start, end = message.next()
    if not message.done:
        raise SNMPError("SNMPv3 message has trailing data")
    if tag not in (0x30, 0x04):
        raise SNMPError("SNMPv3 message data has an unexpected tag")
    # encrypted: the OCTET STRING contents; plaintext: the whole ScopedPDU SEQUENCE element
    scoped = datagram[start:end] if tag == 0x04 else datagram[element_start:end]
    return _Message(
        msg_id=msg_id, flags=flags_raw[0], engine_id=engine_id, boots=boots, time=engine_time, user=user,
        auth_params=datagram[auth_start:auth_end], auth_span=(auth_start, auth_end), priv_params=priv_params,
        scoped=scoped,
    )


def _parse_scoped(scoped: bytes) -> tuple[bytes, DecodedPdu]:
    outer = _Cursor(scoped)
    body = outer.sequence()
    if not outer.done:
        raise SNMPError("SNMPv3 scoped PDU has trailing data")
    context_engine_id = body.octets()
    body.octets()  # contextName
    pdu_tag, start, end = body.next()
    if not body.done:
        raise SNMPError("SNMPv3 scoped PDU has trailing data")
    if pdu_tag not in (GET_RESPONSE, REPORT):
        raise SNMPError("SNMPv3 response is not a response or report PDU")
    return context_engine_id, decode_pdu(pdu_tag, scoped[start:end])


def _encode_usm(engine_id: bytes, boots: int, engine_time: int, user: bytes, auth_params: bytes, priv_params: bytes) -> bytes:
    return _tlv(
        0x30,
        _tlv(0x04, engine_id) + _integer(boots) + _integer(engine_time) + _tlv(0x04, user)
        + _tlv(0x04, auth_params) + _tlv(0x04, priv_params),
    )


def _assemble(msg_id: int, flags: int, usm: bytes, data: bytes) -> bytes:
    header = _tlv(0x30, _integer(msg_id) + _integer(MAX_MESSAGE_SIZE) + _tlv(0x04, bytes([flags])) + _integer(SECURITY_MODEL_USM))
    return _tlv(0x30, _integer(3) + header + _tlv(0x04, usm) + data)


# ----------------------------------------------------------------------------- session
@dataclass(slots=True)
class _Engine:
    engine_id: bytes
    boots: int
    time: int
    synced_at: float
    keys: UsmKeys


@dataclass(frozen=True, slots=True)
class _Report:
    reason: str
    engine_id: bytes
    boots: int
    time: int
    authenticated: bool


class SNMPv3Session:
    """authPriv SNMPv3 session. Not thread-safe; use one per poll."""

    def __init__(
        self,
        target: SNMPTarget,
        credentials: SNMPv3Credentials,
        *,
        policy: SNMPTargetPolicy | None = None,
        timeout_seconds: float = 5.0,
        retries: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retries < 0:
            raise ValueError("retries must not be negative")
        self.target = target
        self._credentials = credentials
        self._policy = policy or SNMPTargetPolicy()
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._clock = clock
        self._engine: _Engine | None = None
        self._user = credentials.username.encode("utf-8")
        self._context = credentials.context_name.encode("utf-8")

    def __repr__(self) -> str:
        return f"SNMPv3Session(target={self.target!r})"

    # -- public operations
    def get(self, oid: str) -> Varbind:
        pdu = self._request(GET_REQUEST, 0, 0, [oid])
        if len(pdu.varbinds) != 1 or pdu.varbinds[0].oid != oid:
            raise SNMPError("SNMP response OID did not match request")
        binding = pdu.varbinds[0]
        if binding.is_exception:
            raise SNMPUnknownOIDError("OID is not available on the agent")
        return binding

    def get_next(self, oid: str) -> Varbind:
        pdu = self._request(GET_NEXT_REQUEST, 0, 0, [oid])
        if len(pdu.varbinds) != 1:
            raise SNMPMalformedValueError("SNMP response varbind count did not match request")
        return pdu.varbinds[0]

    def get_bulk(self, oid: str, max_repetitions: int = DEFAULT_MAX_REPETITIONS) -> list[Varbind]:
        if not 1 <= max_repetitions <= 50:
            raise ValueError("max_repetitions must be between 1 and 50")
        return list(self._request(GET_BULK_REQUEST, 0, max_repetitions, [oid]).varbinds)

    def walk(self, base_oid: str, *, max_rows: int = DEFAULT_WALK_ROWS) -> WalkResult:
        return walk(self.get_bulk, base_oid, max_rows=max_rows)

    # -- request machinery
    def _request(self, pdu_tag: int, field_one: int, field_two: int, oids: list[str]) -> DecodedPdu:
        resyncs = 0
        while True:
            engine = self._engine or self._discover()
            request_id = secrets.randbelow(2**31 - 1) + 1
            msg_id = secrets.randbelow(2**31 - 1) + 1
            datagram = self._build_secured(engine, msg_id, encode_pdu(pdu_tag, request_id, field_one, field_two, oids))
            outcome = udp_exchange(
                target=self.target, policy=self._policy, datagram=datagram,
                accept=self._acceptor(engine, msg_id, request_id),
                timeout_seconds=self.timeout_seconds, retries=self.retries,
            )
            if isinstance(outcome, DecodedPdu):
                if outcome.error_status:
                    raise SNMPError("SNMP agent returned an error status")
                return outcome
            if outcome.reason in _AUTH_FAILURE_REASONS:
                raise SNMPv3AuthenticationError(outcome.reason)
            resyncs += 1
            if resyncs > 2:
                raise SNMPv3AuthenticationError(outcome.reason)
            if outcome.reason == "unknown_engine_id":
                self._engine = None
            elif outcome.reason == "not_in_time_window" and outcome.authenticated:
                self._engine = _Engine(engine.engine_id, outcome.boots, outcome.time, self._clock(), engine.keys)
            else:
                raise SNMPError("SNMP agent returned an unrecognized report")

    def _estimated_time(self, engine: _Engine) -> int:
        return min(engine.time + int(self._clock() - engine.synced_at), 2**31 - 1)

    def _discover(self) -> _Engine:
        """Engine discovery (RFC 3414 §4): an unauthenticated, reportable request with an
        empty engine ID makes the agent answer with a Report carrying its engine ID, boots
        and time. Keys are localized to that engine only after the answer arrives."""
        request_id = secrets.randbelow(2**31 - 1) + 1
        msg_id = secrets.randbelow(2**31 - 1) + 1
        pdu = _tlv(GET_REQUEST, _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, b""))
        scoped = _tlv(0x30, _tlv(0x04, b"") + _tlv(0x04, b"") + pdu)
        datagram = _assemble(msg_id, FLAG_REPORTABLE, _encode_usm(b"", 0, 0, b"", b"", b""), scoped)

        def accept(data: bytes) -> _Report | None:
            try:
                message = _parse_message(data)
                if message.msg_id != msg_id or message.flags & (FLAG_AUTH | FLAG_PRIV):
                    return None
                _context_engine, pdu_decoded = _parse_scoped(message.scoped)
            except SNMPError:
                return None
            if pdu_decoded.pdu_tag != REPORT or not 5 <= len(message.engine_id) <= 32:
                return None
            if not 0 <= message.boots < 2**31 or not 0 <= message.time < 2**31:
                return None
            return _Report("discovery", message.engine_id, message.boots, message.time, False)

        report = udp_exchange(
            target=self.target, policy=self._policy, datagram=datagram, accept=accept,
            timeout_seconds=self.timeout_seconds, retries=self.retries,
        )
        engine = _Engine(
            report.engine_id, report.boots, report.time, self._clock(), derive_keys(self._credentials, report.engine_id)
        )
        self._engine = engine
        return engine

    def _build_secured(self, engine: _Engine, msg_id: int, pdu: bytes) -> bytes:
        credentials, keys = self._credentials, engine.keys
        boots, engine_time = engine.boots, self._estimated_time(engine)
        scoped = _tlv(0x30, _tlv(0x04, engine.engine_id) + _tlv(0x04, self._context) + pdu)
        salt = new_salt()
        data = _tlv(0x04, encrypt_scoped_pdu(keys.priv_key, boots, engine_time, salt, scoped))
        flags = FLAG_AUTH | FLAG_PRIV | FLAG_REPORTABLE

        def assemble(auth_params: bytes) -> bytes:
            usm = _encode_usm(engine.engine_id, boots, engine_time, self._user, auth_params, salt)
            return _assemble(msg_id, flags, usm, data)

        unsigned = assemble(bytes(keys.mac_length))
        return assemble(compute_mac(credentials.auth_protocol, keys.auth_key, unsigned))

    def _acceptor(self, engine: _Engine, msg_id: int, request_id: int) -> Callable[[bytes], DecodedPdu | _Report | None]:
        def accept(datagram: bytes) -> DecodedPdu | _Report | None:
            return self._accept(datagram, engine, msg_id, request_id)

        return accept

    def _accept(self, datagram: bytes, engine: _Engine, msg_id: int, request_id: int) -> DecodedPdu | _Report | None:
        """Return a response/report for this request, or None to drop the datagram."""
        try:
            message = _parse_message(datagram)
            if message.msg_id != msg_id:
                return None
            authenticated = bool(message.flags & FLAG_AUTH)
            if authenticated:
                if message.engine_id != engine.engine_id or len(message.auth_params) != engine.keys.mac_length:
                    return None
                zeroed = bytearray(datagram)
                zeroed[message.auth_span[0]:message.auth_span[1]] = bytes(len(message.auth_params))
                if not verify_mac(self._credentials.auth_protocol, engine.keys.auth_key, bytes(zeroed), message.auth_params):
                    return None
            elif message.flags & FLAG_PRIV:
                return None  # priv without auth is not a valid combination
            scoped = message.scoped
            if message.flags & FLAG_PRIV:
                scoped = decrypt_scoped_pdu(
                    engine.keys.priv_key, message.boots, message.time, message.priv_params, message.scoped
                )
            _context_engine, pdu = _parse_scoped(scoped)
        except (SNMPError, ValueError):
            return None
        if pdu.pdu_tag == REPORT:
            reason = next((_REPORT_REASONS[v.oid] for v in pdu.varbinds if v.oid in _REPORT_REASONS), None)
            if reason is None:
                return None
            return _Report(reason, message.engine_id, message.boots, message.time, authenticated)
        if not authenticated or pdu.request_id != request_id:
            return None
        if message.boots != engine.boots or abs(message.time - self._estimated_time(engine)) > TIME_WINDOW_SECONDS:
            return None
        return pdu

