"""Edge-local SNMP credential store.

SNMP secrets are *not* delivered by Central (`/discovery-plan` is secret-free). They live in
a local JSON file on the collector host, keyed by integration ID:

    {"<integration-uuid>": {"community": "..."},
     "<integration-uuid>": {"snmpv3": {"username": "...", "auth_protocol": "sha256",
                                       "auth_secret": "...", "priv_protocol": "aes128",
                                       "priv_secret": "...", "context_name": ""}}}

The file must not be readable by group or others (POSIX), and nothing here logs, prints or
puts a secret in an exception message.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .snmp import SNMPTarget, SNMPTargetPolicy
from .snmp_usm import SNMPv3ConfigError, SNMPv3Credentials
from .snmp_v2c import SNMPv2cSession
from .snmp_v3 import SNMPv3Session


class CredentialStoreError(RuntimeError):
    """The store is unusable or lacks credentials for an integration (never includes secrets)."""


class LocalCredentialStore:
    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        self._entries = entries

    def __repr__(self) -> str:
        return f"LocalCredentialStore({len(self._entries)} integrations)"

    @classmethod
    def load(cls, path: Path | str) -> LocalCredentialStore:
        file = Path(path)
        try:
            mode = file.stat().st_mode
        except OSError as error:
            raise CredentialStoreError("credential file cannot be read") from error
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialStoreError("credential file must not be accessible by group or others (chmod 600)")
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CredentialStoreError("credential file is not valid JSON") from error
        if not isinstance(document, dict) or not all(isinstance(v, dict) for v in document.values()):
            raise CredentialStoreError("credential file has an unexpected shape")
        return cls(document)

    def session_for(
        self, integration_id: str, target: SNMPTarget, *, policy: SNMPTargetPolicy, timeout_seconds: float = 5.0,
        retries: int = 1,
    ) -> SNMPv2cSession | SNMPv3Session:
        entry = self._entries.get(integration_id)
        if entry is None:
            raise CredentialStoreError("no local credentials for this integration")
        if "snmpv3" in entry:
            document = entry["snmpv3"]
            try:
                credentials = SNMPv3Credentials(
                    username=document["username"], auth_protocol=document["auth_protocol"],
                    auth_secret=document["auth_secret"], priv_protocol=document["priv_protocol"],
                    priv_secret=document["priv_secret"], context_name=document.get("context_name", ""),
                )
            except (KeyError, TypeError, SNMPv3ConfigError) as error:
                raise CredentialStoreError("local SNMPv3 credentials are invalid") from error
            return SNMPv3Session(target, credentials, policy=policy, timeout_seconds=timeout_seconds, retries=retries)
        community = entry.get("community")
        if not isinstance(community, str) or not community:
            raise CredentialStoreError("local SNMP community is missing")
        return SNMPv2cSession(target, community, policy=policy, timeout_seconds=timeout_seconds, retries=retries)
