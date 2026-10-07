"""SNMPv3 interoperability harness: the real Edge Collector SNMP stack against an independent implementation.

Starts a pysnmp reference agent on loopback, puts a recording UDP relay between it and the collector, and runs
every scenario in `scenarios.py` terms: all supported authentication/privacy combinations (GET and a multi-page
GETBULK walk), refused combinations, wrong user / authentication secret / privacy secret, algorithm mismatches,
time-window resynchronisation, a missing OID, and what is (and is not) visible on the wire.

    pip install -r edge_collector/interop/requirements.txt
    PYTHONPATH=. python -m edge_collector.interop.run_interop [--json results.json]

Exit status 0 only if every scenario behaved as expected. Needs no privileges, no network beyond loopback and
no hardware. See edge_collector/interop/README.md for what this does and does not prove.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata

from edge_collector.neighbors import collect_neighbors
from edge_collector.snmp import (
    SNMPError,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPUnknownOIDError,
)
from edge_collector.snmp_usm import (
    SNMPv3ConfigError,
    SNMPv3Credentials,
    derive_keys,
    password_to_key,
)
from edge_collector.snmp_v3 import (
    FLAG_AUTH,
    FLAG_PRIV,
    SNMPv3AuthenticationError,
    SNMPv3Session,
    _parse_message,
)

from . import scenarios as sc

READY_TIMEOUT_S = 30.0
REQUEST_TIMEOUT_S = 3.0


# ------------------------------------------------------------------------------ recording relay
class Relay:
    """UDP relay between the collector and the agent that records every datagram in both directions."""

    def __init__(self, agent_port: int) -> None:
        self._agent = ("127.0.0.1", agent_port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.requests: list[bytes] = []
        self.responses: list[bytes] = []
        self._client: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._upstream.settimeout(0.1)
        self._threads = [threading.Thread(target=self._pump_in, daemon=True), threading.Thread(target=self._pump_out, daemon=True)]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._sock.close()
        self._upstream.close()

    def reset(self) -> None:
        with self._lock:
            self.requests, self.responses = [], []

    def snapshot(self) -> tuple[list[bytes], list[bytes]]:
        with self._lock:
            return list(self.requests), list(self.responses)

    def _pump_in(self) -> None:
        while not self._stop.is_set():
            try:
                data, client = self._sock.recvfrom(65_535)
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self._client = client
                self.requests.append(data)
            self._upstream.sendto(data, self._agent)

    def _pump_out(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._upstream.recvfrom(65_535)
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self.responses.append(data)
                client = self._client
            if client is not None:
                self._sock.sendto(data, client)


# ------------------------------------------------------------------------------ scenario plumbing
@dataclass
class Outcome:
    name: str
    group: str
    ok: bool
    detail: str


@dataclass
class Harness:
    relay: Relay
    outcomes: list[Outcome] = field(default_factory=list)

    def session(self, credentials: SNMPv3Credentials, *, clock: Callable[[], float] | None = None) -> SNMPv3Session:
        policy = SNMPTargetPolicy(allowed_ports=frozenset({self.relay.port}), allow_loopback=True)
        extra = {"clock": clock} if clock is not None else {}
        return SNMPv3Session(
            SNMPTarget("127.0.0.1", self.relay.port), credentials, policy=policy,
            timeout_seconds=REQUEST_TIMEOUT_S, retries=1, **extra,
        )

    def record(self, group: str, name: str, check: Callable[[], str]) -> None:
        self.relay.reset()
        try:
            detail = check()
            self.outcomes.append(Outcome(name, group, True, detail))
        except AssertionError as error:
            self.outcomes.append(Outcome(name, group, False, f"assertion failed: {error}"))
        except Exception as error:  # noqa: BLE001 - a harness must report, not crash, on any unexpected error
            self.outcomes.append(Outcome(name, group, False, f"{type(error).__name__}: {error}"))


def credentials_for(auth: str, priv: str, **override: str) -> SNMPv3Credentials:
    values = {
        "username": sc.username(auth, priv), "auth_protocol": auth, "auth_secret": sc.auth_secret(auth, priv),
        "priv_protocol": priv, "priv_secret": sc.priv_secret(auth, priv),
    }
    values.update(override)
    return SNMPv3Credentials(**values)


def expect_auth_failure(call: Callable[[], object], reason: str) -> str:
    try:
        call()
    except SNMPv3AuthenticationError as error:
        assert error.reason == reason, f"expected {reason}, agent reported {error.reason}"
        assert "secret" not in str(error).lower() and str(error) == "SNMPv3 authentication failed", f"unexpected message {error}"
        return f"rejected as {reason}"
    raise AssertionError("the request succeeded but must have been rejected")


# ------------------------------------------------------------------------------ scenarios
def supported_combination(h: Harness, auth: str, priv: str) -> str:
    session = h.session(credentials_for(auth, priv))
    descr = session.get(sc.SYS_DESCR_OID).as_bytes().decode()
    assert descr.startswith(sc.SYS_DESCR_PREFIX), f"unexpected sysDescr {descr!r}"
    walked = session.walk(sc.MIB_BASE)
    assert not walked.truncated and len(walked.rows) == sc.MIB_ROWS, f"walk returned {len(walked.rows)} rows"
    values = [row.as_bytes().decode() for row in walked.rows]
    assert values == [f"row-{n:02d}" for n in range(1, sc.MIB_ROWS + 1)], "walk rows differ from the agent's table"
    requests, _ = h.relay.snapshot()
    assert len(requests) > 4, "a 25-row walk needs several GETBULK pages"  # discovery + GET + >= 3 GETBULK
    return f"GET ok, walk {len(walked.rows)} rows in {len(requests)} datagrams"


def refused_combination(h: Harness, auth: str, priv: str, why: str) -> str:
    try:
        credentials_for(auth, priv)
    except SNMPv3ConfigError as error:
        message = str(error)
        assert "secret" not in message.lower().replace("authentication secret", "").replace("privacy secret", ""), message
        requests, _ = h.relay.snapshot()
        assert not requests, "a refused configuration must not reach the network"
        return f"refused before any packet ({why})"
    raise AssertionError(f"{auth}/{priv} was accepted but the product must refuse it ({why})")


def wrong_username(h: Harness) -> str:
    auth, priv = "sha256", "aes128"
    session = h.session(credentials_for(auth, priv, username="u-nobody"))
    return expect_auth_failure(lambda: session.get(sc.SYS_DESCR_OID), "unknown_user")


def wrong_auth_secret(h: Harness) -> str:
    auth, priv = "sha256", "aes128"
    session = h.session(credentials_for(auth, priv, auth_secret="not-the-auth-secret"))
    return expect_auth_failure(lambda: session.get(sc.SYS_DESCR_OID), "wrong_digest")


def wrong_priv_secret(h: Harness) -> str:
    auth, priv = "sha256", "aes128"
    session = h.session(credentials_for(auth, priv, priv_secret="not-the-priv-secret"))
    return expect_auth_failure(lambda: session.get(sc.SYS_DESCR_OID), "decryption_error")


def mismatched_auth_algorithm(h: Harness) -> str:
    name, _agent_auth, _agent_priv = sc.MISMATCH_AUTH_USER
    creds = SNMPv3Credentials(name, "sha256", sc.MISMATCH_AUTH_SECRET, "aes128", sc.MISMATCH_PRIV_SECRET)
    return expect_auth_failure(lambda: h.session(creds).get(sc.SYS_DESCR_OID), "wrong_digest")


def mismatched_priv_algorithm(h: Harness) -> str:
    name, _agent_auth, _agent_priv = sc.MISMATCH_PRIV_USER
    creds = SNMPv3Credentials(name, "sha256", sc.MISMATCH_AUTH_SECRET, "aes256", sc.MISMATCH_PRIV_SECRET)
    return expect_auth_failure(lambda: h.session(creds).get(sc.SYS_DESCR_OID), "decryption_error")


def missing_oid(h: Harness) -> str:
    session = h.session(credentials_for("sha256", "aes128"))
    try:
        session.get(sc.MISSING_OID)
    except SNMPUnknownOIDError:
        return "noSuchObject surfaced as SNMPUnknownOIDError"
    raise AssertionError("a missing OID must raise SNMPUnknownOIDError")


class SkewedClock:
    """Monotonic clock the harness can push forward, to make the agent's time window expire."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def time_window_resync(h: Harness) -> str:
    clock = SkewedClock()
    session = h.session(credentials_for("sha256", "aes128"), clock=clock)
    session.get(sc.SYS_DESCR_OID)  # discovery + first authenticated request
    h.relay.reset()
    clock.offset = 3600.0  # the collector now believes the agent's clock is an hour ahead: outside the 150 s window
    value = session.get(sc.SYS_DESCR_OID).as_bytes().decode()
    assert value.startswith(sc.SYS_DESCR_PREFIX)
    requests, responses = h.relay.snapshot()
    assert len(requests) >= 2 and len(responses) >= 2, (
        f"expected request, notInTimeWindow report and retry; saw {len(requests)} requests / {len(responses)} responses"
    )
    return f"agent reported notInTimeWindow, collector resynchronised and succeeded ({len(requests)} requests)"


