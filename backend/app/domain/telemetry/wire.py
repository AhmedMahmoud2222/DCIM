"""Lossless parsing of telemetry request numbers (Issue #128 / G3).

pydantic parses a JSON number through a 64-bit float even for a `Decimal` field, so `1234567890.12345678` becomes
`1234567890.1234567` before any application code runs. This module parses the telemetry request body itself:
numeric tokens keep their exact text, so a bare JSON number, a JSON integer and a decimal string are all converted to
`Decimal` without a float intermediary. Everything else about the request (HMAC authentication, the byte-counted size
cap, the batch limit, the error shape and the G1 contract pinning) is unchanged: this runs after authentication on the
already-bounded raw body and hands a plain dict to the same pydantic models.

Failure classes:
  * structurally unusable input (invalid JSON, a boolean/null/array/object where a number belongs, a string that is
    not a decimal numeral, a numeral longer than 64 characters) -> the usual 422 validation error for the whole request;
  * a well-formed number the service cannot store (NaN, Infinity, out of range, absurd exponent) -> the existing
    per-record `INVALID_TELEMETRY_VALUE` acknowledgement, so valid peers in the batch still persist.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.telemetry.numeric import MAX_LEXEME_CHARS

_NUMERAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NONFINITE_WORDS = {"nan", "inf", "infinity"}
NUMBER_FIELDS = ("value", "source_scale")


class WireNumberError(ValueError):
    """The request cannot be parsed into exact numbers (reported as a 422 for the whole request)."""


class _LexFloat(float):
    """A JSON fractional/exponent number that remembers its exact token (still a float for JSON re-serialisation)."""

    lexeme: str


class _LexInt(int):
    lexeme: str


class _NonFinite:
    def __init__(self, token: str) -> None:
        self.token = token


def _checked(token: str) -> str:
    if len(token) > MAX_LEXEME_CHARS:
        raise WireNumberError("A numeric literal exceeds the maximum length.")
    return token


def _parse_float(token: str) -> float:
    value = _LexFloat(_checked(token))
    value.lexeme = token
    return value


def _parse_int(token: str) -> int:
    value = _LexInt(int(_checked(token)))
    value.lexeme = token
    return value


def _parse_constant(token: str) -> Any:
    return _NonFinite(token)


def _contains_nonfinite(node: Any) -> bool:
    if isinstance(node, _NonFinite):
        return True
    if isinstance(node, dict):
        return any(_contains_nonfinite(item) for item in node.values())
    if isinstance(node, list):
        return any(_contains_nonfinite(item) for item in node)
    return False


def _nonfinite(text: str) -> Decimal:
    bare = text.strip().lower().lstrip("+-")
    if bare.startswith("nan"):
        return Decimal("NaN")
    return Decimal("-Infinity") if text.strip().startswith("-") else Decimal("Infinity")


def exact_number(raw: Any) -> tuple[Decimal, str]:
    """Return `(value, lexeme)` for a parsed JSON number or decimal string, or raise `WireNumberError`.

    A non-finite input yields a non-finite `Decimal` (and its text) so the service can reject that one record.
    """
    if isinstance(raw, _NonFinite):
        return _nonfinite(raw.token), raw.token
    if isinstance(raw, bool) or raw is None:
        raise WireNumberError("A number is required.")
    if isinstance(raw, (_LexFloat, _LexInt)):
        text = raw.lexeme
    elif isinstance(raw, str):
        text = raw
        if len(text) > MAX_LEXEME_CHARS:
            raise WireNumberError("A numeric literal exceeds the maximum length.")
        if text.lower().lstrip("+-") in _NONFINITE_WORDS:
            return _nonfinite(text), text
        if not _NUMERAL.match(text):
            raise WireNumberError("A decimal numeral is required.")
    else:
        raise WireNumberError("A number is required.")
    try:
        return Decimal(text), text
    except InvalidOperation as error:  # an exponent beyond Decimal's own limits
        raise WireNumberError("A numeric literal is malformed.") from error


def loads_exact(raw_body: bytes) -> Any:
    """Parse JSON keeping numeric lexemes; non-finite constants become markers."""
    try:
        return json.loads(raw_body, parse_float=_parse_float, parse_int=_parse_int, parse_constant=_parse_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise WireNumberError("The request body is not valid JSON.") from error
    except WireNumberError:
        raise
    except ValueError as error:  # e.g. an integer token beyond the interpreter's digit limit
        raise WireNumberError("The request body contains a malformed numeral.") from error


def prepare_records(document: Any) -> tuple[Any, dict[int, str]]:
    """Replace each record's exact numbers by `Decimal` and return `{record index: value lexeme}`.

    Only `value` and `source_scale` of the entries of `records` are touched. Anything else that is not a plain JSON
    value (a non-finite marker inside `attributes`) is refused, because it could not be stored as JSON.
    """
    lexemes: dict[int, str] = {}
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        return document, lexemes
    for index, record in enumerate(document["records"]):
        if not isinstance(record, dict):
            continue
        for key in NUMBER_FIELDS:
            if key in record and record[key] is not None:
                number, text = exact_number(record[key])
                record[key] = number
                if key == "value":
                    lexemes[index] = text
        attributes = record.get("attributes")
        if attributes is not None and _contains_nonfinite(attributes):
            raise WireNumberError("attributes must not contain non-finite numbers.")
    return document, lexemes
