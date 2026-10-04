# ruff: noqa: E501  (the regular-expression tables read better unwrapped)
"""Turns page text into candidate specification values with provenance.

Pure and deterministic: standard library only, no I/O, no database, no network. It runs inside
the sandboxed analysis child (`analysis_worker`), because its input is text taken from a hostile
file. Every regular expression here is linear (no nested quantifiers) and runs on lines capped
at `MAX_LINE_CHARS`, and the work per page, per table and per document is bounded.

What this module never does:
- convert units (a value in kW stays kW; a value in °F stays °F; the manufacturer's raw value
  and unit are always kept next to the parsed ones),
- fill a missing value, a missing unit or a missing axis order,
- pick between conflicting values,
- attribute a value to the target model without evidence in the document.

The semantic fields are deliberately separate keys: typical, maximum, rated, idle and
unspecified power; PSU capacity; heat dissipation; airflow volume and direction; width, height,
depth and unordered dimensions; net and shipping weight; rack units; electrical and
environmental attributes. A matching unit never merges two of them."""

import re
from dataclasses import dataclass, field

EXTRACTOR_VERSION = "datasheet-extractor-1.0"
UNIT_REGISTRY_VERSION = "1"

MAX_LINE_CHARS = 1000
MAX_LINES_PER_PAGE = 3000
MAX_TABLE_COLUMNS = 24
MAX_CANDIDATES = 1000
MAX_IDENTIFIED_MODELS = 50
MAX_SOURCE_TEXT_CHARS = 500
MAX_TOKEN_CHARS = 32
MAX_ABS_VALUE = 1e12  # the candidate table stores NUMERIC(18,6)

# ---------------------------------------------------------------------------- unit registry
# token (lowercase, no spaces) -> (canonical label, dimension). Labels are metadata about what the
# manufacturer wrote; nothing here converts a value.
UNIT_TOKENS: dict[str, tuple[str, str]] = {
    "w": ("W", "power"),
    "watt": ("W", "power"),
    "watts": ("W", "power"),
    "kw": ("kW", "power"),
    "btu/hr": ("BTU/hr", "thermal"),
    "btu/h": ("BTU/hr", "thermal"),
    "btuh": ("BTU/hr", "thermal"),
    "btu/hour": ("BTU/hr", "thermal"),
    "btus/hr": ("BTU/hr", "thermal"),
    "mm": ("mm", "length"),
    "cm": ("cm", "length"),
    "m": ("m", "length"),
    "in": ("in", "length"),
    "inch": ("in", "length"),
    "inches": ("in", "length"),
    '"': ("in", "length"),
    "kg": ("kg", "mass"),
    "g": ("g", "mass"),
    "lb": ("lb", "mass"),
    "lbs": ("lb", "mass"),
    "pound": ("lb", "mass"),
    "pounds": ("lb", "mass"),
    "cfm": ("CFM", "airflow"),
    "m3/h": ("m3/h", "airflow"),
    "m³/h": ("m3/h", "airflow"),
    "m3/hr": ("m3/h", "airflow"),
    "cmh": ("m3/h", "airflow"),
    "l/s": ("L/s", "airflow"),
    "v": ("V", "voltage"),
    "vac": ("V", "voltage"),
    "vdc": ("V", "voltage"),
    "a": ("A", "current"),
    "amp": ("A", "current"),
    "amps": ("A", "current"),
    "hz": ("Hz", "frequency"),
    "c": ("°C", "temperature"),
    "°c": ("°C", "temperature"),
    "℃": ("°C", "temperature"),
    "degc": ("°C", "temperature"),
    "f": ("°F", "temperature"),
    "°f": ("°F", "temperature"),
    "℉": ("°F", "temperature"),
    "degf": ("°F", "temperature"),
    "%": ("%", "percent"),
    "u": ("U", "rack"),
    "ru": ("U", "rack"),
}

# ---------------------------------------------------------------------------- field registry


