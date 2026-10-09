"""SNMPv3 User-based Security Model primitives (RFC 3414, RFC 3826, RFC 7860).

Only authPriv is supported, and only algorithms that are still sound:

* authentication: HMAC-SHA-1 (96 bit), HMAC-SHA-224/256/384/512 (RFC 7860 truncations);
* privacy: AES-128/192/256 in CFB128 mode. AES-192/256 use the localized key directly, so
  they are accepted only with an authentication hash at least as long as the key (the
  Blumenthal key-extension scheme for shorter hashes is deliberately not implemented).

MD5, DES, noAuthNoPriv and authNoPriv are rejected with an explicit error rather than
negotiated down. Secrets live only in `SNMPv3Credentials`, whose `repr` never includes
them, and in key material derived from them; nothing here logs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # cryptography >= 49 moved CFB out of the primitives package
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover - older cryptography releases
    from cryptography.hazmat.primitives.ciphers.modes import CFB

# protocol -> (hashlib name, truncated MAC length, hash output length)
AUTH_PROTOCOLS: dict[str, tuple[str, int, int]] = {
    "sha1": ("sha1", 12, 20),
    "sha224": ("sha224", 16, 28),
    "sha256": ("sha256", 24, 32),
    "sha384": ("sha384", 32, 48),
    "sha512": ("sha512", 48, 64),
}
# protocol -> key length in bytes
PRIV_PROTOCOLS: dict[str, int] = {"aes128": 16, "aes192": 24, "aes256": 32}
INSECURE_PROTOCOLS = frozenset({"md5", "des", "3des", "none", "noauth", "nopriv"})
MIN_SECRET_LENGTH = 8
MAX_SECRET_LENGTH = 256
MAX_USERNAME_LENGTH = 32
MAX_CONTEXT_NAME_LENGTH = 255
_PASSWORD_EXPANSION = 1_048_576  # RFC 3414 A.2.1


class SNMPv3ConfigError(ValueError):
    """The credential/algorithm combination is invalid or unsupported. Messages never
    contain secret values."""


def _check_secret(label: str, value: str) -> None:
    if not isinstance(value, str) or not MIN_SECRET_LENGTH <= len(value.encode("utf-8")) <= MAX_SECRET_LENGTH:
        raise SNMPv3ConfigError(f"{label} must be {MIN_SECRET_LENGTH} to {MAX_SECRET_LENGTH} bytes")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SNMPv3ConfigError(f"{label} must not contain control characters")


def validate_algorithms(auth_protocol: str, priv_protocol: str) -> None:
    for label, value in (("authentication", auth_protocol), ("privacy", priv_protocol)):
        if value.lower() in INSECURE_PROTOCOLS:
            raise SNMPv3ConfigError(f"{label} protocol {value!r} is insecure and not supported; use authPriv with SHA-2/AES")
    if auth_protocol not in AUTH_PROTOCOLS:
        raise SNMPv3ConfigError(f"unsupported authentication protocol {auth_protocol!r}; expected one of {sorted(AUTH_PROTOCOLS)}")
    if priv_protocol not in PRIV_PROTOCOLS:
        raise SNMPv3ConfigError(f"unsupported privacy protocol {priv_protocol!r}; expected one of {sorted(PRIV_PROTOCOLS)}")
    hash_length = AUTH_PROTOCOLS[auth_protocol][2]
    if hash_length < PRIV_PROTOCOLS[priv_protocol]:
        raise SNMPv3ConfigError(
            f"{priv_protocol} needs a {PRIV_PROTOCOLS[priv_protocol]}-byte key but {auth_protocol} yields only {hash_length}; "
            "choose a longer authentication hash"
        )


@dataclass(frozen=True, slots=True)
class SNMPv3Credentials:
    """authPriv credentials. Secrets are excluded from `repr`/`str` and equality-by-print."""

    username: str
    auth_protocol: str
    auth_secret: str = field(repr=False)
    priv_protocol: str
    priv_secret: str = field(repr=False)
    context_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.username, str) or not 1 <= len(self.username.encode("utf-8")) <= MAX_USERNAME_LENGTH:
            raise SNMPv3ConfigError(f"username must be 1 to {MAX_USERNAME_LENGTH} bytes")
        if len(self.context_name.encode("utf-8")) > MAX_CONTEXT_NAME_LENGTH:
            raise SNMPv3ConfigError("context_name is too long")
        validate_algorithms(self.auth_protocol, self.priv_protocol)
        _check_secret("authentication secret", self.auth_secret)
        _check_secret("privacy secret", self.priv_secret)


def password_to_key(password: str, hash_name: str) -> bytes:
    """RFC 3414 A.2.1: hash the password repeated to 1 MiB."""
    raw = password.encode("utf-8")
    stream = (raw * (_PASSWORD_EXPANSION // len(raw) + 1))[:_PASSWORD_EXPANSION]
    return hashlib.new(hash_name, stream).digest()


def localize_key(master_key: bytes, engine_id: bytes, hash_name: str) -> bytes:
    """RFC 3414 A.2.2: bind a master key to one authoritative engine."""
    return hashlib.new(hash_name, master_key + engine_id + master_key).digest()


@dataclass(frozen=True, slots=True)
class UsmKeys:
    auth_key: bytes = field(repr=False)
    priv_key: bytes = field(repr=False)
    mac_length: int


def derive_keys(credentials: SNMPv3Credentials, engine_id: bytes) -> UsmKeys:
    hash_name, mac_length, _hash_length = AUTH_PROTOCOLS[credentials.auth_protocol]
    auth_key = localize_key(password_to_key(credentials.auth_secret, hash_name), engine_id, hash_name)
    priv_key = localize_key(password_to_key(credentials.priv_secret, hash_name), engine_id, hash_name)
    return UsmKeys(auth_key, priv_key[: PRIV_PROTOCOLS[credentials.priv_protocol]], mac_length)


def compute_mac(auth_protocol: str, auth_key: bytes, message: bytes) -> bytes:
    hash_name, mac_length, _ = AUTH_PROTOCOLS[auth_protocol]
    return hmac.new(auth_key, message, hash_name).digest()[:mac_length]


def verify_mac(auth_protocol: str, auth_key: bytes, message: bytes, received: bytes) -> bool:
    return hmac.compare_digest(compute_mac(auth_protocol, auth_key, message), received)


def new_salt() -> bytes:
    return os.urandom(8)


def _iv(engine_boots: int, engine_time: int, salt: bytes) -> bytes:
    return engine_boots.to_bytes(4, "big") + engine_time.to_bytes(4, "big") + salt


def encrypt_scoped_pdu(priv_key: bytes, engine_boots: int, engine_time: int, salt: bytes, plaintext: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(priv_key), CFB(_iv(engine_boots, engine_time, salt))).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def decrypt_scoped_pdu(priv_key: bytes, engine_boots: int, engine_time: int, salt: bytes, ciphertext: bytes) -> bytes:
    if len(salt) != 8:
        raise SNMPv3ConfigError("privacy parameters must be 8 bytes")
    decryptor = Cipher(algorithms.AES(priv_key), CFB(_iv(engine_boots, engine_time, salt))).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()