def wire_confidentiality(h: Harness) -> str:
    auth, priv = "sha512", "aes256"
    creds = credentials_for(auth, priv)
    session = h.session(creds)
    session.get(sc.MARKER_OID)
    session.walk(sc.MIB_BASE)
    requests, responses = h.relay.snapshot()
    assert requests and responses
    engine_id = bytes.fromhex(sc.ENGINE_ID_HEX)
    keys = derive_keys(creds, engine_id)
    hash_name = "sha512"
    forbidden = {
        "authentication secret": creds.auth_secret.encode(), "privacy secret": creds.priv_secret.encode(),
        "master authentication key": password_to_key(creds.auth_secret, hash_name),
        "master privacy key": password_to_key(creds.priv_secret, hash_name),
        "localized authentication key": keys.auth_key, "localized privacy key": keys.priv_key,
        "plaintext marker value": sc.MARKER_VALUE.encode(), "plaintext table value": b"row-01",
    }
    # Positive control: the user name is a cleartext USM field by design, so a scan that finds nothing would be broken.
    assert any(creds.username.encode() in datagram for datagram in requests), "the wire scan cannot see the USM user name"
    for datagram in requests + responses:
        for label, needle in forbidden.items():
            assert needle not in datagram, f"{label} appears in cleartext on the wire"
    # after engine discovery every request is authenticated and encrypted
    secured = [m for m in (_parse_message(d) for d in requests) if m.engine_id]
    assert secured, "no secured requests captured"
    assert all(m.flags & FLAG_AUTH and m.flags & FLAG_PRIV for m in secured), "a request was sent without authPriv"
    return f"{len(requests) + len(responses)} datagrams inspected: no secret, key or plaintext value; all {len(secured)} secured requests authPriv"


