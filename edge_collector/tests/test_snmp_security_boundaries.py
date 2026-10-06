"""Adversarial response-binding and work-budget regressions for Issue #101."""
import pytest

from edge_collector import snmp_v3 as wire
from edge_collector.snmp import SNMPTarget, _tlv
from edge_collector.snmp_usm import SNMPv3Credentials, compute_mac, derive_keys, encrypt_scoped_pdu
from edge_collector.snmp_wire import GET_RESPONSE, TAG_NO_SUCH_INSTANCE, Varbind, encode_pdu, walk


def response(*, private=True, user=b"poller", context=b"", context_engine=b"engine-id"):
    credentials = SNMPv3Credentials("poller", "sha256", "auth-password", "aes128", "priv-password")
    session = wire.SNMPv3Session(SNMPTarget("192.0.2.1"), credentials, clock=lambda: 0.0)
    keys = derive_keys(credentials, b"engine-id")
    engine = wire._Engine(b"engine-id", 1, 10, 0.0, keys)
    scoped = _tlv(0x30, _tlv(0x04, context_engine) + _tlv(0x04, context)
                  + encode_pdu(GET_RESPONSE, 7, 0, 0, ["1.3.6.1.2.1.1.1.0"]))
    salt = b"12345678" if private else b""
    data = _tlv(0x04, encrypt_scoped_pdu(keys.priv_key, 1, 10, salt, scoped)) if private else scoped
    flags = wire.FLAG_AUTH | (wire.FLAG_PRIV if private else 0)

    def assemble(mac):
        return wire._assemble(8, flags, wire._encode_usm(b"engine-id", 1, 10, user, mac, salt), data)

    packet = assemble(compute_mac("sha256", keys.auth_key, assemble(bytes(keys.mac_length))))
    return session._accept(packet, engine, 8, 7)


def test_accepts_bound_authpriv_response():
    assert response() is not None


@pytest.mark.parametrize("changes", [
    {"private": False},
    {"user": b"different-user"},
    {"context": b"different-context"},
    {"context_engine": b"different-engine"},
])
def test_rejects_security_level_or_identity_context_mismatch(changes):
    assert response(**changes) is None


def test_exception_varbinds_consume_walk_budget():
    calls = []

    def bulk(_cursor, _repetitions):
        calls.append(1)
        assert len(calls) <= 4, "walk exceeded the work budget"
        return [Varbind(f"1.3.6.1.{len(calls)}", TAG_NO_SUCH_INSTANCE, b"")]

    result = walk(bulk, "1.3.6.1", max_rows=3)
    assert result.truncated
    assert result.rows == ()
    assert len(calls) <= 4
