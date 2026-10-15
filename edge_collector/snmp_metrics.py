"""Telemetry metric polling over SNMPv3 USM authPriv, with SNMPv2c as an explicit legacy opt-in.

This is the metric-polling counterpart of `discovery_sessions.py`. It adds no protocol code:
sessions come from `LocalCredentialStore.session_for()` (unchanged), the wire work is done by
`SNMPv3Session.get()` / `SNMPv2cSession.get()`, and a returned `Varbind` is mapped to an
`SNMPMetric` with the same `SUPPORTED_OIDS` table and `_numeric_value()` rules that
`SNMPv2cCollector.get()` in `snmp.py` uses (that raw-socket legacy collector is unchanged).

Protocol selection is fail-closed, per integration, from the local credential file:

1. An ``"snmpv3"`` entry always wins. It is used even when the entry also carries a
   ``"community"`` and ``"allow_legacy_v2c": true``; an invalid ``"snmpv3"`` entry is an error
   and never falls back to the community. There is no silent downgrade.
2. Only an entry with *no* ``"snmpv3"`` key may use SNMPv2c, and only when it also sets
   ``"allow_legacy_v2c": true`` (a JSON boolean; anything else counts as false). Otherwise
   `SNMPLegacyV2cNotEnabledError` is raised before any datagram is built or sent, so the
   community string never reaches the network.

The opt-in governs metric polling only; discovery keeps matching Central's assigned version
as before. Nothing here logs or puts a secret in an exception message.
"""

from __future__ import annotations

from .credentials import LocalCredentialStore
from .snmp import (
    SUPPORTED_OIDS,
    SNMPError,
    SNMPMalformedValueError,
    SNMPMetric,
    SNMPTarget,
    SNMPTargetPolicy,
    SNMPTargetPolicyError,
    SNMPUnknownOIDError,
    _numeric_value,
)
from .snmp_v2c import SNMPv2cSession
from .snmp_v3 import SNMPv3Session
from .snmp_wire import Varbind

LEGACY_V2C_OPT_IN_FIELD = "allow_legacy_v2c"


class SNMPLegacyV2cNotEnabledError(SNMPTargetPolicyError):
    """Only cleartext SNMPv2c credentials exist and legacy v2c polling was not enabled."""


def metric_session_for(
    store: LocalCredentialStore,
    integration_id: str,
    target: SNMPTarget,
    *,
    policy: SNMPTargetPolicy,
    timeout_seconds: float = 5.0,
    retries: int = 1,
) -> SNMPv3Session | SNMPv2cSession:
    """Return the session metric polling may use for this integration (see module docstring)."""
    session = store.session_for(integration_id, target, policy=policy, timeout_seconds=timeout_seconds, retries=retries)
    if isinstance(session, SNMPv3Session):
        return session
    if isinstance(session, SNMPv2cSession) and store.legacy_v2c_allowed(integration_id):
        return session
    raise SNMPLegacyV2cNotEnabledError("legacy SNMPv2c metric collection is not enabled for this target")


def collect_metric(session: SNMPv3Session | SNMPv2cSession, oid: str) -> SNMPMetric:
    """GET one mapped scalar OID and return it as a canonical metric.

    An OID that is not in `SUPPORTED_OIDS` is refused before anything is sent, exactly like
    `SNMPv2cCollector.get()`.
    """
    if oid not in SUPPORTED_OIDS:
        raise SNMPUnknownOIDError("OID is not configured for edge collection")
    return metric_from_varbind(oid, session.get(oid))


def metric_from_varbind(oid: str, varbind: Varbind) -> SNMPMetric:
    """Map a GET result to an `SNMPMetric` with the same rules as `SNMPv2cCollector.get()`."""
    mapping = SUPPORTED_OIDS.get(oid)
    if mapping is None:
        raise SNMPUnknownOIDError("OID is not configured for edge collection")
    if varbind.oid != oid:
        raise SNMPError("SNMP response OID did not match request")
    if varbind.is_exception:
        raise SNMPUnknownOIDError("OID is not available on the agent")
    try:
        numeric_value = _numeric_value((varbind.tag, varbind.value))
    except (TypeError, ValueError) as error:
        raise SNMPMalformedValueError("SNMP response value is not numeric") from error
    return SNMPMetric(oid, mapping.canonical_metric, numeric_value * mapping.scale, mapping.unit)
