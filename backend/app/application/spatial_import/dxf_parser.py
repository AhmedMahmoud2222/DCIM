# ruff: noqa: E501
"""ASCII DXF -> SIR. Pure Python, no third-party DXF library, and it only ever runs inside the sandboxed
child (see worker.py). Supported subset, chosen for data-centre floor plans:

  LINE, LWPOLYLINE, POLYLINE/VERTEX/SEQEND (2D), CIRCLE, TEXT, MTEXT, INSERT (block flattening with
  translation, uniform/non-uniform scale and rotation), layers (LAYER table and entity layer), $INSUNITS.

Everything else (SPLINE, HATCH, DIMENSION, ARC, 3D entities, XREF, images, OLE, ...) is counted as
unsupported with an explicit warning; arcs/bulges in polylines are approximated by their chord and flagged.
Bounded: group-pair count, entity count, expansion budget, block recursion depth, coordinate magnitude, text
length, layer count, points per entity."""

import math
import re
from dataclasses import dataclass, field

from app.application.spatial_import.affine import Matrix
from app.application.spatial_import.limits import DEFAULT_LIMITS, ParserLimits
from app.application.spatial_import.sir import SirDocument, SirEntity

PARSER_NAME = "dxf_ascii_subset"
PARSER_VERSION = "1"

_INSUNITS = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
_UNSUPPORTED_UNIT_CODES = {3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}
_MTEXT_FORMAT = re.compile(r"\\[A-Za-z][^;\\]*;|\\[PpXx~]|[{}]")
_PI = math.pi


class DxfRejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _Block:
    name: str
    base: tuple[float, float] = (0.0, 0.0)
    entities: list[dict] = field(default_factory=list)


