"""Deterministic synthetic signals. Pure functions: the same (key, metric, time, base) always gives the same value,
which is what makes a re-run produce identical rows (and therefore only duplicates) for every slot."""

import hashlib
import math
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal


def unit_interval(*parts: object) -> float:
    """A stable pseudo-random number in [0, 1) derived from the parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def noise(key: str, slot_epoch: int) -> float:
    """Stable noise in [-1, 1] that changes every slot."""
    return unit_interval("noise", key, slot_epoch) * 2 - 1


def diurnal(at: datetime) -> float:
    """-1..1, lowest about 03:00 UTC and highest about 15:00 UTC."""
    hours = at.hour + at.minute / 60 + at.second / 3600
    return math.sin(2 * math.pi * (hours - 9) / 24)


def slot_grid(now: datetime, interval_seconds: int, hours: float, *, after: datetime | None = None) -> list[datetime]:
    """Aligned sample times, ascending, ending at the latest slot not after `now`.

    Slots are aligned to multiples of the poll interval since the epoch, so two runs (or two mappings) agree on the
    slot and a repeated run derives the same dedup keys. `after` excludes slots at or before it (refresh)."""
    interval = max(1, int(interval_seconds))
    last = int(now.timestamp()) // interval * interval
    first = last - int(hours * 3600) // interval * interval
    slots = [datetime.fromtimestamp(t, UTC) for t in range(first, last + 1, interval)]
    if after is not None:
        slots = [s for s in slots if s > after]
    return slots


def next_slot_after(now: datetime, interval_seconds: int) -> datetime:
    interval = max(1, int(interval_seconds))
    return datetime.fromtimestamp((int(now.timestamp()) // interval + 1) * interval, UTC)


def to_decimal(value: float, places: int = 8) -> Decimal:
    return Decimal(repr(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# Hot-spot racks: a few racks run warmer so the heat map shows structure and the Warning/Critical thresholds can fire.
def _hot_offset(key: str) -> float:
    pick = unit_interval("hot", key)
    if pick < 0.03:
        return 6.0
    if pick < 0.10:
        return 3.0
    return 0.0


def canonical_value(metric: str, key: str, at: datetime, base: float | None = None) -> float:
    """The simulated value of `metric` for the series identified by `key` at `at`, in the metric's canonical unit.

    `base` anchors the series on a value that already exists (the latest real reading of an existing mapping), so
    simulated history continues the real one instead of jumping away from it."""
    slot = int(at.timestamp())
    d = diurnal(at)
    n = noise(f"{key}|{metric}", slot)
    anchor = unit_interval("base", key, metric)
    if metric in ("temperature_c", "supply_air_temperature_c", "return_air_temperature_c"):
        if metric == "supply_air_temperature_c":
            centre = 18.0 + 2.0 * anchor
        elif metric == "return_air_temperature_c":
            centre = 28.0 + 3.0 * anchor
        else:
            centre = (base if base is not None else 21.0 + 3.5 * anchor + _hot_offset(key))
        return clamp(centre + 0.9 * d + 0.35 * n, -10.0, 60.0)
    if metric == "humidity_percent":
        centre = base if base is not None else 42.0 + 8.0 * anchor
        return clamp(centre - 3.0 * d + 1.2 * n, 20.0, 80.0)
    if metric == "load_percent":
        centre = base if base is not None else 25.0 + 40.0 * anchor
        return clamp(centre + 8.0 * d + 3.0 * n, 1.0, 99.0)
    if metric == "availability":
        return 1.0
    if metric in ("power_kw", "cooling_output_kw"):
        centre = base if base is not None else 0.12 + 0.6 * anchor
        return max(0.0, centre * (1 + 0.12 * d + 0.05 * n))
    centre = base if base is not None else 1.0 + anchor
    return max(0.0, centre * (1 + 0.06 * d + 0.02 * n))


__all__ = ["canonical_value", "slot_grid", "next_slot_after", "to_decimal", "noise", "diurnal", "unit_interval"]
