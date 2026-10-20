# ruff: noqa: E501
"""SIR -> import candidates, with explicit, inspectable evidence for every suggestion. Nothing here decides
anything: a suggestion is a hint shown to an operator, never an inventory write (§21 of the Phase 2 prompt).
Classification works in source units (the drawing may not be calibrated yet); physical-plausibility evidence
is added only when the file declares trustworthy units. Matching against existing authoritative racks happens
later, in reconcile.py, once a calibration exists."""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.application.spatial_import.sir import UNIT_TO_MM, SirDocument, SirEntity

MAX_CANDIDATES = 5_000
RACK_SUGGESTION_THRESHOLD = 0.35
HIGH_CONFIDENCE = 0.7
MAX_CONFIDENCE = 0.95  # detection never reaches certainty: a human always decides

_RACK_LAYER = re.compile(r"rack|cabinet|cab\b", re.IGNORECASE)
_WALL_LAYER = re.compile(r"wall|partition|boundary|outline", re.IGNORECASE)
_COLUMN_LAYER = re.compile(r"column|pillar|post", re.IGNORECASE)
_RACK_LABEL = re.compile(r"^(?:rack|rk|cab|cabinet|r)[\s_\-]?[A-Za-z]?\d+[A-Za-z0-9\-_.]*$|^[A-Za-z]{1,3}[\s_\-]?\d{1,3}[\s_\-]\d{1,3}$", re.IGNORECASE)
# typical rack footprint envelope (mm) used only when the file declares trustworthy units
_FOOTPRINT_SHORT = (450.0, 1000.0)
_FOOTPRINT_LONG = (600.0, 1500.0)


@dataclass
class CandidateDraft:
    shape_type: str
    raw_geometry: dict[str, Any]
    suggested_object_type: str | None
    suggested_label: str | None
    confidence: float | None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_ref: str = ""


@dataclass
class ClassificationResult:
    drafts: list[CandidateDraft]
    racks_detected: int
    ambiguous_count: int
    confirmed_count: int
    warnings: list[str]


def _point_in_rect(px: float, py: float, e: SirEntity, margin: float = 0.0) -> bool:
    assert e.cx is not None and e.cy is not None and e.width is not None and e.height is not None
    rad = math.radians(-e.rotation_deg)
    dx, dy = px - e.cx, py - e.cy
    lx = dx * math.cos(rad) - dy * math.sin(rad)
    ly = dx * math.sin(rad) + dy * math.cos(rad)
    return abs(lx) <= e.width / 2 + margin and abs(ly) <= e.height / 2 + margin


def _geometry(e: SirEntity) -> dict[str, Any]:
    base: dict[str, Any] = {"shape_type": e.kind, "layer": e.layer, "source_ref": e.ref}
    if e.kind == "rect":
        assert e.cx is not None and e.cy is not None and e.width is not None and e.height is not None
        base.update(
            cx=e.cx, cy=e.cy, width=e.width, height=e.height, rotation_deg=e.rotation_deg,
            x=e.cx - e.width / 2, y=e.cy - e.height / 2, text=e.text,
        )
    elif e.kind == "circle":
        base.update(cx=e.cx, cy=e.cy, x=e.cx, y=e.cy, radius=e.radius)
    elif e.kind == "text":
        base.update(cx=e.cx, cy=e.cy, x=e.cx, y=e.cy, text=e.text)
    else:
        xs = [p[0] for p in e.points]
        ys = [p[1] for p in e.points]
        base.update(points=[list(p) for p in e.points], x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys))
    return base


def _cluster_key(width: float, height: float) -> tuple[int, int]:
    short, long_ = sorted((width, height))
    step = math.log(1.03)
    return round(math.log(max(short, 1e-9)) / step), round(math.log(max(long_, 1e-9)) / step)


