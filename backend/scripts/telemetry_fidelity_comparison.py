"""Issue #128 / G3: reproducible before/after comparison of source-number handling (no database needed).

    cd backend && PYTHONPATH=. python scripts/telemetry_fidelity_comparison.py

BEFORE models main as of 59bbb8d: the JSON number is parsed as a float, Decimal(str(float)) feeds the conversion, the
canonical value is computed from that UNROUNDED number (half-even), and PostgreSQL stores the raw value rounded half away
from zero. AFTER is the G3 path: the exact numeral is parsed, rounded once half-even, and the canonical value starts from
that stored raw value.

The invariant tested for every sample: canonical == convert(stored raw, unit, scale). The inputs are a fixed-seed synthetic
corpus, not production data.
"""

import json
import random
from decimal import ROUND_HALF_UP, Decimal

from app.domain.telemetry.numeric import MAX_STORABLE, InvalidTelemetryValue, ensure_storable, quantize_source
from app.domain.telemetry.registry import convert_to_canonical

Q = Decimal("0.00000001")
CASES = [("power_kw", "kW"), ("power_kw", "W"), ("temperature_c", "degF"), ("humidity_percent", "%"),
         ("airflow_m3_s", "CFM"), ("differential_pressure_pa", "inH2O")]
SCALES = [Decimal(1), Decimal("0.1"), Decimal("10"), Decimal("0.001")]


def before(literal: str, metric: str, unit: str, scale: Decimal):
    raw = Decimal(str(json.loads(literal)))                     # float intermediary
    ensure_storable(raw, "source value")
    canonical = convert_to_canonical(metric, raw, unit, source_scale=scale).value
    stored_raw = raw.quantize(Q, rounding=ROUND_HALF_UP)         # what PostgreSQL does on INSERT
    return raw, stored_raw, canonical


def after(literal: str, metric: str, unit: str, scale: Decimal):
    raw = quantize_source(Decimal(literal))                      # exact numeral, one half-even rounding
    canonical = convert_to_canonical(metric, raw, unit, source_scale=scale).value
    return Decimal(literal), raw, canonical


def corpus(count: int):
    rng = random.Random(128)
    for _ in range(count):
        integer = rng.randint(0, 10**rng.randint(1, 9))
        decimals = rng.choice([2, 4, 8, 9, 9, 9, 10, 12])
        fraction = "".join(rng.choice("0123456789") for _ in range(decimals))
        if decimals == 9 and rng.random() < 0.7:
            fraction = fraction[:-1] + "5"                       # exact ties at the 9th decimal
        yield f"{'-' if rng.random() < 0.3 else ''}{integer}.{fraction}"


def main(count: int = 20000) -> None:
    literals = list(corpus(count)) + ["1234567890.12345678", "9999999999.99999999", "-9999999999.99999999", "1.000000005"]
    totals = {"inputs": 0, "wire digits lost (float != exact)": 0, "before: rejected": 0, "after: rejected": 0,
              "before: canonical != convert(stored raw)": 0, "after: canonical != convert(stored raw)": 0,
              "before: stored raw != exact input rounded half-even": 0, "after: stored raw != exact input rounded half-even": 0}
    for literal in literals:
        metric, unit = CASES[totals["inputs"] % len(CASES)]
        scale = SCALES[(totals["inputs"] // len(CASES)) % len(SCALES)]
        totals["inputs"] += 1
        exact = Decimal(literal)
        if Decimal(str(json.loads(literal))) != exact:
            totals["wire digits lost (float != exact)"] += 1
        try:
            _, stored_raw, canonical = before(literal, metric, unit, scale)
            if convert_to_canonical(metric, stored_raw, unit, source_scale=scale).value != canonical:
                totals["before: canonical != convert(stored raw)"] += 1
            if stored_raw != exact.quantize(Q):
                totals["before: stored raw != exact input rounded half-even"] += 1
        except (InvalidTelemetryValue, ArithmeticError):
            totals["before: rejected"] += 1
        try:
            _, stored_raw, canonical = after(literal, metric, unit, scale)
            if convert_to_canonical(metric, stored_raw, unit, source_scale=scale).value != canonical:
                totals["after: canonical != convert(stored raw)"] += 1
            if stored_raw != exact.quantize(Q):
                totals["after: stored raw != exact input rounded half-even"] += 1
        except (InvalidTelemetryValue, ArithmeticError):
            totals["after: rejected"] += 1
    width = max(map(len, totals))
    for key, value in totals.items():
        print(f"{key:<{width}}  {value}")
    print(f"\nMAX_STORABLE = {MAX_STORABLE}")
    print("documented maximum accepted BEFORE over JSON:", end=" ")
    try:
        before("9999999999.99999999", "power_kw", "kW", Decimal(1)); print("yes")
    except InvalidTelemetryValue:
        print("no (rejected: the float is 1e10)")
    print("documented maximum accepted AFTER  over JSON:", after("9999999999.99999999", "power_kw", "kW", Decimal(1))[1])


if __name__ == "__main__":
    main()
