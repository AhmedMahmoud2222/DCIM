"""Reversible encryption for Integration credentials (SNMP community strings, REST API
keys, etc.) that a protocol driver must retrieve in plaintext to actually authenticate
to the device being polled -- unlike `app/core/security.py`'s Argon2 hashing (one-way,
verify-only, used for user passwords and the Collector's own machine trust secret),
this needs to come back out.

No reversible-secret-storage convention existed anywhere in this codebase before Phase
8 (confirmed by a repository-wide search for "Fernet"/"encrypt"/"cryptography" turning
up nothing). This module is the minimal, real primitive that gap requires: symmetric
authenticated encryption via `cryptography`'s `Fernet` (AES-128-CBC + HMAC-SHA256,
industry-standard, not a custom scheme).

**Versioned keys (SEC-02, Issue #38).** Ciphertext is written as `v<N>:<fernet token>`,
where `v<N>` names the key generation that sealed it. Fernet tokens are urlsafe-base64
and never contain ':', so the prefix is unambiguous. Ciphertext with no prefix is the
format written before versioning existed and is decrypted as v1, so every row already
stored keeps working with no data migration. A prefixed ciphertext is only ever tried
against the key its prefix names: an unknown version or a failed authentication raises
`SecretDecryptionError`; no other key is tried. New ciphertext always uses the highest
configured version, and `scripts/rotate_credential_encryption_key.py` re-encrypts
existing rows onto it while the application keeps running (docs/OPERATIONS.md).

**Key-provider seam.** Key material comes from a `KeyProvider`. The only implementation
here is `SettingsKeyProvider` (keys from `CREDENTIAL_ENCRYPTION_KEY` as v1 plus
`CREDENTIAL_ENCRYPTION_KEYS` for v2 and later). A KMS- or Vault-backed provider (for
example one that unwraps data keys through AWS KMS or reads them from a HashiCorp Vault
KV/Transit mount) would implement the same two methods and be installed as the default
provider; no caller of `encrypt_secret`/`decrypt_secret` changes. No such provider is
wired up: which external service to use is an operator decision, documented separately."""

import re
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings

_VERSIONED = re.compile(r"v([1-9][0-9]{0,8}):(.*)", re.DOTALL)
LEGACY_VERSION = "v1"


class SecretDecryptionError(Exception):
    """The stored ciphertext could not be decrypted -- corrupted, tampered with, or sealed
    under a key version that is not configured."""


class KeyProvider(Protocol):
    """Source of credential-encryption key material, by version label ("v1", "v2", ...)."""

    def key_for_version(self, version: str) -> bytes:
        """The Fernet key for `version`. Raises KeyError when that version is unknown."""
        ...

    def current_version(self) -> str:
        """The version new ciphertext is sealed with."""
        ...


class SettingsKeyProvider:
    """Keys from application settings: `credential_encryption_key` is v1, and
    `credential_encryption_keys` adds v2 and later. The highest version is current."""

    def __init__(self, settings: Settings) -> None:
        versions = settings.credential_encryption_key_versions()
        self._keys = {f"v{number}": key.encode() for number, key in versions.items()}
        self._current = f"v{max(versions)}"

    def key_for_version(self, version: str) -> bytes:
        return self._keys[version]

    def current_version(self) -> str:
        return self._current


def default_key_provider() -> KeyProvider:
    # Built from the cached settings on each call, as `_fernet()` was before versioning, so the
    # active keys always follow whatever `get_settings()` currently returns.
    return SettingsKeyProvider(get_settings())


def ciphertext_version(ciphertext: str) -> str:
    """The key version a stored ciphertext names; unprefixed (pre-versioning) ciphertext is v1."""
    match = _VERSIONED.fullmatch(ciphertext)
    return f"v{match.group(1)}" if match else LEGACY_VERSION


def encrypt_secret(plaintext: str, *, provider: KeyProvider | None = None) -> str:
    provider = provider or default_key_provider()
    version = provider.current_version()
    token = Fernet(provider.key_for_version(version)).encrypt(plaintext.encode()).decode()
    return f"{version}:{token}"


def decrypt_secret(ciphertext: str, *, provider: KeyProvider | None = None) -> str:
    provider = provider or default_key_provider()
    match = _VERSIONED.fullmatch(ciphertext)
    version, token = (f"v{match.group(1)}", match.group(2)) if match else (LEGACY_VERSION, ciphertext)
    try:
        key = provider.key_for_version(version)
    except KeyError:
        raise SecretDecryptionError(f"Credential ciphertext names key version {version}, which is not configured.") from None
    try:
        return Fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(f"Credential ciphertext could not be decrypted with key version {version}.") from exc