@dataclass(frozen=True)
class FieldRule:
    key: str
    label: re.Pattern[str]
    dimensions: frozenset[str]
    kind: str = "quantity"  # quantity | range | text | dimensions | psu | rack


def _rule(key: str, pattern: str, dims: str, kind: str = "quantity") -> FieldRule:
    return FieldRule(key, re.compile(pattern, re.IGNORECASE), frozenset(dims.split(",")), kind)


_P = r"(?:\s+(?:consumption|draw|usage|output))?"
# Order matters: the first rule whose label matches wins, so specific labels precede general ones.
FIELD_RULES: tuple[FieldRule, ...] = (
    _rule("power_idle_w", rf"\bidle\s+power{_P}|\bpower{_P}\s+(?:at|in)\s+idle|\bpower\s+consumption\s*\(\s*idle\s*\)", "power"),
    _rule(
        "power_typical_w",
        rf"\btypical\s+(?:system\s+)?power{_P}|\bpower\s+consumption\s*\(\s*typical\s*\)|\btypical\s+consumption",
        "power",
    ),
    _rule(
        "power_max_w",
        rf"\b(?:max(?:imum)?|peak)\s+(?:system\s+)?power{_P}|\bpower\s+consumption\s*\(\s*max(?:imum)?\s*\)",
        "power",
    ),
    _rule("power_rated_w", rf"\brated\s+power{_P}|\bnominal\s+power|\bpower\s+consumption\s*\(\s*rated\s*\)", "power"),
    _rule(
        "psu_capacity_w",
        r"\bpower\s+suppl(?:y|ies)(?:\s+(?:capacity|rating|wattage|size))?|\bpsu(?:\s+(?:capacity|rating|wattage))?",
        "power",
        "psu",
    ),
    _rule("power_unspecified_w", r"\bpower\s+(?:consumption|draw|usage)\b", "power"),
    _rule(
        "heat_dissipation",
        r"\bheat\s+(?:dissipation|output|load|release)|\bthermal\s+(?:dissipation|output|load)",
        "thermal,power",
    ),
    _rule("airflow_volume", r"\bair\s*flow(?:\s+(?:rate|volume|capacity))?", "airflow", "airflow"),
    _rule("shipping_weight", r"\b(?:shipping|gross|package[d]?|boxed)\s+weight", "mass"),
    _rule("weight", r"\b(?:net\s+|unit\s+|system\s+)?weight\b", "mass"),
    _rule("rack_units", r"\brack\s+units?\b|\bheight\s*\(\s*(?:u|ru|rack\s+units?)\s*\)|\bform\s+factor\b", "rack", "rack"),
    _rule("dimensions", r"\bdimensions?\b", "length", "dimensions"),
    _rule("width", r"\bwidth\b", "length"),
    _rule("height", r"\bheight\b", "length"),
    _rule("depth", r"\bdepth\b", "length"),
    _rule("storage_temperature", r"\b(?:storage|non-?operating)\s+temp(?:erature)?(?:\s+range)?", "temperature", "range"),
    _rule("operating_temperature", r"\b(?:operating|ambient)\s+temp(?:erature)?(?:\s+range)?", "temperature", "range"),
    _rule("operating_humidity", r"\b(?:operating\s+)?(?:relative\s+)?humidity(?:\s+range)?", "percent", "range"),
    _rule(
        "input_voltage", r"\b(?:input\s+|rated\s+|nominal\s+|supply\s+)?voltage(?:\s+range)?\b|\bac\s+input\b", "voltage", "range"
    ),
    _rule("input_frequency", r"\b(?:input\s+)?frequency\b", "frequency", "range"),
    _rule("current_max_a", r"\bmax(?:imum)?\s+(?:input\s+)?current\b", "current"),
    _rule("current_rated_a", r"\brated\s+(?:input\s+)?current\b", "current"),
    _rule("current_input_a", r"\binput\s+current\b", "current"),
)
FIELD_KEYS = frozenset(rule.key for rule in FIELD_RULES) | {"psu_quantity", "dimensions_unordered", "airflow_direction"}