def neighbor_discovery_over_snmpv3(h: Harness, auth: str, priv: str) -> str:
    """The product's LLDP and CDP profile specs, walked with the real SNMPv3 stack, decoded by the real adapters."""
    scans = collect_neighbors(h.session(credentials_for(auth, priv)), sc.DISCOVERY_PLAN)
    by_protocol = {scan.protocol: scan for scan in scans}
    assert set(by_protocol) == {"lldp", "cdp"}, f"scanned {sorted(by_protocol)}"
    for scan in scans:
        assert scan.failure is None and scan.complete and scan.malformed_rows == 0, f"{scan.protocol}: {scan.failure or 'incomplete'}"
        assert len(scan.observations) == 1, f"{scan.protocol}: {len(scan.observations)} neighbors"
    lldp = by_protocol["lldp"].observations[0]
    assert lldp["local_port"] == {"name": "Eth1/7", "ref": "7"}
    assert lldp["remote"]["chassis_id"] == "00:50:56:3a:1b:2c" and lldp["remote"]["port_id"] == "Gi1/0/24"
    assert lldp["remote"]["system_name"] == "core-sw-1" and lldp["remote"]["management_address"] == "10.1.2.3"
    assert lldp["capabilities"] == ["bridge", "router"]
    cdp = by_protocol["cdp"].observations[0]
    assert cdp["local_port"] == {"name": "Te1/0/3", "ref": "3"}
    assert cdp["remote"]["chassis_id"] == "dist-sw-2" and cdp["remote"]["port_id"] == "TenGigabitEthernet1/1"
    assert cdp["remote"]["platform"] == "cisco WS-C3850" and cdp["remote"]["management_address"] == "10.9.8.7"
    assert cdp["native_vlan"] == 42
    return "LLDP and CDP neighbors decoded identically to the unit-test fixtures"


