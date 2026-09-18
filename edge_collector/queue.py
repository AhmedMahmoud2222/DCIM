from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import CollectorConfig

UTC = timezone.utc
MIGRATION_VERSION = 1
MIGRATION_FILE = Path(__file__).with_name("migrations") / "001_initial.sql"


class QueueCorruptionError(RuntimeError):
    """The durable queue cannot safely be opened."""

    def __init__(self, diagnostic: dict[str, str]) -> None:
        self.diagnostic = diagnostic
        super().__init__("queue integrity check failed")


@dataclass(frozen=True, slots=True)
class QueueRecord:
    record_id: str
    occurred_at: datetime
    payload: Mapping[str, Any]
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class QueueMetrics:
    count: int
    bytes: int
    oldest_at: datetime | None
    dropped: int
    retries: int
    expired: int


class SQLiteQueue:
    """A transactionally bounded SQLite queue for unacknowledged observations."""

    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(str(config.database_path), isolation_level=None)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            check = self._connection.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(str(check))
        except sqlite3.DatabaseError as error:
            try:
                self._connection.close()
            except AttributeError:
                pass
            raise QueueCorruptionError(
                {
                    "database_path": str(config.database_path),
                    "check": "integrity_check",
                    "result": "failed",
                }
            ) from error

    def close(self) -> None:
        self._connection.close()

    def enqueue(self, record: QueueRecord) -> bool:
        payload_json = json.dumps(record.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload_bytes = len(payload_json.encode("utf-8"))
        now = _utc_now()
        with self._transaction():
            self._expire(now)
            existing = self._connection.execute(
                "SELECT 1 FROM queue_records WHERE record_id = ?", (record.record_id,)
            ).fetchone()
            if existing:
                return False
            if payload_bytes > self.config.max_bytes:
                self._increment_counter("dropped")
                return False
            self._evict_for(payload_bytes)
            self._connection.execute(
                """INSERT INTO queue_records
                   (record_id, occurred_at, payload_json, payload_bytes, attempts, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.record_id,
                    _as_storage_time(record.occurred_at),
                    payload_json,
                    payload_bytes,
                    record.attempts,
                    _as_storage_time(record.occurred_at),
                    _as_storage_time(now),
                ),
            )
        return True

    def list_due(self, now: datetime, limit: int = 500) -> list[QueueRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._transaction():
            self._expire(now)
            rows = self._connection.execute(
                """SELECT record_id, occurred_at, payload_json, attempts FROM queue_records
                   WHERE next_attempt_at <= ?
                   ORDER BY occurred_at, record_id LIMIT ?""",
                (_as_storage_time(now), limit),
            ).fetchall()
        return [
            QueueRecord(row[0], _from_storage_time(row[1]), json.loads(row[2]), row[3])
            for row in rows
        ]

    def acknowledge(self, record_ids: Iterable[str]) -> int:
        identifiers = tuple(dict.fromkeys(record_ids))
        if not identifiers:
            return 0
        with self._transaction():
            cursor = self._connection.executemany(
                "DELETE FROM queue_records WHERE record_id = ?", ((identifier,) for identifier in identifiers)
            )
        return cursor.rowcount

    def mark_retry(self, record_ids: Iterable[str], *, now: datetime, delay_seconds: float) -> int:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        identifiers = tuple(dict.fromkeys(record_ids))
        if not identifiers:
            return 0
        retry_at = _as_storage_time(now + timedelta(seconds=delay_seconds))
        with self._transaction():
            cursor = self._connection.executemany(
                """UPDATE queue_records
                   SET attempts = attempts + 1, next_attempt_at = ?
                   WHERE record_id = ?""",
                ((retry_at, identifier) for identifier in identifiers),
            )
            self._increment_counter("retries", cursor.rowcount)
        return cursor.rowcount

    def metrics(self) -> QueueMetrics:
        row = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0), MIN(occurred_at) FROM queue_records"
        ).fetchone()
        counters = dict(self._connection.execute("SELECT name, value FROM queue_counters").fetchall())
        return QueueMetrics(
            count=row[0], bytes=row[1], oldest_at=_from_storage_time(row[2]) if row[2] else None,
            dropped=counters.get("dropped", 0), retries=counters.get("retries", 0), expired=counters.get("expired", 0),
        )

    def _migrate(self) -> None:
        with self._transaction():
            # sqlite3.executescript() commits an active transaction before it
            # runs. Execute this deliberately simple, versioned migration one
            # statement at a time so schema creation is atomic too.
            for statement in MIGRATION_FILE.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    self._connection.execute(statement)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS queue_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            for name in ("dropped", "retries", "expired"):
                self._connection.execute("INSERT OR IGNORE INTO queue_counters(name, value) VALUES (?, 0)", (name,))
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (MIGRATION_VERSION, _as_storage_time(_utc_now())),
            )

    def _expire(self, now: datetime) -> int:
        cutoff = _as_storage_time(now - timedelta(seconds=self.config.retention_seconds))
        cursor = self._connection.execute("DELETE FROM queue_records WHERE occurred_at < ?", (cutoff,))
        self._increment_counter("expired", cursor.rowcount)
        return cursor.rowcount

    def _evict_for(self, incoming_bytes: int) -> None:
        while True:
            count, byte_count = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) FROM queue_records"
            ).fetchone()
            if count < self.config.max_records and byte_count + incoming_bytes <= self.config.max_bytes:
                return
            oldest = self._connection.execute(
                "SELECT record_id FROM queue_records ORDER BY occurred_at, record_id LIMIT 1"
            ).fetchone()
            if oldest is None:
                return
            self._connection.execute("DELETE FROM queue_records WHERE record_id = ?", (oldest[0],))
            self._increment_counter("dropped")

    def _increment_counter(self, name: str, amount: int = 1) -> None:
        if amount:
            self._connection.execute("UPDATE queue_counters SET value = value + ? WHERE name = ?", (amount, name))

    def _transaction(self):
        return _Transaction(self._connection)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.connection.execute("ROLLBACK" if exc_type else "COMMIT")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_storage_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_storage_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
