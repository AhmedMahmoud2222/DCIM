"""SEC-02 (Issue #38): online re-encryption onto the current key version, against PostgreSQL."""

import json
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.credential_key_rotation import ENCRYPTED_COLUMNS, rotate_credential_ciphertexts
from app.core.config import Settings
from app.core.secrets import SettingsKeyProvider, ciphertext_version, decrypt_secret, encrypt_secret
from app.domain.integration.models import Collector, Integration
from app.domain.operations.models import ItsmConnection, NotificationChannel

KEY1 = Fernet.generate_key().decode()
KEY2 = Fernet.generate_key().decode()
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _provider(*, with_v2: bool) -> SettingsKeyProvider:
    return SettingsKeyProvider(Settings(
        _env_file=None, database_url="postgresql+asyncpg://user:pass@localhost/test", redis_url="redis://localhost:6379/0",
        jwt_secret_key="x" * 32, credential_encryption_key=KEY1,
        credential_encryption_keys=json.dumps({"v2": KEY2}) if with_v2 else None,
    ))


async def _seed(db_session) -> dict[tuple[str, str], str]:
    """One row per encrypted column, mixing v1-prefixed and legacy unprefixed ciphertext."""
    v1 = _provider(with_v2=False)
    legacy = Fernet(KEY1.encode()).encrypt(b"legacy-collector-secret").decode()
    db_session.add_all([
        Collector(id=uuid.uuid4(), name=f"rot-{uuid.uuid4().hex}", collector_type="central", status="active",
                  secret_ciphertext=legacy, secret_rotated_at=NOW),
        Integration(id=uuid.uuid4(), name=f"rot-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.10",
                    credential_ciphertext=encrypt_secret("community", provider=v1)),
        Integration(id=uuid.uuid4(), name=f"rot-{uuid.uuid4().hex}", integration_type="snmp", target_host="192.0.2.11",
                    credential_ciphertext=None),
        NotificationChannel(id=uuid.uuid4(), name=f"rot-{uuid.uuid4().hex}", kind="webhook", url_display="https://hooks.example",
                            url_ciphertext=encrypt_secret("https://hooks.example/t/abc", provider=v1),
                            secret_ciphertext=encrypt_secret("sign-key", provider=v1)),
        ItsmConnection(id=uuid.uuid4(), name=f"rot-{uuid.uuid4().hex}", provider="servicenow", base_url="https://sn.example",
                       username="svc", secret_ciphertext=encrypt_secret("itsm-password", provider=v1)),
    ])
    await db_session.commit()
    return {
        ("collector", "secret_ciphertext"): "legacy-collector-secret",
        ("integration", "credential_ciphertext"): "community",
        ("notification_channel", "url_ciphertext"): "https://hooks.example/t/abc",
        ("notification_channel", "secret_ciphertext"): "sign-key",
        ("itsm_connection", "secret_ciphertext"): "itsm-password",
    }


async def _stored(db_session) -> dict[tuple[str, str], list[tuple[str, datetime]]]:
    db_session.expire_all()
    stored = {}
    for model, column in ENCRYPTED_COLUMNS:
        rows = (await db_session.execute(
            select(getattr(model, column), model.updated_at).where(getattr(model, column).is_not(None))
        )).all()
        stored[(model.__tablename__, column)] = [tuple(row) for row in rows]
    return stored


async def test_rotation_moves_every_column_to_current_version_and_is_idempotent(db_engine, db_session):
    expected = await _seed(db_session)
    before = await _stored(db_session)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    provider = _provider(with_v2=True)

    dry = await rotate_credential_ciphertexts(factory, provider=provider, batch_size=1, dry_run=True)
    assert sum(r.rotated for r in dry) == 5
    assert await _stored(db_session) == before  # dry run wrote nothing

    first = await rotate_credential_ciphertexts(factory, provider=provider, batch_size=1)
    assert {(r.table, r.column): r.rotated for r in first} == dict.fromkeys(expected, 1)
    assert all(not r.undecryptable_ids and not r.changed_concurrently for r in first)
    after = await _stored(db_session)
    for key, plaintext in expected.items():
        [(ciphertext, updated_at)] = after[key]
        assert ciphertext_version(ciphertext) == "v2"
        assert decrypt_secret(ciphertext, provider=provider) == plaintext
        assert updated_at == before[key][0][1]  # re-encryption is not a content change

    second = await rotate_credential_ciphertexts(factory, provider=provider, batch_size=1)
    assert sum(r.rotated for r in second) == 0
    assert all(r.versions_before == {"v2": 1} for r in second)
    assert await _stored(db_session) == after


async def test_rotation_reports_and_skips_undecryptable_values(db_engine, db_session):
    tampered = encrypt_secret("s", provider=_provider(with_v2=False))[:-6] + "AAAAAA"
    db_session.add(ItsmConnection(id=uuid.uuid4(), name="rot-bad", provider="servicenow", base_url="https://sn.example",
                                  username="svc", secret_ciphertext=tampered))
    await db_session.commit()
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    reports = await rotate_credential_ciphertexts(factory, provider=_provider(with_v2=True))
    [itsm] = [r for r in reports if r.table == "itsm_connection"]
    assert len(itsm.undecryptable_ids) == 1 and itsm.rotated == 0
    db_session.expire_all()
    assert (await db_session.execute(select(ItsmConnection.secret_ciphertext))).scalar_one() == tampered