def repr_and_errors_hide_secrets(h: Harness) -> str:
    creds = credentials_for("sha256", "aes128", auth_secret="distinct-auth-secret-value")
    session = h.session(creds)
    texts = [repr(creds), str(creds), repr(session)]
    try:
        session.get(sc.SYS_DESCR_OID)
    except SNMPError as error:
        texts.append(str(error))
        texts.append(repr(error))
    for text in texts:
        assert "distinct-auth-secret-value" not in text and sc.priv_secret("sha256", "aes128") not in text, "a secret leaked into text"
    return "credentials, session and error text contain no secret"


# ------------------------------------------------------------------------------ orchestration
def start_agent(port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "edge_collector.interop.agent", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + READY_TIMEOUT_S
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line.strip() == "ready":
            return process
        if process.poll() is not None:
            raise RuntimeError(f"the reference agent exited early: {line}{process.stdout.read()}")
    process.terminate()
    raise RuntimeError("the reference agent did not become ready in time")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run(json_path: str | None) -> int:
    agent_port = free_port()
    agent = start_agent(agent_port)
    relay = Relay(agent_port)
    relay.start()
    harness = Harness(relay)
    try:
        for auth, priv in sc.SUPPORTED_COMBINATIONS:
            harness.record("supported", f"{auth} + {priv}", lambda a=auth, p=priv: supported_combination(harness, a, p))
        for auth, priv, why in sc.REFUSED_COMBINATIONS:
            harness.record("refused", f"{auth} + {priv}", lambda a=auth, p=priv, w=why: refused_combination(harness, a, p, w))
        harness.record("failure", "unknown username", lambda: wrong_username(harness))
        harness.record("failure", "wrong authentication secret", lambda: wrong_auth_secret(harness))
        harness.record("failure", "wrong privacy secret", lambda: wrong_priv_secret(harness))
        harness.record("failure", "agent uses a different authentication algorithm", lambda: mismatched_auth_algorithm(harness))
        harness.record("failure", "agent uses a different privacy algorithm", lambda: mismatched_priv_algorithm(harness))
        harness.record("protocol", "missing OID is reported, not guessed", lambda: missing_oid(harness))
        harness.record("protocol", "time-window resynchronisation", lambda: time_window_resync(harness))
        for auth, priv in (("sha256", "aes128"), ("sha512", "aes256")):
            harness.record("discovery", f"LLDP+CDP walk ({auth} + {priv})", lambda a=auth, p=priv: neighbor_discovery_over_snmpv3(harness, a, p))
        harness.record("wire", "no secret or plaintext value on the wire", lambda: wire_confidentiality(harness))
        harness.record("wire", "reprs and errors hide secrets", lambda: repr_and_errors_hide_secrets(harness))
    finally:
        relay.stop()
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()

    width = max(len(o.name) for o in harness.outcomes)
    for outcome in harness.outcomes:
        print(f"{'PASS' if outcome.ok else 'FAIL'}  {outcome.group:<9} {outcome.name:<{width}}  {outcome.detail}")
    failed = [o for o in harness.outcomes if not o.ok]
    print(f"\n{len(harness.outcomes) - len(failed)} of {len(harness.outcomes)} scenarios behaved as expected")
    if json_path:
        report = {
            "reference_agent": f"pysnmp {metadata.version('pysnmp')}",
            "python": platform.python_version(),
            "scenarios": [o.__dict__ for o in harness.outcomes],
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    args = parser.parse_args()
    raise SystemExit(run(args.json))


if __name__ == "__main__":
    main()
