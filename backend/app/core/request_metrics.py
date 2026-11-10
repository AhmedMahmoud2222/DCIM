"""Small process-local API metrics with a fixed, bounded label set."""

from collections import defaultdict
from threading import RLock

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
BUCKETS_MS = (10, 25, 50, 100, 250, 500, 1000, 2500)

_lock = RLock()
_counts: dict[tuple[str, str], int] = defaultdict(int)
_durations: dict[str, float] = defaultdict(float)
_buckets: dict[tuple[str, int], int] = defaultdict(int)


def observe(*, method: str, status_code: int, duration_ms: float) -> None:
    method_label = method if method in METHODS else "OTHER"
    status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"
    with _lock:
        _counts[(method_label, status_class)] += 1
        _durations[method_label] += max(0.0, duration_ms)
        for upper_bound in BUCKETS_MS:
            if duration_ms <= upper_bound:
                _buckets[(method_label, upper_bound)] += 1


def snapshot() -> dict:
    """Return a bounded-cardinality snapshot; no URL, user, or query labels are stored."""
    with _lock:
        return {
            "requests_total": [
                {"method": method, "status_class": status_class, "count": count}
                for (method, status_class), count in sorted(_counts.items())
            ],
            "request_duration_ms": {
                method: {
                    "sum": round(_durations[method], 2),
                    "buckets": [
                        {"le": upper_bound, "count": _buckets[(method, upper_bound)]}
                        for upper_bound in BUCKETS_MS
                    ],
                }
                for method in sorted(_durations)
            },
        }
