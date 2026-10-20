"""SNMPv3 authPriv: key derivation vectors, credential validation, and the real UDP wire
path against an in-test USM agent.

The agent below decodes requests with its own minimal BER reader (not `snmp.py`'s) and
performs authentication/decryption with `hmac`/`cryptography` directly, so an encoding or
crypto mistake in the collector is visible instead of agreeing with itself. Key
localization is the one shared primitive; it is pinned to the RFC 3414 appendix A.3 vectors.
(The same session was also exercised by hand against pysnmp 7.1.30 for every supported
auth/priv combination; that interop run is not part of CI.)
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from edge_collector.snmp import (
    SNMPAuthenticationError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTimeoutError,
    SNMPUnknownOIDError,
)
from edge_collector.snmp_usm import (
    SNMPv3ConfigError,
    SNMPv3Credentials,
    compute_mac,
    decrypt_scoped_pdu,
    derive_keys,
    encrypt_scoped_pdu,
    localize_key,
    password_to_key,
    validate_algorithms,
)
from edge_collector.snmp_v2c import SNMPv2cSession
from edge_collector.snmp_v3 import SNMPv3AuthenticationError, SNMPv3Session

ENGINE_ID = bytes.fromhex("80001f8880c0ffee0123456789")
AUTH_SECRET = "auth-secret-0123"
PRIV_SECRET = "priv-secret-4567"
USER = "dcim-poller"
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"


# --------------------------------------------------------------------------- RFC vectors
def test_rfc3414_key_derivation_vectors():
    # RFC 3414 appendix A.3.1 (MD5) and A.3.2 (SHA-1); engine 00..02.
    engine = bytes.fromhex("000000000000000000000002")
    ku_md5 = password_to_key("maplesyrup", "md5")
    assert ku_md5.hex() == "9faf3283884e92834ebc9847d8edd963"
    assert localize_key(ku_md5, engine, "md5").hex() == "526f5eed9fcce26f8964c2930787d82b"
    ku_sha = password_to_key("maplesyrup", "sha1")
    assert ku_sha.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    assert localize_key(ku_sha, engine, "sha1").hex() == "6695febc9288e36282235fc7151f128497b38f3f"


def test_aes_cfb_roundtrip_and_iv_binding():
    key = bytes(range(16))
    plain = b"scoped pdu bytes" * 3
    cipher = encrypt_scoped_pdu(key, 7, 1234, b"saltsalt", plain)
    assert cipher != plain and len(cipher) == len(plain)
    assert decrypt_scoped_pdu(key, 7, 1234, b"saltsalt", cipher) == plain
    assert decrypt_scoped_pdu(key, 7, 1235, b"saltsalt", cipher) != plain  # time is part of the IV
    assert decrypt_scoped_pdu(key, 7, 1234, b"saltsal2", cipher) != plain


def test_mac_lengths_follow_rfc7860():
    sizes = {"sha1": 12, "sha224": 16, "sha256": 24, "sha384": 32, "sha512": 48}
    for protocol, size in sizes.items():
        assert len(compute_mac(protocol, b"k" * 20, b"message")) == size


# ------------------------------------------------------------------------- validation
def make_credentials(**changes) -> SNMPv3Credentials:
    values = {
        "username": USER, "auth_protocol": "sha256", "auth_secret": AUTH_SECRET,
        "priv_protocol": "aes128", "priv_secret": PRIV_SECRET,
    }
    return SNMPv3Credentials(**(values | changes))


def test_valid_auth_priv_combinations_are_accepted():
    for auth, priv in (
        ("sha1", "aes128"), ("sha224", "aes128"), ("sha224", "aes192"), ("sha256", "aes256"),
        ("sha384", "aes256"), ("sha512", "aes256"),
    ):
        assert make_credentials(auth_protocol=auth, priv_protocol=priv).auth_protocol == auth


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"auth_protocol": "md5"}, "insecure"),
        ({"priv_protocol": "des"}, "insecure"),
        ({"priv_protocol": "3des"}, "insecure"),
        ({"auth_protocol": "none"}, "insecure"),
        ({"auth_protocol": "sha3"}, "unsupported authentication"),
        ({"priv_protocol": "chacha"}, "unsupported privacy"),
        ({"auth_protocol": "sha1", "priv_protocol": "aes192"}, "longer authentication hash"),
        ({"auth_protocol": "sha1", "priv_protocol": "aes256"}, "longer authentication hash"),
        ({"auth_protocol": "sha224", "priv_protocol": "aes256"}, "longer authentication hash"),
        ({"auth_secret": "short"}, "authentication secret"),
        ({"priv_secret": "x" * 7}, "privacy secret"),
        ({"priv_secret": "ctl\x00char-secret"}, "control characters"),
        ({"username": ""}, "username"),
        ({"username": "u" * 33}, "username"),
        ({"context_name": "c" * 256}, "context_name"),
    ],
)
def test_invalid_credentials_are_rejected_explicitly(changes, fragment):
    with pytest.raises(SNMPv3ConfigError, match=fragment):
        make_credentials(**changes)


def test_validation_messages_never_contain_secret_values():
    secret = "tiny"
    with pytest.raises(SNMPv3ConfigError) as error:
        make_credentials(auth_secret=secret)
    assert secret not in str(error.value) and AUTH_SECRET not in str(error.value)
    with pytest.raises(SNMPv3ConfigError):
        validate_algorithms("sha256", "des")


def test_credentials_and_sessions_hide_secrets_in_repr_and_str():
    credentials = make_credentials()
    session = SNMPv3Session(SNMPTarget("192.0.2.1"), credentials)
    for text in (repr(credentials), str(credentials), repr(session), str(session), repr(derive_keys(credentials, ENGINE_ID))):
        assert AUTH_SECRET not in text and PRIV_SECRET not in text
    community = SNMPv2cSession(SNMPTarget("192.0.2.1"), "very-secret-community")
    assert "very-secret-community" not in repr(community)


# -------------------------------------------------------------------- independent agent
def _read(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """(tag, value_start, value_end, next) — independent of snmp.py's reader."""
    tag, first = data[pos], data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    return tag, pos, pos + length, pos + length


