"""Sanitized intermediate representation (SIR). The only structure that crosses back from the isolated
parser to the application. It holds normalized geometry/text in the *source* coordinate system plus
parser metadata; it carries no markup, no file handles and no authority. A SIR is immutable once stored:
re-processing a job re-derives candidates from the stored SIR, never from the upload."""

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from app.application.spatial_import.limits import DEFAULT_LIMITS, ParserLimits

SIR_SCHEMA_VERSION = 1
ENTITY_KINDS = ("rect", "polygon", "polyline", "line", "circle", "text")
SOURCE_UNITS = ("mm", "cm", "m", "in", "ft", "px", "unitless")
Y_AXES = ("up", "down")
UNIT_TO_MM: dict[str, float] = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


class SirInvalid(Exception):
    """The parser returned a document that does not satisfy the SIR contract or its limits."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class SirEntity:
    """One normalized shape. Rectangles carry centre + size + rotation (degrees, in the source axes);
    every other kind carries `points`."""

    kind: str
    ref: str  # stable per-source reference (DXF handle / Visio shape path); used for re-export reconciliation
    layer: str = ""
    cx: float | None = None
    cy: float | None = None
    width: float | None = None
    height: float | None = None
    rotation_deg: float = 0.0
    radius: float | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    text: str | None = None
    text_height: float | None = None
    name: str | None = None  # shape/block name (never executed, display + classification evidence only)


@dataclass
class SirDocument:
    source_format: str
    parser_name: str
    parser_version: str
    source_units: str
    units_trusted: bool
    y_axis: str
    entities: list[SirEntity] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    objects_discovered: int = 0
    unsupported_object_count: int = 0
    notes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SIR_SCHEMA_VERSION

    def bbox(self) -> dict[str, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for e in self.entities:
            if e.kind in ("rect", "circle", "text") and e.cx is not None and e.cy is not None:
                half_w = (e.width or e.radius or 0.0) / 2 if e.kind == "rect" else (e.radius or 0.0)
                half_h = (e.height or e.radius or 0.0) / 2 if e.kind == "rect" else (e.radius or 0.0)
                extent = math.hypot(half_w, half_h) if e.kind == "rect" and e.rotation_deg % 180 else None
                hw, hh = (extent, extent) if extent is not None else (half_w, half_h)
                xs += [e.cx - hw, e.cx + hw]
                ys += [e.cy - hh, e.cy + hh]
            for px, py in e.points:
                xs.append(px)
                ys.append(py)
        if not xs:
            return None
        return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_format": self.source_format,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "source_units": self.source_units,
            "units_trusted": self.units_trusted,
            "y_axis": self.y_axis,
            "layers": list(self.layers),
            "warnings": list(self.warnings),
            "objects_discovered": self.objects_discovered,
            "unsupported_object_count": self.unsupported_object_count,
            "notes": self.notes,
            "bbox": self.bbox(),
            "entities": [_entity_dict(e) for e in self.entities],
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _entity_dict(e: SirEntity) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": e.kind, "ref": e.ref, "layer": e.layer}
    for key in ("cx", "cy", "width", "height", "radius", "text_height"):
        value = getattr(e, key)
        if value is not None:
            out[key] = _r(value)
    if e.rotation_deg:
        out["rotation_deg"] = _r(e.rotation_deg)
    if e.points:
        out["points"] = [[_r(x), _r(y)] for x, y in e.points]
    if e.text is not None:
        out["text"] = e.text
    if e.name is not None:
        out["name"] = e.name
    return out


def canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sir_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


def _finite(value: Any, limits: ParserLimits) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SirInvalid("sir_bad_number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > limits.max_coordinate:
        raise SirInvalid("sir_coordinate_out_of_range")
    return number


def validate_sir(raw: Any, *, limits: ParserLimits = DEFAULT_LIMITS) -> SirDocument:
    """Parent-side re-validation of what the child returned. Raises SirInvalid; never trusts the child."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SIR_SCHEMA_VERSION:
        raise SirInvalid("sir_schema")
    units = raw.get("source_units")
    if units not in SOURCE_UNITS or raw.get("y_axis") not in Y_AXES:
        raise SirInvalid("sir_schema")
    entities_raw = raw.get("entities")
    if not isinstance(entities_raw, list) or len(entities_raw) > limits.max_entities:
        raise SirInvalid("sir_too_many_entities")
    layers = raw.get("layers", [])
    if not isinstance(layers, list) or len(layers) > limits.max_layers:
        raise SirInvalid("sir_too_many_layers")
    layer_names = [str(layer)[: limits.max_text_length] for layer in layers]
    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > limits.max_warnings:
        raise SirInvalid("sir_too_many_warnings")
    clean_warnings = [
        {"code": str(w.get("code", ""))[:64], "detail": str(w.get("detail", ""))[:300]}
        for w in warnings
        if isinstance(w, dict)
    ]
    entities: list[SirEntity] = []
    for item in entities_raw:
        if not isinstance(item, dict) or item.get("kind") not in ENTITY_KINDS:
            raise SirInvalid("sir_bad_entity")
        text = item.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > limits.max_text_length):
            raise SirInvalid("sir_text_too_long")
        points_raw = item.get("points", [])
        if not isinstance(points_raw, list) or len(points_raw) > limits.max_points_per_entity:
            raise SirInvalid("sir_too_many_points")
        points: list[tuple[float, float]] = []
        for point in points_raw:
            if not isinstance(point, list | tuple) or len(point) != 2:
                raise SirInvalid("sir_bad_entity")
            points.append((_finite(point[0], limits), _finite(point[1], limits)))
        entity = SirEntity(
            kind=item["kind"],
            ref=str(item.get("ref", ""))[:128],
            layer=str(item.get("layer", ""))[: limits.max_text_length],
            points=points,
            text=text,
            name=None if item.get("name") is None else str(item["name"])[: limits.max_text_length],
        )
        for key in ("cx", "cy", "width", "height", "radius", "text_height"):
            if item.get(key) is not None:
                setattr(entity, key, _finite(item[key], limits))
        entity.rotation_deg = _finite(item.get("rotation_deg", 0.0), limits)
        if entity.kind == "rect" and (entity.cx is None or entity.cy is None or not entity.width or not entity.height):
            raise SirInvalid("sir_bad_entity")
        if entity.kind in ("rect", "circle", "text") and (entity.cx is None or entity.cy is None):
            raise SirInvalid("sir_bad_entity")
        if entity.kind in ("polygon", "polyline", "line") and len(points) < 2:
            raise SirInvalid("sir_bad_entity")
        entities.append(entity)
    return SirDocument(
        source_format=str(raw.get("source_format", ""))[:8],
        parser_name=str(raw.get("parser_name", ""))[:64],
        parser_version=str(raw.get("parser_version", ""))[:32],
        source_units=units,
        units_trusted=bool(raw.get("units_trusted", False)),
        y_axis=raw["y_axis"],
        entities=entities,
        layers=layer_names,
        warnings=clean_warnings,
        objects_discovered=int(raw.get("objects_discovered", 0)),
        unsupported_object_count=int(raw.get("unsupported_object_count", 0)),
        notes=dict(raw.get("notes", {})) if isinstance(raw.get("notes", {}), dict) else {},
    )