def _decode(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _pairs(text: str, limits: ParserLimits):
    lines = text.splitlines()
    if len(lines) > limits.max_text_lines:
        raise DxfRejected("dxf_too_large", "too many lines")
    if len(lines) % 2:
        lines.append("")  # tolerate a trailing blank; EOF check below catches real truncation
    for i in range(0, len(lines), 2):
        code_text = lines[i].strip()
        try:
            code = int(code_text)
        except ValueError:
            raise DxfRejected("dxf_malformed", "group code is not an integer") from None
        yield code, lines[i + 1].strip() if code not in (1, 3) else lines[i + 1].rstrip("\r\n")


def _num(value: str, limits: ParserLimits) -> float:
    try:
        number = float(value)
    except ValueError:
        raise DxfRejected("dxf_malformed", "invalid number") from None
    if not math.isfinite(number) or abs(number) > limits.max_coordinate:
        raise DxfRejected("dxf_coordinate_out_of_range", "coordinate outside the permitted range")
    return number


def _read_sections(text: str, limits: ParserLimits):
    """Yields (section_name, list[(code, value)]) groups; raises on structural damage."""
    section: str | None = None
    items: list[tuple[int, str]] = []
    saw_eof = False
    pair_budget = limits.max_text_lines // 2
    count = 0
    pending_name = False
    for code, value in _pairs(text, limits):
        count += 1
        if count > pair_budget:
            raise DxfRejected("dxf_too_large", "too many group pairs")
        if saw_eof:
            if value or code != 0:
                continue  # trailing padding after EOF is ignored
            continue
        if section is None:
            if code == 999:
                continue
            if code == 0 and value == "SECTION":
                pending_name = True
                continue
            if pending_name and code == 2:
                section = value
                items = []
                pending_name = False
                continue
            if code == 0 and value == "EOF":
                saw_eof = True
                continue
            raise DxfRejected("dxf_malformed", "unexpected content outside a SECTION")
        if code == 0 and value == "ENDSEC":
            yield section, items
            section = None
            continue
        items.append((code, value))
    if section is not None:
        raise DxfRejected("dxf_truncated", "section not closed")
    if not saw_eof:
        raise DxfRejected("dxf_truncated", "missing EOF marker")


def _split_entities(items: list[tuple[int, str]], limits: ParserLimits) -> list[dict]:
    """Group a flat (code, value) stream into entity dicts: {'type':..., 'c': {code: [values]}}.
    BLOCK/ENDBLK are returned as entities too so the caller can build block tables."""
    entities: list[dict] = []
    current: dict | None = None
    for code, value in items:
        if code == 0:
            if current is not None:
                entities.append(current)
                if len(entities) > limits.max_expanded_entities:
                    raise DxfRejected("dxf_too_many_entities", "entity budget exceeded")
            current = {"type": value, "c": {}}
        elif current is not None:
            current["c"].setdefault(code, []).append(value)
    if current is not None:
        entities.append(current)
    return entities


def _first(entity: dict, code: int, default: str | None = None) -> str | None:
    values = entity["c"].get(code)
    return values[0] if values else default


def _fnum(entity: dict, code: int, limits: ParserLimits, default: float = 0.0) -> float:
    raw = _first(entity, code)
    return default if raw is None else _num(raw, limits)


def _clean_text(raw: str, limits: ParserLimits) -> str | None:
    cleaned = _MTEXT_FORMAT.sub(" ", raw).replace("\\~", " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if len(cleaned) > limits.max_text_length:
        raise DxfRejected("dxf_text_too_long", "text exceeds the maximum length")
    return cleaned


def parse_dxf(content: bytes, *, limits: ParserLimits = DEFAULT_LIMITS) -> SirDocument:
    if len(content) > limits.max_input_bytes:
        raise DxfRejected("dxf_too_large", "file exceeds the DXF size limit")
    text = _decode(content)
    sections = {}
    for name, items in _read_sections(text, limits):
        if name in sections:
            raise DxfRejected("dxf_malformed", f"duplicate section {name}")
        sections[name] = items
    if "ENTITIES" not in sections:
        raise DxfRejected("dxf_malformed", "missing ENTITIES section")

    warnings: list[dict[str, str]] = []
    unsupported_types: dict[str, int] = {}

    def warn(code: str, detail: str) -> None:
        if len(warnings) < limits.max_warnings and not any(w["code"] == code and w["detail"] == detail for w in warnings):
            warnings.append({"code": code, "detail": detail})

    # --- units
    units = "unitless"
    units_trusted = False
    header = sections.get("HEADER", [])
    for i, (code, value) in enumerate(header):
        if code == 9 and value == "$INSUNITS" and i + 1 < len(header) and header[i + 1][0] == 70:
            try:
                unit_code = int(header[i + 1][1])
            except ValueError:
                raise DxfRejected("dxf_invalid_units", "$INSUNITS is not an integer") from None
            if unit_code in _INSUNITS:
                units = _INSUNITS[unit_code]
                units_trusted = unit_code != 0
            elif unit_code in _UNSUPPORTED_UNIT_CODES:
                warn("dxf_unit_unsupported", f"$INSUNITS {unit_code} is not a supported length unit; set the scale manually")
            else:
                raise DxfRejected("dxf_invalid_units", f"$INSUNITS {unit_code} is not a valid value")
    if units == "unitless":
        warn("dxf_units_undeclared", "The drawing declares no units; calibrate the scale manually")

    # --- layers
    layers: list[str] = []
    for ent in _split_entities(sections.get("TABLES", []), limits):
        if ent["type"] == "LAYER":
            name = _first(ent, 2)
            if name is not None and name not in layers:
                layers.append(name[: limits.max_text_length])
                if len(layers) > limits.max_layers:
                    raise DxfRejected("dxf_too_many_layers", "layer count exceeds the limit")

    # --- blocks
    blocks: dict[str, _Block] = {}
    current_block: _Block | None = None
    for ent in _split_entities(sections.get("BLOCKS", []), limits):
        if ent["type"] == "BLOCK":
            name = _first(ent, 2) or ""
            current_block = _Block(name=name, base=(_fnum(ent, 10, limits), _fnum(ent, 20, limits)))
            blocks[name] = current_block
        elif ent["type"] == "ENDBLK":
            current_block = None
        elif current_block is not None:
            current_block.entities.append(ent)

    # --- entities (iterative expansion; the recursion bound is an explicit stack depth)
    out: list[SirEntity] = []
    visited = 0
    discovered = 0
    unsupported = 0

    def emit(entity: SirEntity) -> None:
        if len(out) >= limits.max_entities:
            raise DxfRejected("dxf_too_many_entities", "entity count exceeds the limit")
        out.append(entity)

    def entity_layer(ent: dict) -> str:
        layer = (_first(ent, 8) or "0")[: limits.max_text_length]
        if layer not in layers:
            if len(layers) >= limits.max_layers:
                raise DxfRejected("dxf_too_many_layers", "layer count exceeds the limit")
            layers.append(layer)
        return layer

    def fold_polyline(points: list[tuple[float, float]], closed: bool, layer: str, ref: str, name: str | None) -> SirEntity | None:
        if len(points) < 2:
            return None
        if len(points) > limits.max_points_per_entity:
            raise DxfRejected("dxf_too_many_points", "polyline exceeds the point limit")
        if closed and len(points) >= 3 and points[0] == points[-1]:
            points = points[:-1]
        rect = _as_rect(points) if closed else None
        if rect is not None:
            cx, cy, w, h, rot = rect
            return SirEntity(kind="rect", ref=ref, layer=layer, cx=cx, cy=cy, width=w, height=h, rotation_deg=rot, name=name)
        if closed and len(points) >= 3:
            return SirEntity(kind="polygon", ref=ref, layer=layer, points=points, name=name)
        return SirEntity(kind="polyline", ref=ref, layer=layer, points=points, name=name)

    # work stack of (entity_dict, matrix, depth, ref_prefix, active_block_names)
    stack: list[tuple[dict, Matrix, int, str, tuple[str, ...]]] = [
        (e, Matrix(), 0, "", ()) for e in reversed(_split_entities(sections["ENTITIES"], limits))
    ]
    polyline_acc: dict | None = None
    while stack:
        ent, matrix, depth, prefix, active = stack.pop()
        visited += 1
        if visited > limits.max_expanded_entities:
            raise DxfRejected("dxf_expansion_limit", "expanded geometry exceeds the limit")
        kind = ent["type"]
        handle = _first(ent, 5) or str(visited)
        ref = f"dxf:{prefix}{handle}"
        layer = entity_layer(ent) if kind not in ("VERTEX", "SEQEND") else ""

        # classic POLYLINE: collect following VERTEX records until SEQEND
        if kind == "POLYLINE":
            polyline_acc = {"ent": ent, "matrix": matrix, "prefix": prefix, "ref": ref, "layer": layer, "points": []}
            continue
        if kind == "VERTEX" and polyline_acc is not None:
            x, y = matrix.apply(_fnum(ent, 10, limits), _fnum(ent, 20, limits))
            polyline_acc["points"].append((x, y))
            continue
        if kind == "SEQEND" and polyline_acc is not None:
            acc, polyline_acc = polyline_acc, None
            discovered += 1
            closed = bool(int(_first(acc["ent"], 70, "0") or 0) & 1)
            shape = fold_polyline(acc["points"], closed, acc["layer"], acc["ref"], None)
            if shape is not None:
                emit(shape)
            continue

        if kind == "LINE":
            discovered += 1
            p1 = matrix.apply(_fnum(ent, 10, limits), _fnum(ent, 20, limits))
            p2 = matrix.apply(_fnum(ent, 11, limits), _fnum(ent, 21, limits))
            emit(SirEntity(kind="line", ref=ref, layer=layer, points=[p1, p2]))
        elif kind == "LWPOLYLINE":
            discovered += 1
            xs, ys = ent["c"].get(10, []), ent["c"].get(20, [])
            if len(xs) != len(ys):
                raise DxfRejected("dxf_malformed", "LWPOLYLINE vertex arrays differ in length")
            if len(xs) > limits.max_points_per_entity:
                raise DxfRejected("dxf_too_many_points", "polyline exceeds the point limit")
            if ent["c"].get(42) and any(abs(float(b)) > 1e-9 for b in ent["c"][42] if _isnum(b)):
                warn("dxf_bulge_approximated", "Arc segments (bulge) are approximated by straight chords")
            pts = [matrix.apply(_num(x, limits), _num(y, limits)) for x, y in zip(xs, ys, strict=True)]
            closed = bool(int(_first(ent, 70, "0") or 0) & 1)
            shape = fold_polyline(pts, closed, layer, ref, None)
            if shape is not None:
                emit(shape)
        elif kind == "CIRCLE":
            discovered += 1
            cx, cy = matrix.apply(_fnum(ent, 10, limits), _fnum(ent, 20, limits))
            sx, sy = matrix.scale_xy()
            emit(SirEntity(kind="circle", ref=ref, layer=layer, cx=cx, cy=cy, radius=abs(_fnum(ent, 40, limits)) * (sx + sy) / 2))
        elif kind in ("TEXT", "MTEXT"):
            discovered += 1
            raw = "".join(ent["c"].get(3, [])) + (_first(ent, 1) or "") if kind == "MTEXT" else (_first(ent, 1) or "")
            label = _clean_text(raw, limits)
            if label is not None:
                cx, cy = matrix.apply(_fnum(ent, 10, limits), _fnum(ent, 20, limits))
                emit(
                    SirEntity(
                        kind="text", ref=ref, layer=layer, cx=cx, cy=cy, text=label,
                        text_height=_fnum(ent, 40, limits) * matrix.scale_xy()[1], rotation_deg=matrix.rotation_deg(),
                    )
                )
        elif kind == "INSERT":
            discovered += 1
            name = _first(ent, 2) or ""
            block = blocks.get(name)
            if block is None:
                warn("dxf_unknown_block", f"INSERT references undefined block '{name[:60]}'")
                unsupported += 1
                continue
            if name in active:
                raise DxfRejected("dxf_recursive_block", f"block '{name[:60]}' references itself")
            if depth + 1 > limits.max_block_depth:
                raise DxfRejected("dxf_block_depth", "block nesting exceeds the depth limit")
            if int(_first(ent, 70, "1") or 1) > 1 or int(_first(ent, 71, "1") or 1) > 1:
                warn("dxf_insert_array", "Array INSERTs are placed once; the repeat grid is not expanded")
            sx = _fnum(ent, 41, limits, 1.0)
            sy = _fnum(ent, 42, limits, sx if _first(ent, 42) is None else 1.0)
            rot = math.radians(_fnum(ent, 50, limits))
            cos, sin = math.cos(rot), math.sin(rot)
            bx, by = block.base
            local = Matrix(a=cos * sx, b=sin * sx, c=-sin * sy, d=cos * sy)
            local.e = _fnum(ent, 10, limits) - (local.a * bx + local.c * by)
            local.f = _fnum(ent, 20, limits) - (local.b * bx + local.d * by)
            combined = local.then(matrix)
            for child in reversed(block.entities):
                stack.append((child, combined, depth + 1, f"{prefix}{handle}/", (*active, name)))
        elif kind in ("VERTEX", "SEQEND", "ATTRIB", "ATTDEF", "VIEWPORT"):
            continue
        else:
            unsupported += 1
            unsupported_types[kind] = unsupported_types.get(kind, 0) + 1

    for kind, count in sorted(unsupported_types.items()):
        warn("dxf_unsupported_entity", f"{count} {kind} entit{'y' if count == 1 else 'ies'} ignored")

    return SirDocument(
        source_format="dxf", parser_name=PARSER_NAME, parser_version=PARSER_VERSION, source_units=units,
        units_trusted=units_trusted, y_axis="up", entities=out, layers=layers, warnings=warnings,
        objects_discovered=discovered, unsupported_object_count=unsupported,
        notes={"insunits_declared": units_trusted},
    )


def _isnum(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _as_rect(points: list[tuple[float, float]]) -> tuple[float, float, float, float, float] | None:
    """(cx, cy, width, height, rotation_deg) when the 4 points form a rectangle (any rotation), else None.
    Rotation is normalised to (-90, 90]; an edge pair at exactly 90 degrees is reported axis-aligned with
    width and height swapped, so the same physical rectangle always yields the same representation."""
    if len(points) != 4:
        return None
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    e1 = (x1 - x0, y1 - y0)
    e2 = (x2 - x1, y2 - y1)
    e3 = (x3 - x2, y3 - y2)
    e4 = (x0 - x3, y0 - y3)
    l1, l2, l3, l4 = (math.hypot(*e) for e in (e1, e2, e3, e4))
    longest = max(l1, l2, l3, l4)
    if min(l1, l2, l3, l4) <= 0:
        return None
    tol = 1e-4 * longest
    if abs(l1 - l3) > tol or abs(l2 - l4) > tol:
        return None
    if abs(e1[0] * e2[0] + e1[1] * e2[1]) > 1e-4 * l1 * l2:
        return None
    if abs(e1[0] + e3[0]) > tol or abs(e1[1] + e3[1]) > tol:
        return None
    width, height = l1, l2
    rot = math.degrees(math.atan2(e1[1], e1[0]))
    if rot > 90:
        rot -= 180
    elif rot <= -90:
        rot += 180
    if abs(rot - 90) < 1e-6:
        rot, width, height = 0.0, l2, l1
    if abs(rot) < 1e-6:
        rot = 0.0
    return (x0 + x2) / 2, (y0 + y2) / 2, width, height, rot
