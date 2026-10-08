# ruff: noqa: E501
"""Match calibrated rack candidates against existing authoritative racks and already-accepted geometry.
Pure functions over plain data (the service layer loads rows); the result is advisory evidence stored on the
candidate. Nothing here moves or creates a rack. Evidence deliberately does not rely on asset-tag text alone:
position, footprint and orientation outweigh the label, so a re-export with renamed labels still reconciles
and a mislabelled shape in the wrong place is flagged as a conflict rather than silently trusted."""

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.application.spatial_import.geometry import center_distance, rect_corners, rect_iou

POSITION_FULL_MM = 100.0
POSITION_ZERO_MM = 500.0
MATCH_THRESHOLD = 0.70
AMBIGUOUS_THRESHOLD = 0.40
MATCH_MARGIN = 0.20
DUPLICATE_IOU = 0.60
_NORMALISE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RackRef:
    """An existing authoritative rack with a current placement in the room."""

    rack_id: str
    name: str
    asset_tag: str
    x_mm: float
    y_mm: float
    width_mm: float
    depth_mm: float
    rotation_deg: float


@dataclass(frozen=True)
class ExistingObject:
    """An already-accepted rack-like SpatialObject on the floor plan."""

    object_id: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float
    label: str | None


@dataclass(frozen=True)
class CandidateFootprint:
    candidate_id: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float
    label: str | None
    source_ref: str


@dataclass
class MatchResult:
    status: str  # matched | ambiguous | conflict | duplicate | unmatched
    matched_rack_id: str | None = None
    score: float = 0.0
    duplicate_of_object_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


def _norm(text: str | None) -> str:
    return _NORMALISE.sub("", (text or "").lower())


def _position_score(distance: float) -> float:
    if distance <= POSITION_FULL_MM:
        return 1.0
    if distance >= POSITION_ZERO_MM:
        return 0.0
    return 1.0 - (distance - POSITION_FULL_MM) / (POSITION_ZERO_MM - POSITION_FULL_MM)


def _dimension_score(c: CandidateFootprint, rack: RackRef) -> float:
    direct = (abs(c.width_mm - rack.width_mm) / rack.width_mm + abs(c.height_mm - rack.depth_mm) / rack.depth_mm) / 2
    swapped = (abs(c.width_mm - rack.depth_mm) / rack.depth_mm + abs(c.height_mm - rack.width_mm) / rack.width_mm) / 2
    return max(0.0, 1.0 - min(direct, swapped) * 3)


def _orientation_score(c: CandidateFootprint, rack: RackRef) -> float:
    diff = abs(((c.rotation_deg - rack.rotation_deg) + 90) % 180 - 90)  # 0..90, symmetric under 180
    return 1.0 if diff <= 10 else (0.5 if abs(diff - 90) <= 10 else 0.0)


def _label_score(c: CandidateFootprint, rack: RackRef) -> float:
    label = _norm(c.label)
    if not label:
        return 0.0
    names = {_norm(rack.name), _norm(rack.asset_tag)} - {""}
    if label in names:
        return 1.0
    return 0.5 if any(label in n or n in label for n in names) else 0.0


