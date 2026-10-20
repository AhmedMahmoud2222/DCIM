"""Central SNMP driver v3 handling and the contract with the edge collector's USM policy."""

import itertools
import uuid

import pytest

from app.application.drivers.base import DriverConnectionError
from app.application.drivers.snmp import SimulatedSNMPTransport, SNMPDriver
from app.application.drivers.snmpv3 import (
    AUTH_PROTOCOL_HASH_BYTES,
    INSECURE_PROTOCOLS,
    MAX_SECRET_BYTES,
    MIN_SECRET_BYTES,
    PRIV_PROTOCOL_KEY_BYTES,
    SnmpV3Credential,
    SnmpV3CredentialError,
)

AUTH_SECRET = "driver-auth-secret-1"
PRIV_SECRET = "driver-priv-secret-2"


def credential(**changes) -> SnmpV3Credential:
    values = {
        "username": "poller", "auth_protocol": "sha256", "auth_secret": AUTH_SECRET,
        "priv_protocol": "aes128", "priv_secret": PRIV_SECRET,
    }
    return SnmpV3Credential(**(values | changes))


def test_repr_str_and_describe_never_expose_secrets():
    c = credential()
    for blob in (repr(c), str(c), str(c.describe()), repr([c]), f"{c}"):
        assert AUTH_SECRET not in blob and PRIV_SECRET not in blob
    assert c.describe() == {
        "security_level": "authPriv", "username": "poller", "auth_protocol": "sha256", "priv_protocol": "aes128",
        "context_name": "",
    }


def test_plaintext_roundtrip_and_tamper_detection():
    c = credential(context_name="ctx")
    assert SnmpV3Credential.from_plaintext(c.to_plaintext()) == c
    for bad in (None, "", "not json", "[]", '{"kind":"other","v":1}', '{"kind":"snmpv3","v":2}', '{"kind":"snmpv3","v":1}'):
        with pytest.raises(SnmpV3CredentialError):
            SnmpV3Credential.from_plaintext(bad)
    weakened = c.to_plaintext().replace("sha256", "md5")
    with pytest.raises(SnmpV3CredentialError, match="insecure"):
        SnmpV3Credential.from_plaintext(weakened)


async def test_driver_passes_validated_v3_credential_to_transport():
    seen = {}

    def factory(host, port, version, cred):
        seen.update(host=host, port=port, version=version, cred=cred)
        return SimulatedSNMPTransport({"1.3.6.1.2.1.1.1.0": "x"})

    from app.application.drivers.snmp import MetricMapping

    driver = SNMPDriver(uuid.uuid4(), metric_mappings=[MetricMapping("1.3.6.1.2.1.1.1.0", "descr")], transport_factory=factory)
    await driver.connect(
        target_host="192.0.2.1", target_port=161,
        config={"version": "v3", "snmpv3": credential().describe()}, credential=credential().to_plaintext(),
    )
    assert seen["version"] == "v3" and isinstance(seen["cred"], SnmpV3Credential)
    assert seen["cred"].auth_secret == AUTH_SECRET
    result = await driver.poll()
    assert result.metrics == {"descr": "x"}
    assert AUTH_SECRET not in repr(result) and PRIV_SECRET not in repr(result.raw_attributes)


@pytest.mark.parametrize("bad_credential", [None, "", "public", "{}", '{"kind":"snmpv3"}'])
async def test_driver_refuses_v3_without_a_valid_structured_credential(bad_credential):
    driver = SNMPDriver(uuid.uuid4(), transport_factory=lambda *a: SimulatedSNMPTransport())
    with pytest.raises(DriverConnectionError) as error:
        await driver.connect(target_host="h", target_port=161, config={"version": "v3"}, credential=bad_credential)
    assert error.value.reason == "SNMPv3 credential is missing or invalid."


async def test_driver_refuses_downgraded_security_level():
    driver = SNMPDriver(uuid.uuid4(), transport_factory=lambda *a: SimulatedSNMPTransport())
    with pytest.raises(DriverConnectionError) as error:
        await driver.connect(
            target_host="h", target_port=161, config={"version": "v3", "snmpv3": {"security_level": "authNoPriv"}},
            credential=credential().to_plaintext(),
        )
    assert "authPriv" in error.value.reason


async def test_v2c_credential_is_passed_through_unchanged():
    seen = {}

    def factory(host, port, version, cred):
        seen.update(version=version, cred=cred)
        return SimulatedSNMPTransport()

    driver = SNMPDriver(uuid.uuid4(), transport_factory=factory)
    await driver.connect(target_host="h", target_port=161, config={"version": "v2c"}, credential="community")
    assert seen == {"version": "v2c", "cred": "community"}


def test_backend_and_edge_algorithm_policy_agree():
    """The two implementations are separate deployables; this keeps their policy identical."""
    from edge_collector import snmp_usm as edge

    assert {name: values[2] for name, values in edge.AUTH_PROTOCOLS.items()} == AUTH_PROTOCOL_HASH_BYTES
    assert edge.PRIV_PROTOCOLS == PRIV_PROTOCOL_KEY_BYTES
    assert edge.INSECURE_PROTOCOLS == INSECURE_PROTOCOLS
    assert (edge.MIN_SECRET_LENGTH, edge.MAX_SECRET_LENGTH) == (MIN_SECRET_BYTES, MAX_SECRET_BYTES)
    candidates = [*AUTH_PROTOCOL_HASH_BYTES, *PRIV_PROTOCOL_KEY_BYTES, "md5", "des", "none", "sha3", "aes512"]
    for auth, priv in itertools.product(candidates, candidates):
        assert backend_accepts(auth, priv) == edge_accepts(auth, priv), (auth, priv)
    for secret in ("", "x" * 7, "x" * 8, "x" * 256, "x" * 257, "bad\x01secret"):
        assert backend_accepts("sha256", "aes128", auth_secret=secret) == edge_accepts("sha256", "aes128", auth_secret=secret)


def backend_accepts(auth: str, priv: str, auth_secret: str = AUTH_SECRET) -> bool:
    try:
        credential(auth_protocol=auth, priv_protocol=priv, auth_secret=auth_secret)
    except SnmpV3CredentialError:
        return False
    return True


def edge_accepts(auth: str, priv: str, auth_secret: str = AUTH_SECRET) -> bool:
    from edge_collector import snmp_usm as edge

    try:
        edge.SNMPv3Credentials("poller", auth, auth_secret, priv, PRIV_SECRET)
    except edge.SNMPv3ConfigError:
        return False
    return True
