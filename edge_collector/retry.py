from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential retry timing with symmetric jitter."""

    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for(self, attempts: int, *, random_value: float) -> float:
        """Returns the next retry delay; `random_value` is uniformly in [0, 1]."""
        if attempts < 0:
            raise ValueError("attempts cannot be negative")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between 0 and 1")
        unjittered = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** min(attempts, 63)))
        jitter_multiplier = 1 + self.jitter_ratio * ((2 * random_value) - 1)
        return min(self.max_delay_seconds, unjittered * jitter_multiplier)
