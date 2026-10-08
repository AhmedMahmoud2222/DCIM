"""Rack detection evidence and reconciliation against existing authoritative racks (Issue #104)."""

import pytest

from app.application.spatial_import.classify import MAX_CONFIDENCE, classify
from app.application.spatial_import.dxf_parser import parse_dxf
from app.application.spatial_import.reconcile import (
    CandidateFootprint,
    ExistingObject,
    RackRef,
    reconcile_candidate,
    resolve_shared_matches,
)
from app.application.spatial_import.sir import SirDocument, SirEntity
from tests import spatial_fixtures as fx


def _by_ref(result):
    return {d.source_ref: d for d in result.drafts}


def test_rack_row_is_detected_with_explicit_evidence_and_labels_are_consumed():
    result = classify(parse_dxf(fx.rack_row_dxf(4)))
    racks = [d for d in result.drafts if d.suggested_object_type == "rack"]
    assert len(racks) == 4 and result.racks_detected == 4
    assert [d.suggested_label for d in racks] == ["RACK-01", "RACK-02", "RACK-03", "RACK-04"]
    codes = {e["code"] for e in racks[0].evidence}
    assert {"rack_like_aspect", "layer_name", "label_pattern", "repeated_footprint", "footprint_plausible"} <= codes
    assert all(0.7 <= d.confidence <= MAX_CONFIDENCE for d in racks)
    assert not any(d.shape_type == "text" for d in result.drafts), "labels attached to a rack are not separate candidates"


def test_room_outline_is_suggested_for_the_enclosing_rectangle():
    result = classify(parse_dxf(fx.rack_row_dxf(4)))
    outline = [d for d in result.drafts if d.suggested_object_type == "room_outline"]
    assert len(outline) == 1 and outline[0].raw_geometry["width"] == 6000


def test_confidence_never_reaches_certainty():
    assert max(d.confidence or 0 for d in classify(parse_dxf(fx.rack_row_dxf(8))).drafts) <= MAX_CONFIDENCE < 1


def test_a_lone_square_is_not_a_rack_and_an_implausible_footprint_is_penalised():
    square = SirDocument("dxf", "t", "1", "mm", True, "up", entities=[SirEntity(kind="rect", ref="s", cx=0, cy=0, width=500, height=500)])
    assert classify(square).drafts[0].suggested_object_type is None
    huge = SirDocument("dxf", "t", "1", "mm", True, "up", entities=[SirEntity(kind="rect", ref="h", cx=0, cy=0, width=800, height=5000, layer="RACKS")])
    draft = classify(huge).drafts[0]
    assert any(e["code"] == "footprint_implausible" for e in draft.evidence)


def test_untrusted_units_add_no_physical_plausibility_evidence():
    doc = parse_dxf(fx.dxf(fx.rect_poly("1", "R", 0, 0, 60, 100), insunits=None))
    assert not any(e["code"].startswith("footprint") for e in classify(doc).drafts[0].evidence)


def test_walls_columns_and_free_text_get_typed_suggestions():
    body = fx.line("1", "WALLS", 0, 0, 100, 0) + fx.line("2", "MISC", 0, 0, 5, 5) + fx.text("3", "NOTES", 50, 50, "UPS ROOM")
    result = classify(parse_dxf(fx.dxf(body)))
    types = sorted((d.shape_type, d.suggested_object_type) for d in result.drafts)
    assert types == [("line", "wall"), ("text", "annotation")]


