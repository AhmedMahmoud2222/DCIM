"""Phase 10A PR-5 storage abstraction (architectural plan, directive 2). A minimal
key/bytes protocol — every caller in this codebase only ever needs to save content-
addressed bytes and read them back by key, never directory listing, metadata, or partial
reads, so the protocol stays deliberately small rather than anticipating capabilities
nothing here uses yet."""

from typing import Protocol


class StorageBackend(Protocol):
    def save(self, key: str, content: bytes) -> None:
        """Writes `content` under `key`, replacing any existing object at that key.
        Idempotent for content-addressed keys (the same key is always the same bytes, by
        construction of the caller), so implementations are free to skip the write
        entirely when the key already exists."""
        ...

    def read(self, key: str) -> bytes:
        """Raises FileNotFoundError if `key` has never been saved."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None:
        """Removes `key`; a missing key is not an error. Callers must only delete keys no
        remaining database row references (content-addressed keys can be shared)."""
        ...