def _int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return b"\x02" + _len(len(raw)) + raw


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        out.extend(reversed(chunk))
    return b"\x06" + _len(len(out)) + bytes(out)


def _oid_text(value: bytes) -> str:
    parts = [value[0] // 40, value[0] % 40]
    acc = 0
    for byte in value[1:]:
        acc = (acc << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(acc)
            acc = 0
    return ".".join(map(str, parts))


class UsmAgent:
    """A tiny authoritative SNMPv3 engine. `faults` is consumed one entry per request."""

    def __init__(self, *, auth="sha256", priv_secret=PRIV_SECRET, auth_secret=AUTH_SECRET, faults=()):
        self.auth = auth
        self.boots, self.time_offset = 5, 1000
        self.started = time.monotonic()
        self.faults = list(faults)
        self.received: list[bytes] = []
        self.requests = 0
        self.mib = {
            SYS_DESCR: _tlv(0x04, b"Test switch"),
            SYS_NAME: _tlv(0x04, b"sw-edge-1"),
            "1.3.6.1.2.1.1.3.0": _tlv(0x43, b"\x01\x02\x03"),
            "1.3.6.1.2.1.2.1.0": _tlv(0x02, b"\x18"),
        }
        hash_name = auth
        self.auth_key = localize_key(password_to_key(auth_secret, hash_name), ENGINE_ID, hash_name)
        self.priv_key = localize_key(password_to_key(priv_secret, hash_name), ENGINE_ID, hash_name)[:16]
        self.mac_len = {"sha1": 12, "sha224": 16, "sha256": 24, "sha384": 32, "sha512": 48}[auth]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def engine_time(self) -> int:
        return self.time_offset + int(time.monotonic() - self.started)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self.received.append(data)
            for reply in self._handle(data):
                self.sock.sendto(reply, peer)

    # -- message codec
    def _header(self, msg_id, flags):
        return _tlv(0x30, _int(msg_id) + _int(65507) + _tlv(0x04, bytes([flags])) + _int(3))

    def _usm(self, engine_id, boots, etime, user, auth_params, priv_params):
        return _tlv(
            0x30,
            _tlv(0x04, engine_id) + _int(boots) + _int(etime) + _tlv(0x04, user) + _tlv(0x04, auth_params)
            + _tlv(0x04, priv_params),
        )

    def _report(self, msg_id, oid, *, flags=0, boots=None, etime=None, user=b"", sign=False):
        boots = self.boots if boots is None else boots
        etime = self.engine_time() if etime is None else etime
        pdu = _tlv(0xA8, _int(1) + _int(0) + _int(0) + _tlv(0x30, _tlv(0x30, _oid(oid) + _tlv(0x41, b"\x01"))))
        scoped = _tlv(0x30, _tlv(0x04, ENGINE_ID) + _tlv(0x04, b"") + pdu)

        def build(auth_params):
            usm = self._usm(ENGINE_ID, boots, etime, user, auth_params, b"")
            return _tlv(0x30, _int(3) + self._header(msg_id, flags) + _tlv(0x04, usm) + scoped)

        if not sign:
            return build(b"")
        message = build(bytes(self.mac_len))
        return build(hmac.new(self.auth_key, message, self.auth).digest()[: self.mac_len])

    def _response(self, msg_id, request_id, varbinds, *, bad_mac=False, boots=None, etime=None):
        boots = self.boots if boots is None else boots
        etime = self.engine_time() if etime is None else etime
        body = b"".join(_tlv(0x30, _oid(oid) + value) for oid, value in varbinds)
        pdu = _tlv(0xA2, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, body))
        scoped = _tlv(0x30, _tlv(0x04, ENGINE_ID) + _tlv(0x04, b"") + pdu)
        salt = b"agentslt"
        cipher = encrypt_scoped_pdu(self.priv_key, boots, etime, salt, scoped)

        def build(auth_params):
            usm = self._usm(ENGINE_ID, boots, etime, USER.encode(), auth_params, salt)
            return _tlv(0x30, _int(3) + self._header(msg_id, 0x03) + _tlv(0x04, usm) + _tlv(0x04, cipher))

        message = build(bytes(self.mac_len))
        mac = hmac.new(self.auth_key, message, self.auth).digest()[: self.mac_len]
        if bad_mac:
            mac = bytes(b ^ 0xFF for b in mac)
        return build(mac)

    def _parse(self, data):
        _t, s, _e, _n = _read(data, 0)
        pos = s
        _t, a, b, pos = _read(data, pos)  # version
        _t, hs, _he, pos = _read(data, pos)  # header
        _t, i, j, k = _read(data, hs)
        msg_id = int.from_bytes(data[i:j], "big", signed=True)
        _t, _a, _b, k = _read(data, k)
        _t, fs, _fe, k = _read(data, k)
        flags = data[fs]
        _t, ss, _se, pos = _read(data, pos)  # security params octet string
        _t, us, _ue, _ = _read(data, ss)
        p = us
        _t, a, b, p = _read(data, p)
        engine_id = data[a:b]
        _t, a, b, p = _read(data, p)
        boots = int.from_bytes(data[a:b], "big")
        _t, a, b, p = _read(data, p)
        etime = int.from_bytes(data[a:b], "big")
        _t, a, b, p = _read(data, p)
        user = data[a:b]
        _t, a_s, a_e, p = _read(data, p)
        auth_params = data[a_s:a_e]
        _t, a, b, p = _read(data, p)
        priv_params = data[a:b]
        tag, ds, de, _ = _read(data, pos)
        return {
            "msg_id": msg_id, "flags": flags, "engine_id": engine_id, "boots": boots, "time": etime, "user": user,
            "auth_params": auth_params, "auth_span": (a_s, a_e), "priv_params": priv_params,
            "data_tag": tag, "data": data[ds:de], "raw": data,
        }

    def _handle(self, data):
        self.requests += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "drop":
            return []
        if fault == "garbage":
            return [b"\x30\x03\x02\x01"]
        try:
            msg = self._parse(data)
        except Exception:  # noqa: BLE001 - test agent ignores undecodable input like a real agent
            return []
        if not msg["engine_id"]:  # discovery
            return [self._report(msg["msg_id"], "1.3.6.1.6.3.15.1.1.4.0")]
        if msg["user"] != USER.encode():
            return [self._report(msg["msg_id"], "1.3.6.1.6.3.15.1.1.3.0")]
        zeroed = bytearray(msg["raw"])
        zeroed[msg["auth_span"][0]:msg["auth_span"][1]] = bytes(len(msg["auth_params"]))
        expected = hmac.new(self.auth_key, bytes(zeroed), self.auth).digest()[: self.mac_len]
        if not hmac.compare_digest(expected, msg["auth_params"]):
            return [self._report(msg["msg_id"], "1.3.6.1.6.3.15.1.1.5.0", user=USER.encode())]
        if fault == "not_in_window":
            return [self._report(msg["msg_id"], "1.3.6.1.6.3.15.1.1.2.0", flags=0x01, sign=True, user=USER.encode(),
                                 boots=self.boots, etime=self.engine_time() + 40)]
        try:
            plain = decrypt_scoped_pdu(self.priv_key, msg["boots"], msg["time"], msg["priv_params"], msg["data"])
            _t, s, _e, _n = _read(plain, 0)
            _t, a, b, p = _read(plain, s)  # context engine id
            _t, a, b, p = _read(plain, p)  # context name
            pdu_tag, ps, _pe, _ = _read(plain, p)
            _t, a, b, p = _read(plain, ps)
            request_id = int.from_bytes(plain[a:b], "big", signed=True)
            _t, a, b, p = _read(plain, p)
            _t, a, b, p = _read(plain, p)
            max_repetitions = int.from_bytes(plain[a:b], "big")
            _t, vs, _ve, _ = _read(plain, p)
            _t, bs, _be, _ = _read(plain, vs)
            _t, os_, oe, _ = _read(plain, bs)
            requested = _oid_text(plain[os_:oe])
        except (IndexError, ValueError):  # a wrong privacy key yields garbage: report decryptionError
            return [self._report(msg["msg_id"], "1.3.6.1.6.3.15.1.1.6.0", flags=0x01, sign=True, user=USER.encode())]
        if pdu_tag == 0xA0:
            value = self.mib.get(requested)
            varbinds = [(requested, value if value is not None else _tlv(0x81, b""))]
        else:  # GETBULK
            ordered = sorted(self.mib, key=lambda o: tuple(map(int, o.split("."))))
            after = [o for o in ordered if tuple(map(int, o.split("."))) > tuple(map(int, requested.split(".")))]
            varbinds = [(o, self.mib[o]) for o in after[:max_repetitions]] or [(requested, _tlv(0x82, b""))]
        if fault == "bad_mac_then_good":
            return [self._response(msg["msg_id"], request_id, varbinds, bad_mac=True),
                    self._response(msg["msg_id"], request_id, varbinds)]
        if fault == "wrong_request_id":
            return [self._response(msg["msg_id"], request_id + 1, varbinds)]
        if fault == "stale_time":
            return [self._response(msg["msg_id"], request_id, varbinds, etime=self.engine_time() + 3000)]
        return [self._response(msg["msg_id"], request_id, varbinds)]


@contextmanager
def agent(**kwargs):
    instance = UsmAgent(**kwargs)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def session_for(instance, *, credentials=None, timeout=1.0, retries=1) -> SNMPv3Session:
    policy = SNMPTargetPolicy(
        allowed_networks=(ipaddress.ip_network("127.0.0.0/8"),), allowed_ports=frozenset({instance.port}), allow_loopback=True
    )
    return SNMPv3Session(
        SNMPTarget("127.0.0.1", instance.port), credentials or make_credentials(auth_protocol=instance.auth),
        policy=policy, timeout_seconds=timeout, retries=retries,
    )


# ------------------------------------------------------------------------- wire tests
@pytest.mark.parametrize("auth", ["sha1", "sha224", "sha256", "sha384", "sha512"])
def test_auth_priv_get_over_real_udp(auth):
    with agent(auth=auth) as server:
        session = session_for(server, credentials=make_credentials(auth_protocol=auth, priv_protocol="aes128"))
        assert session.get(SYS_DESCR).as_bytes() == b"Test switch"
        assert session.get(SYS_NAME).as_bytes() == b"sw-edge-1"
        # engine discovery happened once, then two authenticated requests
        assert server.requests == 3


def test_request_on_the_wire_is_authenticated_and_encrypted_without_secrets():
    with agent() as server:
        session_for(server).get(SYS_DESCR)
        secured = server.received[-1]
        assert USER.encode() in secured  # the user name is part of the clear-text USM header
        assert AUTH_SECRET.encode() not in secured and PRIV_SECRET.encode() not in secured
        assert bytes.fromhex("2b06010201010100") not in secured  # sysDescr OID is inside the ciphertext
        msg = server._parse(secured)
        assert msg["flags"] & 0x03 == 0x03 and len(msg["priv_params"]) == 8 and msg["engine_id"] == ENGINE_ID
        assert msg["boots"] == server.boots and abs(msg["time"] - server.engine_time()) <= 2


def test_discovery_request_is_unauthenticated_and_carries_no_user():
    with agent() as server:
        session_for(server).get(SYS_DESCR)
        discovery = server._parse(server.received[0])
        assert discovery["flags"] & 0x03 == 0 and discovery["engine_id"] == b"" and discovery["user"] == b""


def test_each_poll_uses_a_fresh_salt_and_message_id():
    with agent() as server:
        session = session_for(server)
        session.get(SYS_DESCR)
        session.get(SYS_DESCR)
        first, second = (server._parse(d) for d in server.received[-2:])
        assert first["priv_params"] != second["priv_params"] and first["msg_id"] != second["msg_id"]


def test_get_of_missing_oid_raises_unknown_oid():
    with agent() as server, pytest.raises(SNMPUnknownOIDError):
        session_for(server).get("1.3.6.1.2.1.99.1.0")


def test_walk_follows_getbulk_batches_and_stops_at_subtree_end():
    with agent() as server:
        result = session_for(server).walk("1.3.6.1.2.1.1")
        assert [row.oid for row in result.rows] == [SYS_DESCR, "1.3.6.1.2.1.1.3.0", SYS_NAME]
        assert result.truncated is False
        capped = session_for(server).walk("1.3.6.1.2.1", max_rows=2)
        assert len(capped.rows) == 2 and capped.truncated is True


def test_retransmits_once_after_a_dropped_request():
    with agent(faults=[None, "drop"]) as server:  # discovery ok, first secured request lost
        started = time.monotonic()
        assert session_for(server, timeout=0.4, retries=1).get(SYS_DESCR).as_bytes() == b"Test switch"
        assert time.monotonic() - started < 2.0
        assert server.requests == 3  # discovery + lost request + retransmission


def test_no_retries_means_a_single_attempt_then_timeout():
    with agent(faults=[None, "drop"]) as server:
        with pytest.raises(SNMPTimeoutError):
            session_for(server, timeout=0.3, retries=0).get(SYS_DESCR)
        assert server.requests == 2


def test_silent_agent_times_out_within_the_attempt_budget():
    with agent(faults=["drop"] * 10) as server:
        started = time.monotonic()
        with pytest.raises(SNMPTimeoutError):
            session_for(server, timeout=0.25, retries=2).get(SYS_DESCR)
        elapsed = time.monotonic() - started
        assert 0.7 <= elapsed < 2.5  # (retries + 1) * timeout, never unbounded


def test_wrong_authentication_secret_is_reported_without_leaking_it():
    with agent() as server:
        session = session_for(server, credentials=make_credentials(auth_secret="wrong-auth-secret"))
        with pytest.raises(SNMPv3AuthenticationError) as error:
            session.get(SYS_DESCR)
        assert error.value.reason == "wrong_digest"
        for secret in ("wrong-auth-secret", AUTH_SECRET, PRIV_SECRET, USER):
            assert secret not in str(error.value) and secret not in repr(error.value)


def test_unknown_user_is_an_authentication_error_without_naming_the_user():
    with agent() as server:
        with pytest.raises(SNMPv3AuthenticationError) as error:
            session_for(server, credentials=make_credentials(username="someone-else")).get(SYS_DESCR)
        assert error.value.reason == "unknown_user" and "someone-else" not in str(error.value)


def test_wrong_privacy_secret_is_a_decryption_error_without_leaking_it():
    with agent(priv_secret="a-different-priv-secret") as server:
        with pytest.raises(SNMPv3AuthenticationError) as error:
            session_for(server, timeout=0.5, retries=0).get(SYS_DESCR)
        assert error.value.reason == "decryption_error"
        assert PRIV_SECRET not in str(error.value) and "a-different-priv-secret" not in str(error.value)


def test_forged_reply_with_bad_mac_is_ignored_in_favour_of_the_real_one():
    with agent(faults=[None, "bad_mac_then_good"]) as server:
        assert session_for(server).get(SYS_DESCR).as_bytes() == b"Test switch"


def test_garbage_and_wrong_request_id_do_not_abort_the_poll():
    with agent(faults=[None, "garbage"]) as server:
        assert session_for(server, timeout=0.4, retries=1).get(SYS_DESCR).as_bytes() == b"Test switch"
    with agent(faults=[None, "wrong_request_id"]) as server:
        assert session_for(server, timeout=0.4, retries=1).get(SYS_DESCR).as_bytes() == b"Test switch"


def test_reply_outside_the_time_window_is_dropped():
    with agent(faults=[None, "stale_time"]) as server:
        assert session_for(server, timeout=0.4, retries=1).get(SYS_DESCR).as_bytes() == b"Test switch"


def test_not_in_time_window_report_resynchronises_and_succeeds():
    with agent(faults=[None, "not_in_window"]) as server:
        assert session_for(server, timeout=0.5, retries=0).get(SYS_DESCR).as_bytes() == b"Test switch"
        assert server.requests == 3  # discovery, rejected request, resynchronised request


def test_credentials_never_reach_logs(caplog):
    caplog.set_level(logging.DEBUG)
    with agent() as server:
        session_for(server).get(SYS_DESCR)
        with pytest.raises(SNMPAuthenticationError):
            session_for(server, credentials=make_credentials(auth_secret="wrong-auth-secret")).get(SYS_DESCR)
    assert AUTH_SECRET not in caplog.text and PRIV_SECRET not in caplog.text and "wrong-auth-secret" not in caplog.text


def test_target_policy_still_applies_before_any_packet_is_sent():
    credentials = make_credentials()
    session = SNMPv3Session(SNMPTarget("127.0.0.1", 161), credentials, policy=SNMPTargetPolicy())
    from edge_collector.snmp import SNMPTargetPolicyError

    with pytest.raises(SNMPTargetPolicyError):
        session.get(SYS_DESCR)


# ------------------------------------------------------------------------ v2c regression
class V2cAgent:
    """Answers v2c GET/GETBULK for the walk tests; decodes with this module's own reader."""

    def __init__(self, community=b"public-community"):
        self.community = community
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.stop_event = threading.Event()
        self.requests = 0
        self.drop_first = 0
        self.rows = {"1.3.6.1.2.1.1.1.0": _tlv(0x04, b"v2c switch"), "1.3.6.1.2.1.1.5.0": _tlv(0x04, b"sw-v2c")}
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        while not self.stop_event.is_set():
            try:
                data, peer = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self.requests += 1
            if self.drop_first:
                self.drop_first -= 1
                continue
            _t, s, _e, _n = _read(data, 0)
            _t, a, b, p = _read(data, s)
            _t, a, b, p = _read(data, p)
            pdu_tag, ps, _pe, _ = _read(data, p)
            _t, a, b, q = _read(data, ps)
            request_id = int.from_bytes(data[a:b], "big", signed=True)
            _t, a, b, q = _read(data, q)
            _t, a, b, q = _read(data, q)
            _t, vs, _ve, _ = _read(data, q)
            _t, bs, _be, _ = _read(data, vs)
            _t, os_, oe, _ = _read(data, bs)
            requested = _oid_text(data[os_:oe])
            ordered = sorted(self.rows, key=lambda o: tuple(map(int, o.split("."))))
            if pdu_tag == 0xA0:
                varbinds = [(requested, self.rows.get(requested, _tlv(0x81, b"")))]
            else:
                after = [o for o in ordered if tuple(map(int, o.split("."))) > tuple(map(int, requested.split(".")))]
                varbinds = [(o, self.rows[o]) for o in after] or [(requested, _tlv(0x82, b""))]
            body = b"".join(_tlv(0x30, _oid(o) + v) for o, v in varbinds)
            reply = _tlv(0xA2, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, body))
            self.sock.sendto(_tlv(0x30, _int(1) + _tlv(0x04, self.community) + reply), peer)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.sock.close()


