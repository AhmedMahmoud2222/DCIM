"""SEC-02 (Issue #38): versioned credential encryption, legacy decryption and key validation."""

import json

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings
from app.core.secrets import (
    SecretDecryptionError,
    SettingsKeyProvider,
    ciphertext_version,
    decrypt_secret,
    encrypt_secret,
)

KEY1 = Fernet.generate_key().decode()
KEY2 = Fernet.generate_key().decode()
KEY3 = Fernet.generate_key().decode()


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://user:pass@localhost/test",
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret_key": "x" * 32,
        "credential_encryption_key": KEY1,
    }
    return Settings(**{**values, **overrides})


def _provider(**extra: str) -> SettingsKeyProvider:
    return SettingsKeyProvider(_settings(credential_encryption_keys=json.dumps(extra) if extra else None))


def test_single_version_round_trip_is_prefixed_v1():
    provider = _provider()
    ciphertext = encrypt_secret("snmp-community", provider=provider)
    assert ciphertext.startswith("v1:") and "snmp-community" not in ciphertext
    assert ciphertext_version(ciphertext) == "v1"
    assert decrypt_secret(ciphertext, provider=provider) == "snmp-community"


def test_default_provider_round_trip_uses_application_settings():
    assert decrypt_secret(encrypt_secret("p@ss:word")) == "p@ss:word"


def test_two_versions_encrypt_under_current_and_still_read_legacy_unprefixed():
    provider = _provider(v2=KEY2)
    assert provider.current_version() == "v2"
    ciphertext = encrypt_secret("api-key", provider=provider)
    assert ciphertext_version(ciphertext) == "v2"
    assert decrypt_secret(ciphertext, provider=provider) == "api-key"

    legacy = Fernet(KEY1.encode()).encrypt(b"old-row").decode()  # the pre-versioning stored format
    assert ciphertext_version(legacy) == "v1"
    assert decrypt_secret(legacy, provider=provider) == "old-row"


def test_v1_prefixed_ciphertext_still_decrypts_after_adding_v2():
    before = encrypt_secret("collector-secret", provider=_provider())
    assert decrypt_secret(before, provider=_provider(v2=KEY2)) == "collector-secret"


def test_highest_numeric_version_is_current_not_lexicographic():
    provider = _provider(v2=KEY2, v10=KEY3)
    assert provider.current_version() == "v10"
    assert decrypt_secret(encrypt_secret("s", provider=provider), provider=provider) == "s"


def test_unconfigured_version_raises_secret_decryption_error():
    ciphertext = encrypt_secret("s", provider=_provider(v2=KEY2))
    with pytest.raises(SecretDecryptionError, match="v2"):
        decrypt_secret(ciphertext, provider=_provider())


def test_prefixed_ciphertext_is_never_tried_against_another_key():
    # Same token, wrong version label: must fail closed even though v1 could decrypt it.
    token = Fernet(KEY1.encode()).encrypt(b"s").decode()
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(f"v2:{token}", provider=_provider(v2=KEY2))


@pytest.mark.parametrize("mutate", [
    lambda c: c[:-6] + ("A" if c[-6] != "A" else "B") + c[-5:],  # flip one token character
    lambda c: c[:3] + c[3:20],  # truncated token
    lambda c: "v1:not-a-fernet-token",
    lambda c: "",
])
def test_tampered_ciphertext_raises_secret_decryption_error(mutate):
    provider = _provider(v2=KEY2)
    ciphertext = encrypt_secret("s", provider=provider)
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(mutate(ciphertext), provider=provider)


@pytest.mark.parametrize("raw, expected", [
    (json.dumps({"v2": "SECRET-not-a-fernet-key-SECRET"}), "v2 is not a valid Fernet key"),
    (json.dumps({"v2": "SECRET"}), "v2 is not a valid Fernet key"),
    (json.dumps({"v1": KEY2}), "may not define v1"),
    (json.dumps({"2": KEY2}), "version labels"),
    (json.dumps({"v02": KEY2}), "version labels"),
    (json.dumps({}), "non-empty JSON object"),
    (json.dumps([KEY2]), "non-empty JSON object"),
    ("{SECRET", "must be a JSON object"),
])
def test_invalid_key_configuration_fails_settings_without_leaking_the_key(raw, expected):
    with pytest.raises(ValidationError) as excinfo:
        _settings(credential_encryption_keys=raw)
    message = str(excinfo.value)
    assert expected in message
    assert "SECRET" not in message
    assert KEY1 not in message and KEY2 not in message


def test_invalid_v1_is_rejected_once_multi_version_keys_are_configured():
    with pytest.raises(ValidationError, match="v1 is not a valid Fernet key") as excinfo:
        _settings(credential_encryption_key="y" * 44, credential_encryption_keys=json.dumps({"v2": KEY2}))
    assert "y" * 44 not in str(excinfo.value)


def test_single_key_settings_are_unchanged():
    # Existing deployments set only CREDENTIAL_ENCRYPTION_KEY; an empty CREDENTIAL_ENCRYPTION_KEYS
    # (as Compose renders an unset variable) means the same thing.
    for extra in (None, "", "  "):
        settings = _settings(credential_encryption_keys=extra)
        assert settings.credential_encryption_key_versions() == {1: KEY1}
