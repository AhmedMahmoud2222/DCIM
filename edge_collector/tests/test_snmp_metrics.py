"""SEC-04: telemetry metric polling over SNMPv3 authPriv, SNMPv2c only as an explicit legacy opt-in.

Every wire test uses real UDP on loopback against the independent in-test agents from
`test_snmp_v3.py` (their own BER reader, `hmac`/`cryptography` crypto), so the collector's
encryption, authentication and scaling are checked against code that does not share its
implementation.
"""

from __future__ import annotations

import ipaddress
import json
import os

import pytest
from test_snmp_v3 import (
    AUTH_SECRET,
    ENGINE_ID,
    PRIV_SECRET,
    USER,
    UsmAgent,
    V2cAgent,
    _int,
    _oid,
    _tlv,
    agent,
)

from edge_collector import snmp_wire
from edge_collector.credentials import CredentialStoreError, LocalCredentialStore
from edge_collector.discovery_sessions import BoundedWalker, local_session_factory
from edge_collector.snmp import (
    SNMPError,
    SNMPMalformedValueError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTargetPolicyError,
    SNMPTimeoutError,
    SNMPUnknownOIDError,
)
from edge_collector.snmp_metrics import (
    SNMPLegacyV2cNotEnabledError,
    collect_metric,
    metric_from_varbind,
    metric_session_for,
)
from edge_collector.snmp_usm import localize_key, password_to_key
from edge_collector.snmp_v2c import SNMPv2cSession
from edge_collector.snmp_v3 import SNMPv3AuthenticationError, SNMPv3Session
from edge_collector.snmp_wire import Varbind

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_UPTIME_OID_BYTES = bytes.fromhex("2b06010201010300")
COMMUNITY = "legacy-community-secret"
INTEGRATION = "6f1c2a8e-5b1d-4c7e-9a43-0d2f7b9e1c55"


def v3_entry(**changes) -> dict:
    values = {
        "username": USER, "auth_protocol": "sha256", "auth_secret": AUTH_SECRET,
        "priv_protocol": "aes128", "priv_secret": PRIV_SECRET,
    }
    return {"snmpv3": values | changes}


def poll(store: LocalCredentialStore, port: int, *, timeout: float = 1.0, retries: int = 1, oid: str = SYS_UPTIME):
    policy = SNMPTargetPolicy(
        allowed_networks=(ipaddress.ip_network("127.0.0.0/8"),), allowed_ports=frozenset({port}), allow_loopback=True
    )
    session = metric_session_for(
        store, INTEGRATION, SNMPTarget("127.0.0.1", port), policy=policy, timeout_seconds=timeout, retries=retries
    )
    return collect_metric(session, oid)