def reconcile_candidate(
    candidate: CandidateFootprint, racks: list[RackRef], existing: list[ExistingObject], seen_refs: dict[str, str] | None = None
) -> MatchResult:
    """`seen_refs` maps source_ref -> accepted SpatialObject id for refs accepted from earlier jobs on this
    floor plan (a stable re-export keeps its handles)."""
    corners = rect_corners(candidate.x_mm, candidate.y_mm, candidate.width_mm, candidate.height_mm, candidate.rotation_deg)
    evidence: list[dict[str, Any]] = []

    # 1) already-accepted geometry: a re-export proposing the same rack again is a duplicate, not a new rack
    best_dup: tuple[float, ExistingObject] | None = None
    for obj in existing:
        iou = rect_iou(corners, rect_corners(obj.x_mm, obj.y_mm, obj.width_mm, obj.height_mm, obj.rotation_deg))
        if best_dup is None or iou > best_dup[0]:
            best_dup = (iou, obj)
    ref_hit = (seen_refs or {}).get(candidate.source_ref)
    if best_dup is not None and best_dup[0] >= DUPLICATE_IOU:
        evidence.append({"code": "overlaps_accepted_geometry", "score": round(best_dup[0], 3), "detail": f"{best_dup[0]:.0%} overlap with an accepted shape"})
        if ref_hit:
            evidence.append({"code": "source_ref_seen", "score": 1.0, "detail": "Same source reference was accepted earlier"})
        return MatchResult(status="duplicate", duplicate_of_object_id=best_dup[1].object_id, score=best_dup[0], evidence=evidence)

    # 2) existing authoritative racks, ranked by position + footprint + orientation (+ label)
    ranked: list[tuple[float, RackRef, dict[str, float]]] = []
    for rack in racks:
        if rack.width_mm <= 0 or rack.depth_mm <= 0:
            continue
        rack_corners = rect_corners(rack.x_mm, rack.y_mm, rack.width_mm, rack.depth_mm, rack.rotation_deg)
        distance = center_distance(corners, rack_corners)
        parts = {
            "position": _position_score(distance),
            "footprint": _dimension_score(candidate, rack),
            "orientation": _orientation_score(candidate, rack),
            "label": _label_score(candidate, rack),
        }
        total = 0.45 * parts["position"] + 0.25 * parts["footprint"] + 0.10 * parts["orientation"] + 0.20 * parts["label"]
        parts["distance_mm"] = distance
        ranked.append((total, rack, parts))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < AMBIGUOUS_THRESHOLD:
        return MatchResult(status="unmatched", evidence=evidence)

    best_score, best, parts = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    evidence += [
        {"code": "position", "score": round(parts["position"], 3), "detail": f"{parts['distance_mm']:.0f} mm from rack '{best.name}'"},
        {"code": "footprint", "score": round(parts["footprint"], 3), "detail": f"{candidate.width_mm:.0f} x {candidate.height_mm:.0f} mm vs {best.width_mm:.0f} x {best.depth_mm:.0f} mm"},
        {"code": "orientation", "score": round(parts["orientation"], 3), "detail": "Orientation compared modulo 180 degrees"},
        {"code": "label", "score": round(parts["label"], 3), "detail": f"Label '{(candidate.label or '')[:40]}' compared with name and asset tag"},
    ]
    # conflict: the label names a different rack than the one the geometry sits on, or the labelled rack is elsewhere
    labelled = [r for _, r, p in ranked if p["label"] >= 1.0]
    if labelled and labelled[0].rack_id != best.rack_id and parts["position"] >= 0.5:
        evidence.append({"code": "label_conflicts_with_position", "score": 1.0, "detail": f"Label names '{labelled[0].name}' but the shape sits on '{best.name}'"})
        return MatchResult(status="conflict", matched_rack_id=best.rack_id, score=best_score, evidence=evidence)
    if labelled and labelled[0].rack_id == best.rack_id and parts["position"] < 0.5:
        evidence.append({"code": "label_matches_but_moved", "score": 1.0, "detail": f"Label matches '{best.name}' but the shape is {parts['distance_mm']:.0f} mm away from its recorded position"})
        return MatchResult(status="conflict", matched_rack_id=best.rack_id, score=best_score, evidence=evidence)
    if best_score >= MATCH_THRESHOLD and parts["position"] >= 0.5 and best_score - second >= MATCH_MARGIN:
        return MatchResult(status="matched", matched_rack_id=best.rack_id, score=best_score, evidence=evidence)
    if math.isfinite(best_score):
        evidence.append({"code": "close_alternatives" if second >= AMBIGUOUS_THRESHOLD else "weak_match", "score": round(second, 3), "detail": "No single rack clearly matches"})
    return MatchResult(status="ambiguous", matched_rack_id=best.rack_id, score=best_score, evidence=evidence)


def resolve_shared_matches(results: dict[str, MatchResult]) -> None:
    """Two candidates claiming the same rack cannot both be right: flag both as conflicts."""
    claims: dict[str, list[str]] = {}
    for cid, result in results.items():
        if result.status == "matched" and result.matched_rack_id:
            claims.setdefault(result.matched_rack_id, []).append(cid)
    for cids in claims.values():
        if len(cids) > 1:
            for cid in cids:
                results[cid].status = "conflict"
                results[cid].evidence.append({"code": "rack_claimed_twice", "score": 1.0, "detail": f"{len(cids)} candidates match the same rack"})