# Text rules
_DIRECTION = re.compile(
    r"\b(front[\s-]+to[\s-]+(?:back|rear)|(?:back|rear)[\s-]+to[\s-]+front|side[\s-]+to[\s-]+side|"
    r"left[\s-]+to[\s-]+right|right[\s-]+to[\s-]+left|bottom[\s-]+to[\s-]+top)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------- number parsing
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
_NUM_RE = re.compile(_NUM)
_RANGE_RE = re.compile(rf"(?P<a>{_NUM})\s*(?P<unit_a>[A-Za-z°%℃℉\"/³]{{0,8}})\s*(?P<sep>-|–|—|~|to|/)\s*(?P<b>{_NUM})")
_TRIPLE_RE = re.compile(
    rf"(?P<a>{_NUM})\s*[A-Za-z\"]{{0,4}}\s*(?:[xX×]|by)\s*(?P<b>{_NUM})\s*[A-Za-z\"]{{0,4}}\s*(?:[xX×]|by)\s*(?P<c>{_NUM})"
)
_PSU_COUNT_RE = re.compile(rf"(?P<n>\d{{1,2}})\s*(?:[xX×])\s*(?P<w>{_NUM})")
_UNIT_AFTER = re.compile(r"\s*(?P<unit>[A-Za-z°%℃℉\"³][A-Za-z0-9°%/³.]{0,11}|\"|%)")
_RACK_RE = re.compile(r"(?<![\w.])(?P<n>\d{1,2})\s?(?P<u>RU|U)(?![A-Za-z0-9])", re.IGNORECASE)
_AXIS_LABEL = re.compile(r"\(\s*([A-Za-z]+(?:\s*[xX×]\s*[A-Za-z]+){2})\s*\)")


def parse_number(token: str) -> tuple[float | None, list[str]]:
    """`1,200` is 1200 (comma followed by exactly three digits), `1,5` is 1.5 with a decimal-comma
    flag, `1.200` is 1.2 but flagged ambiguous, because a reader in another locale means 1200."""
    flags: list[str] = []
    text = token
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        digits_before = head.replace(",", "")
        if text.count(",") > 1 or (len(tail) == 3 and digits_before.isdigit() and len(digits_before) <= 3):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
            flags.append("decimal_comma")
    elif "." in text:
        head, _, tail = text.partition(".")
        if len(tail) == 3 and head.isdigit() and 1 <= len(head) <= 3:
            flags.append("number_format_ambiguous")
    try:
        value = float(text)
    except ValueError:
        return None, flags
    if abs(value) >= MAX_ABS_VALUE:  # also rejects inf from absurdly long digit strings
        return None, flags
    return value, flags


def resolve_unit(raw_unit: str, dimensions: frozenset[str]) -> tuple[str | None, list[str]]:
    """Maps the manufacturer's unit token to a registry label when it belongs to the field's
    dimension; otherwise keeps it unresolved and says why."""
    cleaned = raw_unit.strip().rstrip(".,;").lower()
    if not cleaned:
        return None, ["unit_missing"]
    entry = UNIT_TOKENS.get(cleaned)
    if entry is None:
        return None, ["unit_unrecognized"]
    label, dimension = entry
    if dimension not in dimensions:
        return None, ["unit_dimension_mismatch"]
    return label, []


# ---------------------------------------------------------------------------- model evidence


def norm_token(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


_QUANTITY_LIKE = re.compile(r"^\d[\d.,]*[A-Za-z%°/]{0,6}$")
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/.+]{1,30}$")
_HEADER_LABELS = frozenset(
    {
        "model",
        "models",
        "specification",
        "specifications",
        "feature",
        "features",
        "parameter",
        "parameters",
        "item",
        "spec",
        "sku",
        "part number",
        "description",
        "technical specifications",
        "product",
    }
)
_HEADING_LABELLED = re.compile(r"^(?:model|sku|part\s*(?:number|no\.?)|product)\s*[:#]\s*(?P<t>\S{2,32})\s*$", re.IGNORECASE)
_HEADING_TITLED = re.compile(
    r"^(?P<t>[A-Za-z0-9][A-Za-z0-9\-_/.+]{1,30})\s+(?:technical\s+)?(?:specifications?|data\s*sheet|datasheet|specs)\s*$",
    re.IGNORECASE,
)


def is_model_like(token: str) -> bool:
    if not _MODEL_TOKEN.match(token) or _QUANTITY_LIKE.match(token):
        return False
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def target_identifiers(names: list[str]) -> set[str]:
    """Normalised forms the document may use for the target: each full name and each whitespace
    separated word that looks like a model token (so "PowerEdge R650" also matches "R650")."""
    found: set[str] = set()
    for name in names:
        if not name:
            continue
        full = norm_token(name)
        if len(full) >= 3:
            found.add(full)
        for word in re.split(r"[\s/,;]+", name):
            if is_model_like(word) and len(norm_token(word)) >= 3:
                found.add(norm_token(word))
    return found


# ---------------------------------------------------------------------------- data


@dataclass
class PageText:
    page: int
    text: str
    method: str  # native | ocr
    ocr_confidence: float | None = None  # mean word confidence, 0-100


@dataclass
class Candidate:
    field_key: str
    value_numeric: float | None
    value_max: float | None
    value_text: str | None
    unit: str | None
    raw_value: str
    raw_unit: str
    source_text: str
    page_number: int
    method: str  # native | table | ocr
    confidence: float
    flags: list[str] = field(default_factory=list)
    model_context: str | None = None  # the model identifier the evidence names, as written
    model_match: str = "unattributed"  # target | other | unattributed
    conflict_group_key: str | None = None


@dataclass
class ExtractionResult:
    candidates: list[Candidate]
    model_resolution: str
    identified_models: list[str]
    warnings: list[dict[str, object]]


# ---------------------------------------------------------------------------- line parsing


def _clip(text: str) -> str:
    return text[:MAX_SOURCE_TEXT_CHARS]


def _base_confidence(method: str, page: PageText, *, labelled: bool) -> float:
    if page.method == "ocr":
        mean = (page.ocr_confidence if page.ocr_confidence is not None else 0.0) / 100.0
        return round(min(0.75, 0.9 * mean), 3)
    if method == "table":
        return 0.9
    return 0.85 if labelled else 0.7


def _finish(conf: float, flags: list[str]) -> float:
    penalty = sum(
        0.15
        for flag in flags
        if flag
        in {
            "unit_missing",
            "unit_unrecognized",
            "unit_dimension_mismatch",
            "number_format_ambiguous",
            "decimal_comma",
            "axis_order_unspecified",
            "semantics_unspecified",
            "multi_value_line",
        }
    )
    if "ocr_low_confidence" in flags:
        penalty += 0.1
    return round(max(0.05, min(1.0, conf - penalty)), 3)


def _unit_after(text: str, end: int, dims: frozenset[str]) -> tuple[str | None, str, list[str], int]:
    match = _UNIT_AFTER.match(text, end)
    raw = match.group("unit") if match else ""
    if match and raw.lower() in {"to", "x", "by", "and", "or", "of", "at", "per", "for", "with"}:
        raw = ""
        match = None
    label, flags = resolve_unit(raw, dims)
    return label, raw, flags, (match.end() if match else end)


def _make(
    rule_key: str,
    page: PageText,
    method: str,
    source: str,
    *,
    value: float | None,
    value_max: float | None,
    text_value: str | None,
    unit: str | None,
    raw_value: str,
    raw_unit: str,
    flags: list[str],
    labelled: bool,
    model_context: str | None,
) -> Candidate:
    flags = list(dict.fromkeys(flags))
    if page.method == "ocr" and page.ocr_confidence is not None and page.ocr_confidence < 70:
        flags.append("ocr_low_confidence")
    return Candidate(
        field_key=rule_key,
        value_numeric=value,
        value_max=value_max,
        value_text=text_value,
        unit=unit,
        raw_value=raw_value,
        raw_unit=raw_unit,
        source_text=_clip(source),
        page_number=page.page,
        method="ocr" if page.method == "ocr" else method,
        confidence=_finish(_base_confidence(method, page, labelled=labelled), flags),
        flags=flags,
        model_context=model_context,
    )


def _quantities(value_text: str, rule: FieldRule) -> list[tuple[float, float | None, str, str | None, str, list[str]]]:
    """Every quantity found in `value_text` as (value, max, raw_value, unit, raw_unit, flags)."""
    results: list[tuple[float, float | None, str, str | None, str, list[str]]] = []
    position = 0
    while len(results) < 8:
        found = _NUM_RE.search(value_text, position)
        if not found:
            break
        first, first_flags = parse_number(found.group(0))
        if first is None:
            position = found.end()
            continue
        upper: float | None = None
        end = found.end()
        flags = list(first_flags)
        raw = found.group(0)
        if rule.kind == "range":
            ranged = _RANGE_RE.match(value_text, found.start())
            if ranged:
                second, second_flags = parse_number(ranged.group("b"))
                if second is not None:
                    upper, end = second, ranged.end()
                    flags += second_flags + ["range_value" if ranged.group("sep") != "/" else "list_of_values"]
                    raw = value_text[found.start() : ranged.end()]
        unit, raw_unit, unit_flags, after = _unit_after(value_text, end, rule.dimensions)
        results.append((first, upper, raw, unit, raw_unit, flags + unit_flags))
        position = max(after, end)
    return results


def _dimension_candidates(
    page: PageText, method: str, source: str, label_text: str, value_text: str, model_context: str | None, labelled: bool
) -> list[Candidate]:
    triple = _TRIPLE_RE.search(value_text)
    if not triple:
        return []
    values = [parse_number(triple.group(name)) for name in ("a", "b", "c")]
    if any(v is None for v, _ in values):
        return []
    flags: list[str] = [flag for _, f in values for flag in f]
    unit, raw_unit, unit_flags, _end = _unit_after(value_text, triple.end(), frozenset({"length"}))
    if unit is None and "unit_missing" in unit_flags:
        # a unit can also trail the first number ("44 mm x 440 mm x 600 mm")
        inner = re.search(r"\d\s*(mm|cm|in|m)\b", value_text[: triple.end()], re.IGNORECASE)
        if inner:
            unit, _inner_flags = resolve_unit(inner.group(1), frozenset({"length"}))
            raw_unit, unit_flags = inner.group(1), []
    raw_value = triple.group(0)
    axis = _AXIS_LABEL.search(label_text) or _AXIS_LABEL.search(value_text)
    order: list[str] = []
    if axis:
        initials = [part.strip()[:1].lower() for part in re.split(r"[xX×]", axis.group(1))]
        mapping = {"w": "width", "h": "height", "d": "depth"}
        if len(set(initials)) == 3 and all(i in mapping for i in initials):
            order = [mapping[i] for i in initials]
    if not order:
        return [
            _make(
                "dimensions_unordered",
                page,
                method,
                source,
                value=None,
                value_max=None,
                text_value=" x ".join(str(int(v)) if v == int(v) else str(v) for v, _ in values if v is not None),
                unit=unit,
                raw_value=raw_value,
                raw_unit=raw_unit,
                flags=flags + unit_flags + ["axis_order_unspecified"],
                labelled=labelled,
                model_context=model_context,
            )
        ]
    return [
        _make(
            axis_name,
            page,
            method,
            source,
            value=values[index][0],
            value_max=None,
            text_value=None,
            unit=unit,
            raw_value=raw_value,
            raw_unit=raw_unit,
            flags=flags + unit_flags,
            labelled=labelled,
            model_context=model_context,
        )
        for index, axis_name in enumerate(order)
    ]


def _split_label_value(line: str) -> tuple[str, str] | None:
    if ":" in line:
        label, _, value = line.partition(":")
        if 0 < len(label.strip()) <= 80 and value.strip():
            return label.strip(), value.strip()
    parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
    if len(parts) == 2 and len(parts[0]) <= 80:
        return parts[0].strip(), parts[1].strip()
    return None


def _first_rule(text: str) -> tuple[FieldRule, re.Match[str]] | None:
    for rule in FIELD_RULES:
        found = rule.label.search(text)
        if found:
            return rule, found
    return None


_SLASH_LABELS = re.compile(
    r"^(?P<a>typical|maximum|max|peak|rated|idle)\s*/\s*(?P<b>typical|maximum|max|peak|rated|idle)\s+power\b", re.IGNORECASE
)
_SLASH_KEYS = {
    "typical": "power_typical_w",
    "maximum": "power_max_w",
    "max": "power_max_w",
    "peak": "power_max_w",
    "rated": "power_rated_w",
    "idle": "power_idle_w",
}


def parse_segment(
    page: PageText, method: str, source: str, label_text: str, value_text: str, model_context: str | None, *, labelled: bool
) -> list[Candidate]:
    """One label/value pair (a line, or a table row for one column) to zero or more candidates."""
    slash = _SLASH_LABELS.match(label_text.strip())
    if slash:
        parts = [p for p in re.split(r"\s*/\s*", value_text) if p.strip()]
        if len(parts) != 2:
            return []
        out: list[Candidate] = []
        rule_power = frozenset({"power"})
        for key_name, part in zip((slash.group("a").lower(), slash.group("b").lower()), parts, strict=True):
            probe = FieldRule(_SLASH_KEYS[key_name], re.compile("x"), rule_power)
            quantities = _quantities(part if re.search(r"[A-Za-z]", part) else part + " " + _trailing_unit(parts), probe)
            for value, upper, raw, unit, raw_unit, flags in quantities[:1]:
                out.append(
                    _make(
                        probe.key,
                        page,
                        method,
                        source,
                        value=value,
                        value_max=upper,
                        text_value=None,
                        unit=unit,
                        raw_value=raw,
                        raw_unit=raw_unit,
                        flags=flags,
                        labelled=labelled,
                        model_context=model_context,
                    )
                )
        return out

    matched = _first_rule(label_text)
    if matched is None:
        return []
    rule, _found = matched
    if rule.kind == "dimensions":
        return _dimension_candidates(page, method, source, label_text, value_text, model_context, labelled)
    if rule.kind == "airflow":
        direction = _DIRECTION.search(value_text)
        quantities = _quantities(value_text, rule)
        if not quantities and direction:
            return [
                _make(
                    "airflow_direction",
                    page,
                    method,
                    source,
                    value=None,
                    value_max=None,
                    text_value=re.sub(r"\s+", "-", direction.group(1).lower().replace("rear", "back")),
                    unit=None,
                    raw_value=direction.group(1),
                    raw_unit="",
                    flags=[],
                    labelled=labelled,
                    model_context=model_context,
                )
            ]
        rule = FieldRule("airflow_volume", rule.label, rule.dimensions)
    if rule.kind == "rack":
        rack = _RACK_RE.search(value_text) or _RACK_RE.search(label_text)
        if not rack:
            return []
        return [
            _make(
                "rack_units",
                page,
                method,
                source,
                value=float(rack.group("n")),
                value_max=None,
                text_value=None,
                unit="U",
                raw_value=rack.group("n"),
                raw_unit=rack.group("u"),
                flags=[],
                labelled=labelled,
                model_context=model_context,
            )
        ]
    if rule.kind == "psu":
        count = _PSU_COUNT_RE.search(value_text)
        extra: list[Candidate] = []
        text_for_value = value_text
        if count:
            quantity, _ = parse_number(count.group("n"))
            if quantity is not None and 1 <= quantity <= 16:
                extra.append(
                    _make(
                        "psu_quantity",
                        page,
                        method,
                        source,
                        value=quantity,
                        value_max=None,
                        text_value=None,
                        unit=None,
                        raw_value=count.group("n"),
                        raw_unit="",
                        flags=[],
                        labelled=labelled,
                        model_context=model_context,
                    )
                )
                text_for_value = value_text[count.start("w") :]
        quantities = _quantities(text_for_value, rule)[:1]
        return extra + [
            _make(
                rule.key,
                page,
                method,
                source,
                value=v,
                value_max=u,
                text_value=None,
                unit=unit,
                raw_value=raw,
                raw_unit=raw_unit,
                flags=flags,
                labelled=labelled,
                model_context=model_context,
            )
            for v, u, raw, unit, raw_unit, flags in quantities
        ]

    quantities = _quantities(value_text, rule)
    extra_flags: list[str] = []
    if len(quantities) > 1:
        extra_flags.append("multi_value_line")
    if rule.key == "power_unspecified_w":
        extra_flags.append("semantics_unspecified")
    return [
        _make(
            rule.key,
            page,
            method,
            source,
            value=v,
            value_max=u,
            text_value=None,
            unit=unit,
            raw_value=raw,
            raw_unit=raw_unit,
            flags=flags + extra_flags,
            labelled=labelled,
            model_context=model_context,
        )
        for v, u, raw, unit, raw_unit, flags in quantities
    ]


def _trailing_unit(parts: list[str]) -> str:
    for part in reversed(parts):
        found = re.search(r"[A-Za-z°%]+/?[A-Za-z]*\s*$", part)
        if found:
            return found.group(0)
    return ""


# ---------------------------------------------------------------------------- page walk


def _cells(line: str) -> list[str]:
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def _header_columns(cells: list[str]) -> list[str] | None:
    if len(cells) < 2 or len(cells) > MAX_TABLE_COLUMNS + 1:
        return None
    if cells[0].lower() in _HEADER_LABELS and len(cells) >= 3 and all(is_model_like(c) for c in cells[1:]):
        return cells[1:]
    if len(cells) >= 2 and all(is_model_like(c) for c in cells):
        return cells
    return None


def _heading_model(line: str) -> str | None:
    stripped = line.strip()
    for pattern in (_HEADING_LABELLED, _HEADING_TITLED):
        found = pattern.match(stripped)
        if found and is_model_like(found.group("t").strip(".,:;")):
            return found.group("t").strip(".,:;")
    if len(stripped) <= MAX_TOKEN_CHARS and " " not in stripped and is_model_like(stripped):
        return stripped
    return None


def analyze_pages(pages: list[PageText], target_names: list[str]) -> ExtractionResult:
    warnings: list[dict[str, object]] = []
    raw_candidates: list[Candidate] = []
    models: dict[str, str] = {}  # norm -> first display form
    current_model: str | None = None
    skipped_lines = 0

    for page in pages:
        lines = page.text.splitlines()
        if len(lines) > MAX_LINES_PER_PAGE:
            warnings.append({"code": "line_limit", "page": page.page})
            lines = lines[:MAX_LINES_PER_PAGE]
        columns: list[str] | None = None
        misses = 0
        for raw_line in lines:
            if len(raw_line) > MAX_LINE_CHARS:
                skipped_lines += 1
                continue
            line = raw_line.rstrip()
            if not line.strip():
                if columns is not None:
                    misses += 1
                    if misses > 1:
                        columns = None
                continue
            cells = _cells(line)
            header = _header_columns(cells)
            if header is not None:
                columns = header
                misses = 0
                for column in header:
                    models.setdefault(norm_token(column), column)
                continue
            if columns is not None:
                if len(cells) - 1 == len(columns):
                    misses = 0
                    label_text = cells[0]
                    for column, value_text in zip(columns, cells[1:], strict=True):
                        for candidate in parse_segment(
                            page, "table", line.strip(), label_text, value_text, column, labelled=True
                        ):
                            candidate.flags.append("model_context_from_table_column")
                            raw_candidates.append(candidate)
                    continue
                misses += 1
                if misses > 1:
                    columns = None
            heading = _heading_model(line)
            if heading is not None:
                current_model = heading
                models.setdefault(norm_token(heading), heading)
                continue
            split = _split_label_value(line)
            produced: list[Candidate] = []
            if split is not None:
                produced = parse_segment(page, "native", line.strip(), split[0], split[1], current_model, labelled=True)
            if not produced:
                matched = _first_rule(line)
                if matched is not None:
                    produced = parse_segment(
                        page,
                        "native",
                        line.strip(),
                        line[: matched[1].end()],
                        line[matched[1].end() :],
                        current_model,
                        labelled=False,
                    )
            for candidate in produced:
                if current_model is not None:
                    candidate.flags.append("model_context_from_heading")
                raw_candidates.append(candidate)
            if len(raw_candidates) > MAX_CANDIDATES:
                break
        if len(raw_candidates) > MAX_CANDIDATES:
            warnings.append({"code": "candidate_limit_reached", "page": page.page})
            raw_candidates = raw_candidates[:MAX_CANDIDATES]
            break
    if skipped_lines:
        warnings.append({"code": "lines_too_long_skipped", "count": skipped_lines})

    if len(models) > MAX_IDENTIFIED_MODELS:
        warnings.append({"code": "model_limit_reached"})
    resolution, attributed = _attribute(raw_candidates, models, target_names)
    candidates = _mark_conflicts(_dedupe(attributed))
    return ExtractionResult(candidates, resolution, list(models.values())[:MAX_IDENTIFIED_MODELS], warnings)


def _attribute(candidates: list[Candidate], models: dict[str, str], target_names: list[str]) -> tuple[str, list[Candidate]]:
    wanted = target_identifiers(target_names)
    matching = [norm for norm in models if norm in wanted]
    if len(matching) > 1:
        resolution = "ambiguous_target"
    elif matching and len(matching) == len(models):
        resolution = "single_model_matched"
    elif matching:
        resolution = "multi_model_matched"
    elif models:
        resolution = "target_not_found"
    else:
        resolution = "no_model_evidence"

    for candidate in candidates:
        context = norm_token(candidate.model_context) if candidate.model_context else None
        if resolution in {"ambiguous_target", "no_model_evidence"}:
            candidate.model_match = "unattributed"
            if resolution == "no_model_evidence":
                candidate.flags.append("no_model_evidence")
            else:
                candidate.flags.append("ambiguous_target_model")
        elif context is not None:
            candidate.model_match = "target" if context in wanted else "other"
        elif resolution == "single_model_matched":
            candidate.model_match = "target"
        else:
            candidate.model_match = "unattributed"
            candidate.flags.append(
                "no_model_context_in_multi_model_document" if resolution == "multi_model_matched" else "target_model_not_found"
            )
        candidate.flags = list(dict.fromkeys(candidate.flags))
    return resolution, candidates


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[tuple[object, ...]] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        key = (
            candidate.field_key,
            candidate.value_numeric,
            candidate.value_max,
            candidate.value_text,
            candidate.unit,
            candidate.page_number,
            candidate.source_text,
            candidate.model_context,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _mark_conflicts(candidates: list[Candidate]) -> list[Candidate]:
    groups: dict[tuple[str, str, str], list[Candidate]] = {}
    for candidate in candidates:
        key = (candidate.field_key, candidate.model_match, norm_token(candidate.model_context or ""))
        groups.setdefault(key, []).append(candidate)
    for (field_key, match, model_norm), members in groups.items():
        distinct = {(m.value_numeric, m.value_max, (m.value_text or "").lower(), m.unit, m.raw_unit.lower()) for m in members}
        if len(distinct) > 1:
            group_key = f"{field_key}|{match}|{model_norm}"
            for member in members:
                member.flags.append("conflict")
                member.conflict_group_key = group_key
                member.confidence = round(max(0.05, member.confidence - 0.2), 3)
    return candidates
