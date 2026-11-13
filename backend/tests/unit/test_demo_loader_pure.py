"""Pure parts of the demo loader: signals, slot grid, geometry validation, DXF generation, raw-value inversion."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application.demo_loader.floorplans import build_dxf, validate_geometry
from app.application.demo_loader.provision import free_front_slot
from app.application.demo_loader.selection import RackInfo
from app.application.demo_loader.series import raw_value_for
from app.application.demo_loader.signals import canonical_value, next_slot_after, slot_grid
from app.application.spatial_import.calibration import calibrate_declared_units
from app.application.spatial_import.dxf_parser import parse_dxf
from app.application.telemetry_service import canonical_storage_value

NOW = datetime(2026, 11, 13, 12, 7, 31, tzinfo=UTC)


def rack(tag, x, y, rotation=0, width=600, depth=1200):
    return RackInfo(id=uuid4(), asset_tag=tag, name=tag, site_id=uuid4(), room_id=uuid4(), x_mm=x, y_mm=y, rotation_deg=rotation,
                    height_u=42, width_mm=width, depth_mm=depth)


def test_slot_grid_is_aligned_ascending_and_covers_24_hours():
    slots = slot_grid(NOW, 300, 24)
    assert len(slots) == 289  # 24 h of 5-minute slots, inclusive of both ends
    assert all(int(s.timestamp()) % 300 == 0 for s in slots)
    assert slots == sorted(slots) and slots[-1] <= NOW and (NOW - slots[-1]).total_seconds() < 300
    assert slot_grid(NOW, 300, 24, after=slots[-3]) == slots[-2:]


def test_next_slot_is_strictly_after_now():
    nxt = next_slot_after(NOW, 300)
    assert nxt > NOW and int(nxt.timestamp()) % 300 == 0 and (nxt - NOW).total_seconds() <= 300


@pytest.mark.parametrize("metric", ["temperature_c", "humidity_percent", "power_kw", "load_percent", "availability"])
def test_signals_are_deterministic_bounded_and_varying(metric):
    slots = slot_grid(NOW, 300, 24)
    first = [canonical_value(metric, "series-a", s) for s in slots]
    assert first == [canonical_value(metric, "series-a", s) for s in slots]
    if metric != "availability":
        assert max(first) - min(first) > 0
        assert first != [canonical_value(metric, "series-b", s) for s in slots]
    if metric == "humidity_percent":
        assert all(20 <= v <= 80 for v in first)
    if metric == "temperature_c":
        assert all(10 <= v <= 45 for v in first)
    if metric == "availability":
        assert set(first) == {1.0}


def test_anchored_signal_stays_near_its_anchor():
    values = [canonical_value("temperature_c", "k", s, base=22.5) for s in slot_grid(NOW, 300, 24)]
    assert all(20.5 < v < 24.5 for v in values)


def test_some_racks_run_hot_so_the_heat_map_has_structure():
    centres = [canonical_value("temperature_c", f"rack-{i}", NOW) for i in range(400)]
    assert max(centres) - min(centres) > 6


@pytest.mark.parametrize("unit,scale", [("degC", "1"), ("degF", "1"), ("degC", "0.1")])
def test_raw_value_inverts_the_pinned_conversion(unit, scale):
    revision = SimpleNamespace(source_scale=Decimal(scale), registry_version="1", canonical_metric="temperature_c", source_unit=unit, id=uuid4())
    raw = raw_value_for(revision, 23.4)
    stored, stored_unit = canonical_storage_value("temperature_c", raw, unit, Decimal(scale), "1")
    assert stored_unit == "degC" and abs(float(stored) - 23.4) < 0.001


def test_raw_value_inverts_legacy_unversioned_mappings():
    revision = SimpleNamespace(source_scale=Decimal("2"), registry_version=None, canonical_metric="power_kw", source_unit="kW", id=uuid4())
    assert raw_value_for(revision, 1.0) == Decimal("0.5")


def test_geometry_accepts_a_clean_layout_and_flags_each_defect():
    assert validate_geometry(13400, 12800, [rack("A", 1000, 1000), rack("B", 1800, 1000), rack("C", 1000, 3400, rotation=90)]) == []
    problems = validate_geometry(13400, 12800, [rack("A", 1000, 1000), rack("B", 1300, 1000), rack("C", 13000, 1000), rack("D", None, None),
                                                rack("E", 5000, 5000, rotation=45)])
    text = " | ".join(problems)
    assert "A overlaps B" in text and "C: footprint" in text and "D: no position" in text and "E: rotation 45" in text
    # a quarter turn swaps width and depth: in a 1,000 mm deep room this rack only fits when turned
    assert validate_geometry(13400, 1000, [rack("R", 100, 100, rotation=0)])
    assert validate_geometry(13400, 1000, [rack("R", 100, 100, rotation=90)]) == []


def test_generated_dxf_parses_declares_millimetres_and_calibrates_to_the_identity():
    racks = [rack("A", 1000, 1000), rack("B", 1800, 1000), rack("C", 1000, 3400, rotation=90)]
    document = parse_dxf(build_dxf(13400, 12800, racks))
    assert document.source_units == "mm" and document.units_trusted and document.y_axis == "up"
    params = calibrate_declared_units(document)
    assert params.method == "declared_units" and params.mm_per_unit == 1.0
    # the room outline corner (0, 0) in canonical room coordinates; Y flips because DXF Y points up
    assert params.to_canonical(0.0, 12800.0) == (0.0, 0.0)
    assert params.to_canonical(1000.0, 12800.0 - 1000.0) == (1000.0, 1000.0)
    assert params.confidence == "high"


def test_free_front_slot_searches_outward_from_two_thirds_up():
    assert free_front_slot(42, set()) == 28
    assert free_front_slot(42, {28}) == 27
    assert free_front_slot(42, {27, 28}) == 29
    assert free_front_slot(4, {1, 2, 3, 4}) is None
