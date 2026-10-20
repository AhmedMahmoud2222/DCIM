"""SNMPv2c session for table walks (GETNEXT/GETBULK) used by neighbor discovery.

The original `SNMPv2cCollector` in `snmp.py` (single mapped scalar GET) is unchanged; this
session adds the operations discovery needs on top of the same BER codec, target policy,
timeout and bounded-retry behaviour as the SNMPv3 session. The community string is never
included in `repr` and never logged.
"""

from __future__ import annotations

import hmac
import secrets

from .snmp import (
    SNMPAuthenticationError,
    SNMPError,
    SNMPMalformedValueError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPUnknownOIDError,
    _BERReader,
    _integer,
    _tlv,
)
from .snmp_wire import (
    DEFAULT_MAX_REPETITIONS,
    DEFAULT_WALK_ROWS,
    GET_BULK_REQUEST,
    GET_NEXT_REQUEST,
    GET_REQUEST,
    GET_RESPONSE,
    DecodedPdu,
    Varbind,
    WalkResult,
    decode_pdu,
    encode_pdu,
    udp_exchange,
    walk,
)


class SNMPv2cSession:
    def __init__(
        self,
        target: SNMPTarget,
        community: str,
        *,
        policy: SNMPTargetPolicy | None = None,
        timeout_seconds: float = 5.0,
        retries: int = 1,
    ) -> None:
        if not community:
            raise SNMPAuthenticationError("SNMP community is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retries < 0:
            raise ValueError("retries must not be negative")
        self.target = target
        self._community = community.encode("utf-8")
        self._policy = policy or SNMPTargetPolicy()
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def __repr__(self) -> str:
        return f"SNMPv2cSession(target={self.target!r})"

    def get(self, oid: str) -> Varbind:
        pdu = self._request(GET_REQUEST, 0, 0, [oid])
        if len(pdu.varbinds) != 1 or pdu.varbinds[0].oid != oid:
            raise SNMPError("SNMP response OID did not match request")
        if pdu.varbinds[0].is_exception:
            raise SNMPUnknownOIDError("OID is not available on the agent")
        return pdu.varbinds[0]

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

    def _request(self, pdu_tag: int, field_one: int, field_two: int, oids: list[str]) -> DecodedPdu:
        request_id = secrets.randbelow(2**31 - 1) + 1
        pdu = encode_pdu(pdu_tag, request_id, field_one, field_two, oids)
        datagram = _tlv(0x30, _integer(1) + _tlv(0x04, self._community) + pdu)

        def accept(data: bytes) -> DecodedPdu | None:
            try:
                message = _BERReader(data)
                body = message.read_constructed(0x30)
                if not message.exhausted or body.read_integer() != 1:
                    return None
                if not hmac.compare_digest(body.read_tlv(0x04), self._community):
                    return None
                tag, value = body.read_any_tlv()
                if tag != GET_RESPONSE or not body.exhausted:
                    return None
                decoded = decode_pdu(tag, value)
            except SNMPError:
                return None
            return decoded if decoded.request_id == request_id else None

        response = udp_exchange(
            target=self.target, policy=self._policy, datagram=datagram, accept=accept,
            timeout_seconds=self.timeout_seconds, retries=self.retries,
        )
        if response.error_status:
            raise SNMPError("SNMP agent returned an error status")
        return response
