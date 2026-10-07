"""Bounded discovery sessions using collector-local secrets and explicit target policy."""
from __future__ import annotations

import ipaddress
import time
from collections.abc import Mapping
from typing import Any

from .credentials import CredentialStoreError, LocalCredentialStore
from .snmp import SNMPTarget, SNMPTargetPolicy, SNMPTimeoutError
from .snmp_v2c import SNMPv2cSession
from .snmp_v3 import SNMPv3Session
from .snmp_wire import Varbind, WalkResult, walk


class BoundedWalker:
    """Each table gets 20 seconds plus at most one bounded in-flight request.

    A fresh deadline per table isolates LLDP timeouts from CDP. Production sessions use
    two-second UDP attempts and zero retransmissions; v3 resynchronization is bounded.
    """

    def __init__(self, session: SNMPv2cSession | SNMPv3Session, *, clock=time.monotonic) -> None:
        self._session = session
        self._clock = clock

    def walk(self, base_oid: str, *, max_rows: int = 4096) -> WalkResult:
        deadline = self._clock() + 20.0

        def bulk(oid: str, repetitions: int) -> list[Varbind]:
            if self._clock() >= deadline:
                raise SNMPTimeoutError("discovery table deadline exceeded")
            return self._session.get_bulk(oid, repetitions)

        return walk(bulk, base_oid, max_rows=max_rows)


def local_session_factory(store: LocalCredentialStore, policy: SNMPTargetPolicy):
    def create(item: Mapping[str, Any], target: SNMPTarget) -> BoundedWalker:
        # Numeric addresses avoid an unbounded OS DNS resolver inside the worker.
        try:
            ipaddress.ip_address(target.host)
        except ValueError:
            raise CredentialStoreError("discovery requires a numeric target address") from None
        version = item.get("snmp_version")
        if version not in ("v2c", "v3"):
            raise CredentialStoreError("unsupported discovery SNMP version")
        session = store.session_for(
            str(item["integration_id"]), target, policy=policy, timeout_seconds=2.0, retries=0,
        )
        if (version == "v3") != isinstance(session, SNMPv3Session):
            raise CredentialStoreError("local credentials do not match the assigned SNMP version")
        return BoundedWalker(session)

    return create