class _NoNetwork:
    """Stands in for the `socket` module inside snmp_wire: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"metric polling touched the network (socket.{name})")


# --------------------------------------------------------------------- SNMPv3 authPriv
@pytest.mark.parametrize(("auth", "priv"), [("sha1", "aes128"), ("sha256", "aes128"), ("sha512", "aes256")])
def test_v3_metric_is_decrypted_authenticated_and_scaled(auth, priv):
    with agent(auth=auth) as server:
        if priv == "aes256":
            # The agent uses a 128-bit privacy key by default. AES-256 takes the first 32 bytes of
            # the localized key; snmp_usm requires a hash at least that long (no key extension).
            server.priv_key = localize_key(password_to_key(PRIV_SECRET, auth), ENGINE_ID, auth)[:32]
        metric = poll(LocalCredentialStore({INTEGRATION: v3_entry(auth_protocol=auth, priv_protocol=priv)}), server.port)
        # Agent answers TimeTicks 0x010203 = 66051 centiseconds.
        assert (metric.source_identifier, metric.canonical_metric, metric.unit) == (SYS_UPTIME, "uptime_seconds", "seconds")
        assert metric.value == pytest.approx(660.51)
        assert server.requests == 2  # engine discovery, then one authPriv GET


def test_v3_metric_request_on_the_wire_is_authpriv_and_carries_no_secret_or_oid():
    with agent() as server:
        poll(LocalCredentialStore({INTEGRATION: v3_entry() | {"community": COMMUNITY}}), server.port)
        secured = server.received[-1]
        msg = server._parse(secured)
        assert msg["flags"] & 0x03 == 0x03 and msg["engine_id"] == ENGINE_ID and len(msg["priv_params"]) == 8
        for datagram in server.received:
            assert SYS_UPTIME_OID_BYTES not in datagram  # the polled OID is only inside the ciphertext
            for secret in (AUTH_SECRET, PRIV_SECRET, COMMUNITY):
                assert secret.encode() not in datagram


def test_v3_metric_unknown_oid_is_refused_before_any_packet():
    with agent() as server:
        with pytest.raises(SNMPUnknownOIDError):
            poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port, oid="1.3.6.1.2.1.1.1.0")
        assert server.requests == 0


def test_v3_metric_oid_missing_on_the_agent_is_unknown_oid():
    with agent() as server:
        del server.mib[SYS_UPTIME]  # agent answers noSuchInstance
        with pytest.raises(SNMPUnknownOIDError):
            poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port)


@pytest.mark.parametrize("value", [_tlv(0x04, b"not-a-number"), _tlv(0x04, b"\xff\xfe"), _oid("1.3.6.1")])
def test_v3_metric_non_numeric_value_is_rejected_not_coerced(value):
    with agent() as server:
        server.mib[SYS_UPTIME] = value
        with pytest.raises(SNMPMalformedValueError):
            poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port)


def test_v3_metric_numeric_octet_string_is_accepted_like_the_v2c_collector():
    with agent() as server:
        server.mib[SYS_UPTIME] = _tlv(0x04, b"12345")
        assert poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port).value == pytest.approx(123.45)


class TamperingAgent(UsmAgent):
    """Answers every GET with a forged response; discovery is left intact."""

    def __init__(self, mode: str, **kwargs):
        super().__init__(**kwargs)
        self.mode = mode

    def _response(self, msg_id, request_id, varbinds, **kwargs):
        genuine = super()._response(msg_id, request_id, varbinds, **kwargs)
        if self.mode == "flip_ciphertext":
            tampered = bytearray(genuine)
            tampered[-1] ^= 0x01  # last byte of the encrypted scoped PDU; MAC no longer matches
            return bytes(tampered)
        if self.mode == "plaintext_downgrade":
            # A noAuthNoPriv answer with a cleartext scoped PDU and a forged value.
            body = _tlv(0x30, _oid(SYS_UPTIME) + _tlv(0x43, b"\x7f\xff\xff"))
            pdu = _tlv(0xA2, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, body))
            scoped = _tlv(0x30, _tlv(0x04, ENGINE_ID) + _tlv(0x04, b"") + pdu)
            usm = self._usm(ENGINE_ID, self.boots, self.engine_time(), USER.encode(), b"", b"")
            return _tlv(0x30, _int(3) + self._header(msg_id, 0x00) + _tlv(0x04, usm) + scoped)
        raise AssertionError(self.mode)


@pytest.mark.parametrize("mode", ["flip_ciphertext", "plaintext_downgrade"])
def test_v3_metric_tampered_or_downgraded_response_is_never_accepted(mode):
    server = TamperingAgent(mode)
    server.start()
    try:
        with pytest.raises(SNMPTimeoutError):
            poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port, timeout=0.3, retries=0)
    finally:
        server.stop()


def test_v3_metric_garbage_then_genuine_reply_still_yields_the_metric():
    with agent(faults=[None, "garbage"]) as server:
        assert poll(LocalCredentialStore({INTEGRATION: v3_entry()}), server.port, timeout=0.4).value == pytest.approx(660.51)


def test_v3_metric_wrong_secret_is_an_authentication_error_without_leaking_it():
    with agent() as server:
        with pytest.raises(SNMPv3AuthenticationError) as error:
            poll(LocalCredentialStore({INTEGRATION: v3_entry(auth_secret="wrong-auth-secret")}), server.port)
        for secret in ("wrong-auth-secret", AUTH_SECRET, PRIV_SECRET):
            assert secret not in str(error.value)


def test_v3_metric_target_policy_applies_before_any_packet():
    with agent() as server:
        session = metric_session_for(
            LocalCredentialStore({INTEGRATION: v3_entry()}), INTEGRATION, SNMPTarget("127.0.0.1", server.port),
            policy=SNMPTargetPolicy(),
        )
        with pytest.raises(SNMPTargetPolicyError):
            collect_metric(session, SYS_UPTIME)
        assert server.requests == 0


# ------------------------------------------------------------------ legacy v2c fail-closed
@pytest.mark.parametrize(
    "entry",
    [
        {"community": COMMUNITY},
        {"community": COMMUNITY, "allow_legacy_v2c": False},
        {"community": COMMUNITY, "allow_legacy_v2c": "true"},  # only a JSON boolean counts
        {"community": COMMUNITY, "allow_legacy_v2c": 1},
    ],
)
def test_v2c_only_entry_without_opt_in_is_refused_with_zero_packets(entry, monkeypatch):
    monkeypatch.setattr(snmp_wire, "socket", _NoNetwork())
    with V2cAgent(community=COMMUNITY.encode()) as server:
        server.rows[SYS_UPTIME] = _tlv(0x43, b"\x01\x02\x03")
        with pytest.raises(SNMPLegacyV2cNotEnabledError, match="legacy SNMPv2c metric collection is not enabled") as error:
            poll(LocalCredentialStore({INTEGRATION: entry}), server.port)
        assert server.requests == 0
        assert COMMUNITY not in str(error.value) and COMMUNITY not in repr(error.value)
    assert isinstance(error.value, SNMPTargetPolicyError)


def test_v2c_with_explicit_opt_in_still_collects_for_legacy_devices():
    with V2cAgent(community=COMMUNITY.encode()) as server:
        server.rows[SYS_UPTIME] = _tlv(0x43, b"\x01\x02\x03")
        metric = poll(LocalCredentialStore({INTEGRATION: {"community": COMMUNITY, "allow_legacy_v2c": True}}), server.port)
        assert (metric.canonical_metric, metric.unit) == ("uptime_seconds", "seconds")
        assert metric.value == pytest.approx(660.51)
        assert server.requests == 1


def test_v2c_opt_in_metric_unknown_and_malformed_values_match_the_collector():
    store = LocalCredentialStore({INTEGRATION: {"community": COMMUNITY, "allow_legacy_v2c": True}})
    with V2cAgent(community=COMMUNITY.encode()) as server:
        with pytest.raises(SNMPUnknownOIDError):
            poll(store, server.port, oid="1.3.6.1.2.1.1.5.0")
        assert server.requests == 0
        with pytest.raises(SNMPUnknownOIDError):  # sysUpTime absent: agent answers noSuchInstance
            poll(store, server.port)
        server.rows[SYS_UPTIME] = _tlv(0x04, b"abc")
        with pytest.raises(SNMPMalformedValueError):
            poll(store, server.port)


def test_opt_in_never_downgrades_an_integration_that_has_v3_credentials():
    entry = v3_entry() | {"community": COMMUNITY, "allow_legacy_v2c": True}
    with agent() as server:
        metric = poll(LocalCredentialStore({INTEGRATION: entry}), server.port)
        assert metric.value == pytest.approx(660.51)
        for datagram in server.received:
            assert COMMUNITY.encode() not in datagram
            assert server._parse(datagram)["engine_id"] in (b"", ENGINE_ID)  # every datagram is an SNMPv3 message
        assert server._parse(server.received[-1])["flags"] & 0x03 == 0x03


def test_invalid_v3_entry_never_falls_back_to_an_opted_in_community(monkeypatch):
    monkeypatch.setattr(snmp_wire, "socket", _NoNetwork())
    entry = v3_entry(auth_protocol="md5", priv_protocol="des") | {"community": COMMUNITY, "allow_legacy_v2c": True}
    with pytest.raises(CredentialStoreError) as error:
        poll(LocalCredentialStore({INTEGRATION: entry}), 16100)
    assert COMMUNITY not in str(error.value) and AUTH_SECRET not in str(error.value)


def test_missing_integration_is_refused():
    with pytest.raises(CredentialStoreError):
        poll(LocalCredentialStore({}), 16100)


def test_opt_in_is_read_from_the_private_credential_file(tmp_path):
    path = tmp_path / "snmp.json"
    path.write_text(json.dumps({
        INTEGRATION: {"community": COMMUNITY, "allow_legacy_v2c": True},
        "other": {"community": COMMUNITY},
    }))
    os.chmod(path, 0o600)
    store = LocalCredentialStore.load(path)
    assert store.legacy_v2c_allowed(INTEGRATION) is True
    assert store.legacy_v2c_allowed("other") is False and store.legacy_v2c_allowed("unknown") is False
    assert COMMUNITY not in repr(store)


# ----------------------------------------------------------------------- scope guards
def test_discovery_still_uses_a_community_only_entry_without_the_metric_opt_in():
    store = LocalCredentialStore({INTEGRATION: {"community": COMMUNITY}})
    create = local_session_factory(store, SNMPTargetPolicy())
    walker = create({"integration_id": INTEGRATION, "snmp_version": "v2c"}, SNMPTarget("192.0.2.1"))
    assert isinstance(walker, BoundedWalker)
    assert isinstance(store.session_for(INTEGRATION, SNMPTarget("192.0.2.1"), policy=SNMPTargetPolicy()), SNMPv2cSession)


def test_metric_session_for_returns_the_v3_session_unchanged():
    store = LocalCredentialStore({INTEGRATION: v3_entry()})
    session = metric_session_for(store, INTEGRATION, SNMPTarget("192.0.2.1"), policy=SNMPTargetPolicy())
    assert isinstance(session, SNMPv3Session)
    assert AUTH_SECRET not in repr(session) and PRIV_SECRET not in repr(session)


@pytest.mark.parametrize(
    ("varbind", "error"),
    [
        (Varbind("1.3.6.1.2.1.1.5.0", 0x43, b"\x01"), SNMPError),  # response for another OID
        (Varbind(SYS_UPTIME, 0x80, b""), SNMPUnknownOIDError),  # noSuchObject
        (Varbind(SYS_UPTIME, 0x82, b""), SNMPUnknownOIDError),  # endOfMibView
        (Varbind(SYS_UPTIME, 0x05, b""), SNMPMalformedValueError),  # NULL
    ],
)
def test_metric_from_varbind_rejects_what_the_collector_rejects(varbind, error):
    with pytest.raises(error):
        metric_from_varbind(SYS_UPTIME, varbind)


@pytest.mark.parametrize(
    ("tag", "raw", "expected"),
    [(0x02, b"\xff", -0.01), (0x41, b"\xff", 2.55), (0x42, b"\x00\x64", 1.0), (0x43, b"\x01\x02\x03", 660.51)],
)
def test_metric_from_varbind_uses_the_collector_numeric_rules(tag, raw, expected):
    assert metric_from_varbind(SYS_UPTIME, Varbind(SYS_UPTIME, tag, raw)).value == pytest.approx(expected)
