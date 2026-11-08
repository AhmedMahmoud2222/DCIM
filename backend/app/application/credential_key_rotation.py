"""Online re-encryption of stored credentials onto the current key version (SEC-02, Issue #38).

Every column below holds ciphertext written by `app.core.secrets.encrypt_secret`. Rotation
decrypts each value under the version it names and re-encrypts it under the provider's current
version, while the application keeps serving: `decrypt_secret` reads every configured version,
so rows on old and new versions coexist safely during the run.

- Idempotent: a value already on the current version is never touched, so a second run is a no-op.
- Compare-and-swap: each UPDATE matches the exact ciphertext that was read. If the application
  replaced the value in between (always under the current version), the row is left alone.
- Batched: one short transaction per batch, never one transaction over a whole table.
- Re-encryption is not a content change, so `updated_at` is kept as it was.
- Never logs or returns plaintext or ciphertext; reports carry counts and row ids only.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.secrets import (
    KeyProvider,
    SecretDecryptionError,
    ciphertext_version,
    decrypt_secret,
    default_key_provider,
    encrypt_secret,
)
from app.domain.integration.models import Collector, Integration
from app.domain.operations.models import ItsmConnection, NotificationChannel

# Every column that stores `encrypt_secret` output. A new encrypted column must be added here.
ENCRYPTED_COLUMNS: tuple[tuple[Any, str], ...] = (
    (Collector, "secret_ciphertext"),
    (Integration, "credential_ciphertext"),
    (NotificationChannel, "url_ciphertext"),
    (NotificationChannel, "secret_ciphertext"),
    (ItsmConnection, "secret_ciphertext"),
)


@dataclass
class ColumnRotationReport:
    table: str
    column: str
    versions_before: Counter[str] = field(default_factory=Counter)
    rotated: int = 0
    changed_concurrently: int = 0
    undecryptable_ids: list[uuid.UUID] = field(default_factory=list)


async def rotate_credential_ciphertexts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: KeyProvider | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> list[ColumnRotationReport]:
    """Re-encrypt every stored credential that is not on the current key version. With
    `dry_run`, `rotated` counts the rows that would change and nothing is written."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    provider = provider or default_key_provider()
    current = provider.current_version()
    reports = []
    for model, column_name in ENCRYPTED_COLUMNS:
        column = getattr(model, column_name)
        report = ColumnRotationReport(table=model.__tablename__, column=column_name)
        last_id: uuid.UUID | None = None
        while True:
            async with session_factory() as session:
                query = select(model.id, column).where(column.is_not(None)).order_by(model.id).limit(batch_size)
                if last_id is not None:
                    query = query.where(model.id > last_id)
                rows = (await session.execute(query)).all()
                if not rows:
                    break
                for row_id, ciphertext in rows:
                    version = ciphertext_version(ciphertext)
                    report.versions_before[version] += 1
                    if version == current:
                        continue
                    try:
                        resealed = encrypt_secret(decrypt_secret(ciphertext, provider=provider), provider=provider)
                    except SecretDecryptionError:
                        report.undecryptable_ids.append(row_id)
                        continue
                    if dry_run:
                        report.rotated += 1
                        continue
                    result = await session.execute(
                        update(model)
                        .where(model.id == row_id, column == ciphertext)
                        .values({column_name: resealed, "updated_at": model.updated_at})
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount == 1:  # type: ignore[attr-defined]
                        report.rotated += 1
                    else:
                        report.changed_concurrently += 1
                if not dry_run:
                    await session.commit()
                last_id = rows[-1][0]
        reports.append(report)
    return reports
