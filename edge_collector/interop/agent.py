"""Reference SNMPv3 agent for interoperability testing, built on pysnmp (an independent implementation).

Serves a small fixed MIB (sysDescr plus a 25-row walkable subtree) to one authPriv user per supported
algorithm combination, plus two deliberately mismatched users. Loopback only. Run by `run_interop.py`; it can
also be started by hand:

    python -m edge_collector.interop.agent --port 16100
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import scenarios as sc

try:
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.entity import config, engine
    from pysnmp.entity.rfc3413 import cmdrsp, context
except ImportError:  # pragma: no cover - the harness reports this clearly
    sys.stderr.write("pysnmp is required: pip install -r edge_collector/interop/requirements.txt\n")
    raise

AUTH = {
    "sha1": config.USM_AUTH_HMAC96_SHA, "sha224": config.USM_AUTH_HMAC128_SHA224, "sha256": config.USM_AUTH_HMAC192_SHA256,
    "sha384": config.USM_AUTH_HMAC256_SHA384, "sha512": config.USM_AUTH_HMAC384_SHA512,
}
PRIV = {"aes128": config.USM_PRIV_CFB128_AES, "aes192": config.USM_PRIV_CFB192_AES, "aes256": config.USM_PRIV_CFB256_AES}


def _add_user(snmp_engine, name: str, auth: str, auth_key: str, priv: str, priv_key: str) -> None:
    config.add_v3_user(snmp_engine, name, AUTH[auth], auth_key, PRIV[priv], priv_key)
    config.add_vacm_user(snmp_engine, 3, name, "authPriv", (1,), (1,))  # the whole iso tree: LLDP-MIB lives under 1.0


def _export_mib(snmp_context) -> None:
    builder = snmp_context.get_mib_instrum().get_mib_builder()
    mib_scalar, mib_instance = builder.import_symbols("SNMPv2-SMI", "MibScalar", "MibScalarInstance")
    from pysnmp.proto.rfc1902 import OctetString

    base = tuple(int(p) for p in sc.MIB_BASE.split("."))
    symbols = []
    for row in range(1, sc.MIB_ROWS + 1):
        symbols.append(mib_scalar(base + (row,), OctetString()).set_max_access("read-only"))
        symbols.append(mib_instance(base + (row,), (0,), OctetString(f"row-{row:02d}")))
    marker = tuple(int(p) for p in sc.MARKER_OID.split("."))
    symbols.append(mib_scalar(marker[:-1], OctetString()).set_max_access("read-only"))
    symbols.append(mib_instance(marker[:-1], (0,), OctetString(sc.MARKER_VALUE)))
    builder.export_symbols("__DCIM-INTEROP-MIB", *symbols)
    _export_neighbor_rows(builder, mib_scalar, mib_instance)


def _export_neighbor_rows(builder, mib_scalar, mib_instance) -> None:
    """LLDP/CDP/IF-MIB rows as scalar instances grouped by their parent arc, so a walk returns them in OID order."""
    from pysnmp.proto.rfc1902 import Integer32, OctetString

    grouped: dict[tuple[int, ...], list[tuple[int, object]]] = {}
    for oid, kind, value in sc.NEIGHBOR_ROWS:
        arcs = tuple(int(p) for p in oid.split("."))
        if kind == "int":
            syntax = Integer32(value)
        elif kind == "hex":
            syntax = OctetString(hexValue=str(value))
        else:
            syntax = OctetString(str(value))
        grouped.setdefault(arcs[:-1], []).append((arcs[-1], syntax))
    symbols = []
    for prefix, leaves in grouped.items():
        symbols.append(mib_scalar(prefix, OctetString()).set_max_access("read-only"))
        symbols.extend(mib_instance(prefix, (leaf,), syntax) for leaf, syntax in leaves)
    builder.export_symbols("__DCIM-INTEROP-NEIGHBOR-MIB", *symbols)


async def serve(port: int) -> None:
    snmp_engine = engine.SnmpEngine(snmpEngineID=bytes.fromhex(sc.ENGINE_ID_HEX))
    config.add_transport(snmp_engine, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port)))
    for auth, priv in sc.SUPPORTED_COMBINATIONS:
        _add_user(snmp_engine, sc.username(auth, priv), auth, sc.auth_secret(auth, priv), priv, sc.priv_secret(auth, priv))
    name, auth, priv = sc.MISMATCH_AUTH_USER
    _add_user(snmp_engine, name, auth, sc.MISMATCH_AUTH_SECRET, priv, sc.MISMATCH_PRIV_SECRET)
    name, auth, priv = sc.MISMATCH_PRIV_USER
    _add_user(snmp_engine, name, auth, sc.MISMATCH_AUTH_SECRET, priv, sc.MISMATCH_PRIV_SECRET)
    snmp_context = context.SnmpContext(snmp_engine)
    _export_mib(snmp_context)
    cmdrsp.GetCommandResponder(snmp_engine, snmp_context)
    cmdrsp.NextCommandResponder(snmp_engine, snmp_context)
    cmdrsp.BulkCommandResponder(snmp_engine, snmp_context)
    print("ready", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