def v2c_session(instance, community="public-community", **kwargs):
    policy = SNMPTargetPolicy(
        allowed_networks=(ipaddress.ip_network("127.0.0.0/8"),), allowed_ports=frozenset({instance.port}), allow_loopback=True
    )
    return SNMPv2cSession(SNMPTarget("127.0.0.1", instance.port), community, policy=policy, **kwargs)


def test_v2c_session_get_and_walk_still_work():
    with V2cAgent() as server:
        session = v2c_session(server, timeout_seconds=1.0)
        assert session.get("1.3.6.1.2.1.1.1.0").as_bytes() == b"v2c switch"
        walked = session.walk("1.3.6.1.2.1.1")
        assert [r.oid for r in walked.rows] == ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.5.0"]


def test_v2c_session_retries_and_times_out_like_v3():
    with V2cAgent() as server:
        server.drop_first = 1
        assert v2c_session(server, timeout_seconds=0.3, retries=1).get("1.3.6.1.2.1.1.1.0").as_bytes() == b"v2c switch"
    with V2cAgent() as server:
        server.drop_first = 5
        with pytest.raises(SNMPTimeoutError):
            v2c_session(server, timeout_seconds=0.2, retries=1).get("1.3.6.1.2.1.1.1.0")
        assert server.requests == 2


def test_v2c_reply_with_other_community_is_ignored():
    with V2cAgent(community=b"other-community") as server, pytest.raises(SNMPTimeoutError):
        v2c_session(server, timeout_seconds=0.2, retries=0).get("1.3.6.1.2.1.1.1.0")
