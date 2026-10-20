"""SNMPv3 authPriv credential model shared by the central SNMP driver and the integration API.

Policy (identical to the edge collector's `snmp_usm.py`, which a contract test keeps in
step): only authPriv with SHA-1/SHA-2 authentication and AES privacy is accepted. MD5, DES,
noAuthNoPriv and authNoPriv are rejected explicitly, never negotiated down. AES-192/256
require an authentication hash at least as long as the AES key.

Secrets exist in three places only: the write-only API request body, the Fernet ciphertext
in `Integration.credential_ciphertext`, and the in-memory `SnmpV3Credential` handed to a
transport. `SnmpV3Credential` excludes secrets from `repr`; error messages here never
contain secret values; `describe()` is the non-secret view stored in `Integration.config`.
"""

import json
from dataclasses import dataclass, field

AUTH_PROTOCOL_HASH_BYTES = {"sha1": 20, "sha224": 28, "sha256": 32, "sha384": 48, "sha512": 64}
PRIV_PROTOCOL_KEY_BYTES = {"aes128": 16, "aes192": 24, "aes256": 32}
INSECURE_PROTOCOLS = frozenset({"md5", "des", "3des", "none", "noauth", "nopriv"})
MIN_SECRET_BYTES = 8
MAX_SECRET_BYTES = 256
MAX_USERNAME_BYTES = 32
MAX_CONTEXT_NAME_BYTES = 255
CREDENTIAL_KIND = "snmpv3"
CREDENTIAL_FORMAT_VERSION = 1


class SnmpV3CredentialError(ValueError):
    """Invalid credential/algorithm combination. Messages never contain secret values."""


def _check_secret(label: str, value: object) -> None:
    if not isinstance(value, str) or not MIN_SECRET_BYTES <= len(value.encode("utf-8")) <= MAX_SECRET_BYTES:
        raise SnmpV3CredentialError(f"{label} must be {MIN_SECRET_BYTES} to {MAX_SECRET_BYTES} bytes")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SnmpV3CredentialError(f"{label} must not contain control characters")


def validate_algorithms(auth_protocol: str, priv_protocol: str) -> None:
    for label, value in (("authentication", auth_protocol), ("privacy", priv_protocol)):
        if str(value).lower() in INSECURE_PROTOCOLS:
            raise SnmpV3CredentialError(
                f"{label} protocol {value!r} is insecure and not supported; use authPriv with SHA-2 and AES"
            )
    if auth_protocol not in AUTH_PROTOCOL_HASH_BYTES:
        raise SnmpV3CredentialError(
            f"unsupported authentication protocol {auth_protocol!r}; expected one of {sorted(AUTH_PROTOCOL_HASH_BYTES)}"
        )
    if priv_protocol not in PRIV_PROTOCOL_KEY_BYTES:
        raise SnmpV3CredentialError(
            f"unsupported privacy protocol {priv_protocol!r}; expected one of {sorted(PRIV_PROTOCOL_KEY_BYTES)}"
        )
    if AUTH_PROTOCOL_HASH_BYTES[auth_protocol] < PRIV_PROTOCOL_KEY_BYTES[priv_protocol]:
        raise SnmpV3CredentialError(
            f"{priv_protocol} needs a {PRIV_PROTOCOL_KEY_BYTES[priv_protocol]}-byte key but {auth_protocol} yields only "
            f"{AUTH_PROTOCOL_HASH_BYTES[auth_protocol]}; choose a longer authentication hash"
        )


@dataclass(frozen=True)
class SnmpV3Credential:
    username: str
    auth_protocol: str
    auth_secret: str = field(repr=False)
    priv_protocol: str
    priv_secret: str = field(repr=False)
    context_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.username, str) or not 1 <= len(self.username.encode("utf-8")) <= MAX_USERNAME_BYTES:
            raise SnmpV3CredentialError(f"username must be 1 to {MAX_USERNAME_BYTES} bytes")
        if not isinstance(self.context_name, str) or len(self.context_name.encode("utf-8")) > MAX_CONTEXT_NAME_BYTES:
            raise SnmpV3CredentialError("context_name is too long")
        validate_algorithms(self.auth_protocol, self.priv_protocol)
        _check_secret("authentication secret", self.auth_secret)
        _check_secret("privacy secret", self.priv_secret)

    def describe(self) -> dict:
        """The non-secret view stored in `Integration.config["snmpv3"]` for display."""
        return {
            "security_level": "authPriv",
            "username": self.username,
            "auth_protocol": self.auth_protocol,
            "priv_protocol": self.priv_protocol,
            "context_name": self.context_name,
        }

    def to_plaintext(self) -> str:
        """JSON handed to `encrypt_secret`. Never log or return this."""
        return json.dumps(
            {
                "kind": CREDENTIAL_KIND, "v": CREDENTIAL_FORMAT_VERSION, "username": self.username,
                "auth_protocol": self.auth_protocol, "auth_secret": self.auth_secret,
                "priv_protocol": self.priv_protocol, "priv_secret": self.priv_secret, "context_name": self.context_name,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_plaintext(cls, plaintext: str | None) -> "SnmpV3Credential":
        if not plaintext:
            raise SnmpV3CredentialError("SNMPv3 credential is missing")
        try:
            document = json.loads(plaintext)
        except ValueError as exc:
            raise SnmpV3CredentialError("SNMPv3 credential is not valid") from exc
        if (
            not isinstance(document, dict)
            or document.get("kind") != CREDENTIAL_KIND
            or document.get("v") != CREDENTIAL_FORMAT_VERSION
        ):
            raise SnmpV3CredentialError("SNMPv3 credential is not valid")
        try:
            return cls(
                username=document["username"], auth_protocol=document["auth_protocol"], auth_secret=document["auth_secret"],
                priv_protocol=document["priv_protocol"], priv_secret=document["priv_secret"],
                context_name=document.get("context_name", ""),
            )
        except KeyError as exc:
            raise SnmpV3CredentialError("SNMPv3 credential is not valid") from exc
