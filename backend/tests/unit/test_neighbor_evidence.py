import uuid

import pytest

from app.application.network.neighbor_evidence import (
    InvalidNeighborPayload,
    normalize_address,
    normalize_mac,
    normalize_name,
    parse_neighbor,
)

INTEGRATION = uuid.UUID("11111111-1111-1111-1111-111111111111")


def doc(**overrides) -> dict:
    neighbor = {
        "protocol": "lldp", "local_port": {"name": "Eth1/1", "ref": "7"},
        "remote": {"chassis_id": "00:50:56:3A:1B:2C", "port_id": "Gi1/0/24", "system_name": "core", "management_address": "10.0.0.1"},
        "capabilities": ["bridge", "router"], "native_vlan": 10, "ttl_seconds": 120, "raw": {"sys_name": "core"},
    }
    neighbor.update(overrides)
    return {"scan_id": "s1", "neighbor": neighbor}


def test_mac_and_name_normalization():
    for raw in ("00:50:56:3A:1B:2C", "00-50-56-3a-1b-2c", "0050.563a.1b2c", "00505 63a1b2c".replace(" ", "")):
        assert normalize_mac(raw) == "00:50:56:3a:1b:2c"
    assert normalize_mac("not-a-mac") is None and normalize_mac("00:50:56") is None
    assert normalize_name("  Core-SW  1.  ") == "core-sw 1"
    assert normalize_address(" 10.0.0.1 ") == "10.0.0.1" and normalize_address("2001:DB8::1") == "2001:db8::1"
    assert normalize_address("999.1.1.1") is None and normalize_address(5) is None


def test_identity_key_is_stable_across_notation_and_scoped_to_integration_and_protocol():
    a = parse_neighbor(doc())
    b = parse_neighbor(doc(remote={"chassis_id": "0050.563a.1b2c", "port_id": " GI1/0/24 "}, local_port={"name": " eth1/1 ", "ref": "9"}))
    assert a.identity_key(INTEGRATION) == b.identity_key(INTEGRATION)
    assert a.identity_key(INTEGRATION) != a.identity_key(uuid.uuid4())
    assert a.identity_key(INTEGRATION) != parse_neighbor(doc(protocol="cdp")).identity_key(INTEGRATION)
    other_port = parse_neighbor(doc(remote={"chassis_id": "00:50:56:3a:1b:2c", "port_id": "Gi1/0/25"}))
    assert a.identity_key(INTEGRATION) != other_port.identity_key(INTEGRATION)


def test_local_port_may_be_identified_by_reference_only():
    evidence = parse_neighbor(doc(local_port={"name": None, "ref": "7"}))
    assert evidence.local_port_name is None and evidence.identity_key(INTEGRATION)
    assert evidence.identity_key(INTEGRATION) != parse_neighbor(doc(local_port={"name": None, "ref": "8"})).identity_key(INTEGRATION)


@pytest.mark.parametrize(
    "mutation",
    [
        {"protocol": "bogus"},
        {"local_port": {"name": None, "ref": None}},
        {"local_port": "eth0"},
        {"remote": {"chassis_id": "x"}},
        {"remote": {"port_id": "x"}},
        {"remote": {"chassis_id": "   ", "port_id": "x"}},
        {"remote": {"chassis_id": 5, "port_id": "x"}},
        {"remote": "core"},
        {"capabilities": "bridge"},
        {"raw": []},
    ],
)
def test_unidentifiable_or_malformed_documents_are_refused_with_fixed_text(mutation):
    with pytest.raises(InvalidNeighborPayload) as error:
        parse_neighbor(doc(**mutation))
    assert "core" not in str(error.value)
    with pytest.raises(InvalidNeighborPayload):
        parse_neighbor({"scan_id": "s"})


def test_fields_are_sanitized_and_bounded():
    evidence = parse_neighbor(doc(
        remote={"chassis_id": "bad\x00\x1b[0m id", "port_id": "p" * 999, "system_name": "n‮" * 400,
                "management_address": "not-an-ip", "system_description": "d" * 5000},
        capabilities=["bridge", "BAD CAP", 5, "x" * 40, *["router"] * 100], native_vlan=99999, ttl_seconds=-1,
        raw={"ok": "v" * 1000, "Bad-Key": "x", "n": 5, "flag": True, "nested": {"a": 1}},
    ))
    assert "\x00" not in evidence.remote_chassis_ident and "\x1b" not in evidence.remote_chassis_ident
    assert len(evidence.remote_port_ident) == 255 and len(evidence.remote_system_name) <= 255
    assert len(evidence.remote_system_description) == 512
    assert evidence.remote_management_address is None
    assert evidence.capabilities == ["bridge", "router"]
    assert evidence.native_vlan is None and evidence.ttl_seconds is None
    assert evidence.raw == {"ok": "v" * 256, "n": 5}
