"""Shared definitions for the SNMPv3 interoperability harness: the algorithm matrix, the fixed credentials
both sides use, and the MIB the reference agent serves. Credentials here are throwaway test values for a
loopback-only agent; they are not secrets and never leave the machine."""

from __future__ import annotations

# Authentication/privacy combinations the Edge Collector claims to support (see snmp_usm.validate_algorithms):
# AES-192/256 need an authentication hash at least as long as the AES key.
SUPPORTED_COMBINATIONS: tuple[tuple[str, str], ...] = (
    ("sha1", "aes128"),
    ("sha224", "aes128"), ("sha224", "aes192"),
    ("sha256", "aes128"), ("sha256", "aes192"), ("sha256", "aes256"),
    ("sha384", "aes128"), ("sha384", "aes192"), ("sha384", "aes256"),
    ("sha512", "aes128"), ("sha512", "aes192"), ("sha512", "aes256"),
)

# Combinations a standards-compatible agent can be configured with but the product refuses to speak. The harness
# asserts the refusal happens client-side, before any datagram is sent.
REFUSED_COMBINATIONS: tuple[tuple[str, str, str], ...] = (
    ("md5", "aes128", "md5 authentication is insecure"),
    ("sha256", "des", "des privacy is insecure"),
    ("sha1", "aes192", "sha1 yields only 20 key bytes, aes192 needs 24"),
    ("sha1", "aes256", "sha1 yields only 20 key bytes, aes256 needs 32"),
    ("sha224", "aes256", "sha224 yields only 28 key bytes, aes256 needs 32"),
    ("none", "none", "noAuthNoPriv is not supported"),
)

ENGINE_ID_HEX = "80004fb805636c6f7564"  # arbitrary but fixed, so the proxy test can derive the localized keys
MIB_BASE = "1.3.6.1.3.9999.1"  # an unassigned experimental arc: 25 walkable rows
MIB_ROWS = 25
SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_DESCR_PREFIX = "PySNMP engine version"  # the reference agent's own sysDescr
MARKER_OID = "1.3.6.1.3.9999.3.0"
MARKER_VALUE = "DCIM-INTEROP-PLAINTEXT-MARKER-7f3a91"  # must never appear in cleartext on the wire
MISSING_OID = "1.3.6.1.3.9999.2.1.0"


def combo_name(auth: str, priv: str) -> str:
    return f"{auth}-{priv}"


def username(auth: str, priv: str) -> str:
    return f"u-{combo_name(auth, priv)}"


def auth_secret(auth: str, priv: str) -> str:
    return f"auth-secret-{combo_name(auth, priv)}"


def priv_secret(auth: str, priv: str) -> str:
    return f"priv-secret-{combo_name(auth, priv)}"


# Users configured on the agent that deliberately disagree with what the client is told.
MISMATCH_AUTH_USER = ("u-mismatch-auth", "sha1", "aes128")  # the agent hashes with sha1; the client says sha256
MISMATCH_PRIV_USER = ("u-mismatch-priv", "sha256", "aes128")  # the agent encrypts with aes128; the client says aes256
MISMATCH_AUTH_SECRET = "mismatch-auth-secret"
MISMATCH_PRIV_SECRET = "mismatch-priv-secret"


# ---------------------------------------------------------------------------------------------------------------
# LLDP / CDP data served by the reference agent, and the discovery plan the collector is given for it.
#
# The specs below are a copy of backend/app/application/network/profile_templates.py (LLDP_SPEC, CDP_SPEC); a
# backend test (tests/unit/test_interop_profile_contract.py) fails if the two ever differ, so the harness cannot
# silently exercise a profile the product no longer ships.
LLDP_TABLE = "1.0.8802.1.1.2.1.4.1.1"
LLDP_LOCAL = "1.0.8802.1.1.2.1.3.7.1"
LLDP_ADDR = "1.0.8802.1.1.2.1.4.2.1"
CDP_TABLE = "1.3.6.1.4.1.9.9.23.1.2.1.1"
IFX = "1.3.6.1.2.1.31.1.1.1"

LLDP_SPEC = {
    "enabled": True,
    "table_oid": LLDP_TABLE,
    "columns": {
        "chassis_id_subtype": 4, "chassis_id": 5, "port_id_subtype": 6, "port_id": 7, "port_desc": 8,
        "sys_name": 9, "sys_desc": 10, "sys_cap_supported": 11, "sys_cap_enabled": 12,
    },
    "index_fields": ["time_mark", "local_port_num", "rem_index"],
    "local_port_table_oid": LLDP_LOCAL,
    "local_port_columns": {"port_id_subtype": 2, "port_id": 3, "port_desc": 4},
    "management_address_table_oid": LLDP_ADDR,
}
CDP_SPEC = {
    "enabled": True,
    "table_oid": CDP_TABLE,
    "columns": {
        "address_type": 3, "address": 4, "version": 5, "device_id": 6, "device_port": 7, "platform": 8,
        "capabilities": 9, "native_vlan": 11, "duplex": 12,
    },
    "index_fields": ["if_index", "device_index"],
    "local_port_table_oid": IFX,
    "local_port_columns": {"if_name": 1, "if_alias": 18},
}
DISCOVERY_PLAN = {
    "neighbor_discovery": {"lldp": LLDP_SPEC, "cdp": CDP_SPEC},
    "neighbor_behavior": {
        "lldp": {"enabled": True, "local_port_source": "lldp_loc_port_id", "management_address": True},
        "cdp": {"enabled": True, "device_id_normalization": "strip_domain"},
    },
}

# (oid, kind, value): "int" is an Integer32, "str" an OCTET STRING of text, "hex" an OCTET STRING of raw bytes.
NEIGHBOR_ROWS: tuple[tuple[str, str, object], ...] = (
    # one LLDP neighbor on local port 7 (time mark 1000, remote index 1)
    (f"{LLDP_TABLE}.4.1000.7.1", "int", 4), (f"{LLDP_TABLE}.5.1000.7.1", "hex", "0050563a1b2c"),
    (f"{LLDP_TABLE}.6.1000.7.1", "int", 5), (f"{LLDP_TABLE}.7.1000.7.1", "str", "Gi1/0/24"),
    (f"{LLDP_TABLE}.8.1000.7.1", "str", "uplink to core"), (f"{LLDP_TABLE}.9.1000.7.1", "str", "core-sw-1"),
    (f"{LLDP_TABLE}.10.1000.7.1", "str", "Vendor OS 1.2"), (f"{LLDP_TABLE}.12.1000.7.1", "hex", "28"),
    (f"{LLDP_LOCAL}.2.7", "int", 5), (f"{LLDP_LOCAL}.3.7", "str", "Eth1/7"),
    (f"{LLDP_ADDR}.3.1000.7.1.1.4.10.1.2.3", "int", 2),  # remote management address 10.1.2.3 is in the index
    # one CDP neighbor on ifIndex 3 (device index 1)
    (f"{CDP_TABLE}.3.3.1", "int", 1), (f"{CDP_TABLE}.4.3.1", "hex", "0a090807"), (f"{CDP_TABLE}.5.3.1", "str", "IOS 15.2"),
    (f"{CDP_TABLE}.6.3.1", "str", "dist-sw-2.example.net"), (f"{CDP_TABLE}.7.3.1", "str", "TenGigabitEthernet1/1"),
    (f"{CDP_TABLE}.8.3.1", "str", "cisco WS-C3850"), (f"{CDP_TABLE}.9.3.1", "hex", "00000028"),
    (f"{CDP_TABLE}.11.3.1", "int", 42), (f"{IFX}.1.3", "str", "Te1/0/3"),
)
