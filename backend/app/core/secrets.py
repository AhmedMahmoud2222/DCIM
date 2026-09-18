"""Reversible encryption for Integration credentials (SNMP community strings, REST API
keys, etc.) that a protocol driver must retrieve in plaintext to actually authenticate
to the device being polled -- unlike `app/core/security.py`'s Argon2 hashing (one-way,
verify-only, used for user passwords and the Collector's own machine trust secret),
this needs to come back out.

No reversible-secret-storage convention existed anywhere in this codebase before Phase
8 (confirmed by a repository-wide search for "Fernet"/"encrypt"/"cryptography" turning
up nothing). This module is the minimal, real primitive that gap requires: symmetric
authenticated encryption via `cryptography`'s `Fernet` (AES-128-CBC + HMAC-SHA256,
industry-standard, not a custom scheme), keyed from `settings.credential_encryption_key`.

**Explicitly an OPEN DECISION, not a finished production design**
(PHASE8_EDGE_COLLECTOR_CONTRACT.md): a single static key read from environment/settings
is adequate for this phase's foundation but is not where real device credentials should
live long-term -- production deployment should migrate this key's storage to a real
KMS/secrets manager (AWS KMS, HashiCorp Vault, etc.), rotating the encryption key
independently of application deployment. That migration is out of this phase's scope;
this module's `encrypt_secret`/`decrypt_secret` functions are the seam a KMS-backed
implementation would replace without changing any caller."""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class SecretDecryptionError(Exception):
    """The stored ciphertext could not be decrypted with the current key -- either
    corrupted, or encrypted under a since-rotated key with no migration performed."""


def _fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.credential_encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError("Credential ciphertext could not be decrypted with the current key.") from exc