def classify(document: SirDocument) -> ClassificationResult:
    warnings: list[str] = []
    entities = document.entities
    rects = [e for e in entities if e.kind == "rect"]
    texts = [e for e in entities if e.kind == "text" and e.text]
    if len(rects) > MAX_CANDIDATES:
        warnings.append(f"{len(rects)} rectangles found; only the first {MAX_CANDIDATES} are classified")
        rects = rects[:MAX_CANDIDATES]
    if len(texts) > MAX_CANDIDATES:
        texts = texts[:MAX_CANDIDATES]
    to_mm = UNIT_TO_MM.get(document.source_units) if document.units_trusted else None

    # --- footprint clusters: repeated, similarly-sized rectangles are the strongest cheap rack signal
    clusters = Counter(_cluster_key(r.width or 0, r.height or 0) for r in rects if r.width and r.height)
    dominant_key, dominant_count = (clusters.most_common(1)[0] if clusters else (None, 0))

    # --- label association (nearest text inside, else adjacent; each text labels at most one rectangle).
    # A uniform grid keeps this near-linear: a hostile drawing with thousands of rectangles and labels must not
    # cost O(rects x texts).
    label_for: dict[str, tuple[str, str]] = {}  # rect ref -> (text, "inside"|"adjacent")
    consumed: set[str] = set()
    radii = sorted(math.hypot(r.width or 0, r.height or 0) / 2 + 0.5 * max(r.width or 0, r.height or 0) for r in rects)
    cell = max(radii[len(radii) // 2] * 2, 1e-6) if radii else 1.0
    grid: dict[tuple[int, int], list[SirEntity]] = {}
    big: list[SirEntity] = []
    for r in rects:
        assert r.cx is not None and r.cy is not None
        reach = math.hypot(r.width or 0, r.height or 0) / 2 + 0.5 * max(r.width or 0, r.height or 0)
        span = int(reach // cell) + 1
        if span > 12:
            big.append(r)
            continue
        gx, gy = int(r.cx // cell), int(r.cy // cell)
        for ix in range(gx - span, gx + span + 1):
            for iy in range(gy - span, gy + span + 1):
                grid.setdefault((ix, iy), []).append(r)
    for t in texts:
        assert t.cx is not None and t.cy is not None
        best: tuple[float, SirEntity, str] | None = None
        for r in [*grid.get((int(t.cx // cell), int(t.cy // cell)), []), *big]:
            assert r.cx is not None and r.cy is not None and r.width and r.height
            if _point_in_rect(t.cx, t.cy, r):
                d, how = math.hypot(t.cx - r.cx, t.cy - r.cy), "inside"
            elif _point_in_rect(t.cx, t.cy, r, margin=0.5 * max(r.width, r.height)):
                d, how = math.hypot(t.cx - r.cx, t.cy - r.cy) + 1e6, "adjacent"  # inside beats adjacent
            else:
                continue
            if best is None or d < best[0]:
                best = (d, r, how)
        if best is not None and best[1].ref not in label_for:
            label_for[best[1].ref] = (t.text or "", best[2])
            consumed.add(t.ref)

    # --- container / room outline: a rectangle holding most of the others
    outline_ref: str | None = None
    if len(rects) >= 3:
        biggest = max(rects, key=lambda r: (r.width or 0) * (r.height or 0))
        others = [r for r in rects if r.ref != biggest.ref]
        inside = sum(1 for r in others if r.cx is not None and r.cy is not None and _point_in_rect(r.cx, r.cy, biggest))
        if others and inside / len(others) >= 0.6 and (biggest.width or 0) * (biggest.height or 0) >= 3 * max(
            (o.width or 0) * (o.height or 0) for o in others
        ):
            outline_ref = biggest.ref

    drafts: list[CandidateDraft] = []
    racks = ambiguous = confirmed = 0

    for r in rects:
        assert r.width and r.height
        evidence: list[dict[str, Any]] = []
        score = 0.0
        suggested: str | None = None
        label, label_kind = label_for.get(r.ref, (None, None))
        label = label or r.text
        if r.ref == outline_ref:
            suggested, score = "room_outline", 0.5
            evidence.append({"code": "contains_most_shapes", "weight": 0.5, "detail": "Encloses most other rectangles in the drawing"})
        else:
            aspect = min(r.width, r.height) / max(r.width, r.height)
            if 0.3 <= aspect <= 0.9:
                score += 0.35
                evidence.append({"code": "rack_like_aspect", "weight": 0.35, "detail": f"Aspect ratio {aspect:.2f} matches a rack footprint"})
            if _RACK_LAYER.search(r.layer or "") or (r.name and _RACK_LAYER.search(r.name)):
                score += 0.30
                evidence.append({"code": "layer_name", "weight": 0.30, "detail": f"Layer/shape name '{(r.name or r.layer)[:40]}' suggests a rack"})
            if label and _RACK_LABEL.match(label.strip()):
                w = 0.25 if label_kind != "adjacent" else 0.15
                score += w
                evidence.append({"code": "label_pattern", "weight": w, "detail": f"Label '{label[:40]}' follows a rack naming pattern"})
            if dominant_key is not None and dominant_count >= 3 and _cluster_key(r.width, r.height) == dominant_key and aspect < 0.95:
                score += 0.20
                evidence.append({"code": "repeated_footprint", "weight": 0.20, "detail": f"One of {dominant_count} rectangles with the same footprint"})
            if to_mm is not None:
                short, long_ = sorted((r.width * to_mm, r.height * to_mm))
                if _FOOTPRINT_SHORT[0] <= short <= _FOOTPRINT_SHORT[1] and _FOOTPRINT_LONG[0] <= long_ <= _FOOTPRINT_LONG[1]:
                    score += 0.25
                    evidence.append({"code": "footprint_plausible", "weight": 0.25, "detail": f"{short:.0f} x {long_:.0f} mm is a typical rack footprint"})
                elif long_ > 3000 or short < 200:
                    score -= 0.30
                    evidence.append({"code": "footprint_implausible", "weight": -0.30, "detail": f"{short:.0f} x {long_:.0f} mm is not a rack footprint"})
            score = max(0.0, min(MAX_CONFIDENCE, score))
            if _COLUMN_LAYER.search(r.layer or ""):
                suggested, score = "column", 0.5
                evidence = [{"code": "layer_name", "weight": 0.5, "detail": f"Layer '{r.layer[:40]}' suggests a column"}]
            elif score >= RACK_SUGGESTION_THRESHOLD:
                suggested = "rack"
        if label_kind is not None and label:
            evidence.append({"code": f"label_{label_kind}", "weight": 0.0, "detail": f"Label '{label[:40]}' taken from text {label_kind} the shape"})
        if suggested == "rack":
            racks += 1
        if suggested is None or score < HIGH_CONFIDENCE:
            ambiguous += 1
        else:
            confirmed += 1
        drafts.append(
            CandidateDraft(
                shape_type="rect", raw_geometry=_geometry(r), suggested_object_type=suggested, suggested_label=label,
                confidence=round(score, 3) if suggested else None, evidence=evidence, source_ref=r.ref,
            )
        )

    for e in entities:
        if e.kind == "rect" or (e.kind == "text" and (e.ref in consumed or not e.text)):
            continue
        if len(drafts) >= MAX_CANDIDATES:
            warnings.append(f"candidate limit of {MAX_CANDIDATES} reached; remaining shapes were not turned into candidates")
            break
        suggested = None
        confidence = None
        evidence = []
        if e.kind == "text":
            suggested, confidence = "annotation", 0.3
            evidence = [{"code": "free_text", "weight": 0.3, "detail": "Text not attached to a rectangle"}]
        elif e.kind in ("line", "polyline", "polygon") and _WALL_LAYER.search(e.layer or ""):
            suggested, confidence = "wall", 0.4
            evidence = [{"code": "layer_name", "weight": 0.4, "detail": f"Layer '{e.layer[:40]}' suggests a wall"}]
        elif e.kind == "circle" and _COLUMN_LAYER.search(e.layer or ""):
            suggested, confidence = "column", 0.4
            evidence = [{"code": "layer_name", "weight": 0.4, "detail": f"Layer '{e.layer[:40]}' suggests a column"}]
        elif e.kind in ("line", "polyline"):
            continue  # unclassified linework is context, not a reviewable candidate
        if suggested is None or (confidence or 0) < HIGH_CONFIDENCE:
            ambiguous += 1
        drafts.append(
            CandidateDraft(
                shape_type=e.kind, raw_geometry=_geometry(e), suggested_object_type=suggested,
                suggested_label=e.text if e.kind == "text" else None, confidence=confidence, evidence=evidence, source_ref=e.ref,
            )
        )
    return ClassificationResult(drafts=drafts, racks_detected=racks, ambiguous_count=ambiguous, confirmed_count=confirmed, warnings=warnings)
