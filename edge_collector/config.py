from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Local-only settings for the durable outbound observation buffer."""

    database_path: Path
    max_records: int = 10_000
    max_bytes: int = 64 * 1024 * 1024
    retention_seconds: int = 24 * 60 * 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path))
        if self.max_records < 1:
            raise ValueError("max_records must be at least 1")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        if self.retention_seconds < 1:
            raise ValueError("retention_seconds must be at least 1")
