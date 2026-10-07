"""Credential/config consistency rules for SNMP integrations.

The API accepts an SNMPv3 credential as a structured, write-only object. This module turns
(integration type, current state, requested change) into the `config` and ciphertext to
store, or a 422 whose message never contains secret material. One rule keeps the stored
state coherent: `config["snmpv3"]` (non-secret display data) and the ciphertext are only
ever written together, here.
"""

from pydantic import BaseModel, ConfigDict, SecretStr

from app.application.drivers.snmpv3 import SnmpV3Credential, SnmpV3CredentialError
from app.application.network.profile_schema import reject_secret_like_keys
from app.core.errors import ApiError
from app.core.secrets import encrypt_secret

SNMP_VERSIONS = ("v1", "v2c", "v3")


class SnmpV3In(BaseModel):
    """Write-only authPriv credential. Secrets are `SecretStr`, so even an accidental
    `repr`/`model_dump` of the request body masks them."""

    model_config = ConfigDict(extra="forbid")

    username: str
    auth_protocol: str
    auth_secret: SecretStr
    priv_protocol: str
    priv_secret: SecretStr
    context_name: str = ""

    def to_credential(self) -> SnmpV3Credential:
        return SnmpV3Credential(
            username=self.username, auth_protocol=self.auth_protocol, auth_secret=self.auth_secret.get_secret_value(),
            priv_protocol=self.priv_protocol, priv_secret=self.priv_secret.get_secret_value(), context_name=self.context_name,
        )


def _invalid(detail: str) -> ApiError:
    return ApiError(status_code=422, title="Invalid Credential", detail=detail, type_="https://dcim.internal/errors/validation")


def resolve_snmp_security(
    *, integration_type: str, existing_config: dict | None, existing_ciphertext: str | None, new_config: dict | None,
    credential: str | None, snmpv3: SnmpV3In | None,
) -> tuple[dict, str | None]:
    """Returns the (config, credential_ciphertext) to persist.

    `new_config`/`credential`/`snmpv3` are None when the request does not change them.
    """
    config = dict(new_config if new_config is not None else (existing_config or {}))
    if integration_type != "snmp":
        if snmpv3 is not None:
            raise _invalid("snmpv3 credentials are only valid for snmp integrations.")
        ciphertext = encrypt_secret(credential) if credential else existing_ciphertext
        return config, ciphertext

    try:
        reject_secret_like_keys(config)
    except ValueError as exc:
        raise _invalid("config must not contain secret values; use the credential or snmpv3 fields.") from exc
    previous = (existing_config or {}).get("snmpv3")
    if new_config is not None and "snmpv3" in new_config and new_config["snmpv3"] != previous:
        raise _invalid("config.snmpv3 is managed by the snmpv3 credential object and cannot be set directly.")
    version = config.get("version")
    if version is not None and version not in SNMP_VERSIONS:
        raise _invalid(f"config.version must be one of {SNMP_VERSIONS}.")

    if snmpv3 is not None:
        if credential:
            raise _invalid("Provide either credential (community string) or snmpv3, not both.")
        if version not in (None, "v3"):
            raise _invalid("snmpv3 credentials require config.version to be 'v3'.")
        try:
            sealed = snmpv3.to_credential()
        except SnmpV3CredentialError as exc:
            raise _invalid(str(exc)) from exc
        config["version"] = "v3"
        config["snmpv3"] = sealed.describe()
        return config, encrypt_secret(sealed.to_plaintext())

    if version == "v3":
        if credential:
            raise _invalid("SNMPv3 integrations take the structured snmpv3 credential, not a community string.")
        if previous is None or existing_ciphertext is None:
            raise _invalid("SNMPv3 integrations require the snmpv3 credential object.")
        config["snmpv3"] = previous  # preserved, never user-editable
        return config, existing_ciphertext

    # v1 / v2c (or unspecified): community-string behaviour is unchanged.
    if previous is not None:
        if credential is None:
            raise _invalid("Leaving SNMPv3 requires a new community string in credential.")
        config.pop("snmpv3", None)
    ciphertext = encrypt_secret(credential) if credential else existing_ciphertext
    return config, ciphertext


def credential_kind(integration_type: str, config: dict | None, ciphertext: str | None) -> str:
    if ciphertext is None:
        return "none"
    if integration_type == "snmp" and (config or {}).get("snmpv3"):
        return "snmpv3"
    return "secret"