def test_large_drawings_are_classified_without_quadratic_blowup():
    import time

    entities = [SirEntity(kind="rect", ref=f"r{i}", cx=(i % 100) * 800, cy=(i // 100) * 1200, width=600, height=1000) for i in range(3000)]
    entities += [SirEntity(kind="text", ref=f"t{i}", cx=(i % 100) * 800, cy=(i // 100) * 1200, text=f"R{i}") for i in range(3000)]
    doc = SirDocument("dxf", "t", "1", "mm", True, "up", entities=entities)
    started = time.monotonic()
    result = classify(doc)
    assert time.monotonic() - started < 10
    assert result.racks_detected >= 2900


# ------------------------------------------------------------------- reconcile
RACK_A = RackRef("rack-a", "Rack A", "RK-A", x_mm=1000, y_mm=1000, width_mm=600, depth_mm=1000, rotation_deg=0)
RACK_B = RackRef("rack-b", "Rack B", "RK-B", x_mm=1700, y_mm=1000, width_mm=600, depth_mm=1000, rotation_deg=0)


def _cand(x=1000, y=1000, w=600, h=1000, rot=0, label=None, ref="x", cid="c1"):
    return CandidateFootprint(cid, x, y, w, h, rot, label, ref)


def test_same_footprint_at_the_same_place_matches_without_any_label():
    result = reconcile_candidate(_cand(), [RACK_A, RACK_B], [])
    assert result.status == "matched" and result.matched_rack_id == "rack-a"
    assert {e["code"] for e in result.evidence} >= {"position", "footprint", "orientation", "label"}


def test_small_drift_and_a_rotated_equivalent_still_match_on_geometry_alone():
    assert reconcile_candidate(_cand(x=1120, y=1060), [RACK_A, RACK_B], []).status == "matched"
    assert reconcile_candidate(_cand(x=1300 - 500, y=1500 - 300, w=1000, h=600, rot=90), [RACK_A, RACK_B], []).matched_rack_id == "rack-a"


def test_renamed_label_does_not_break_matching_but_a_wrong_rack_label_is_a_conflict():
    assert reconcile_candidate(_cand(label="totally different"), [RACK_A, RACK_B], []).status == "matched"
    conflict = reconcile_candidate(_cand(label="Rack B"), [RACK_A, RACK_B], [])
    assert conflict.status == "conflict" and any(e["code"] == "label_conflicts_with_position" for e in conflict.evidence)


def test_label_matches_a_rack_that_is_somewhere_else_is_a_conflict_not_a_move():
    far = _cand(x=4000, y=3000, label="Rack A")
    result = reconcile_candidate(far, [RACK_A], [])
    assert result.status == "conflict" and any(e["code"] == "label_matches_but_moved" for e in result.evidence)


def test_nothing_nearby_is_unmatched_and_two_close_racks_are_ambiguous():
    assert reconcile_candidate(_cand(x=5000, y=3000), [RACK_A, RACK_B], []).status == "unmatched"
    between = reconcile_candidate(_cand(x=1350, y=1000), [RACK_A, RACK_B], [])
    assert between.status == "ambiguous"


def test_geometry_overlapping_an_accepted_shape_is_a_duplicate_even_with_new_source_references():
    accepted = ExistingObject("obj-1", 1000, 1000, 600, 1000, 0, "Rack A")
    result = reconcile_candidate(_cand(x=1030, y=990, ref="new-handle-after-reexport"), [RACK_A], [accepted])
    assert result.status == "duplicate" and result.duplicate_of_object_id == "obj-1"
    seen = reconcile_candidate(_cand(ref="old"), [RACK_A], [accepted], {"old": "obj-1"})
    assert any(e["code"] == "source_ref_seen" for e in seen.evidence)


def test_two_candidates_claiming_one_rack_are_both_flagged():
    first = reconcile_candidate(_cand(cid="c1"), [RACK_A], [])
    second = reconcile_candidate(_cand(x=1050, cid="c2"), [RACK_A], [])
    results = {"c1": first, "c2": second}
    resolve_shared_matches(results)
    assert first.status == second.status == "conflict"


@pytest.mark.parametrize("bad", [RackRef("r", "R", "T", 0, 0, 0, 1000, 0), RackRef("r", "R", "T", 0, 0, 600, 0, 0)])
def test_racks_without_usable_dimensions_are_skipped_not_divided_by_zero(bad):
    assert reconcile_candidate(_cand(), [bad], []).status == "unmatched"
