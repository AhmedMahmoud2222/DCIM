"""Read-only startup validation of the immutable registry ledger."""

import hashlib
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.telemetry.registry import REGISTRIES, snapshot_text


def validate_registry_rows(rows: Iterable[tuple[str, str, str]]) -> None:
    """Reject missing, unknown, duplicated or altered contracts; never repair DB state."""
    observed: dict[str, tuple[str, str]] = {}
    for version, canonical_text, content_hash in rows:
        if version in observed:
            raise RuntimeError("Duplicate registry ledger version")
        if hashlib.sha256(canonical_text.encode("utf-8")).hexdigest() != content_hash:
            raise RuntimeError("Registry ledger content hash mismatch")
        observed[version] = (canonical_text, content_hash)
    expected = {version: (snapshot_text(snapshot), snapshot.content_hash) for version, snapshot in REGISTRIES.items()}
    if observed != expected:
        raise RuntimeError("Registry code and database snapshots disagree")


async def verify_registry_ledger(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = await connection.execute(text(
            "SELECT version, canonical_text, content_hash FROM registry_version_snapshot ORDER BY version"
        ))
        validate_registry_rows((row[0], row[1], row[2]) for row in rows)
