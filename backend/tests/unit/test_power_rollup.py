"""Issue #102 area B: pure rollup rules, including the A/B regressions that fail on a naive A + B sum."""

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.application.power_rollup import EdgeIn, EquipmentIn, NodeIn, compute_rollup

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def uid() -> uuid.UUID:
    return uuid.uuid4()


class Dual:
    """UPS-A -> brk-A -> inlet-A and UPS-B -> brk-B -> inlet-B feeding one server."""

    def __init__(self, *, cap_a=10.0, cap_b=10.0, ups_cap=20.0, a_state="closed", b_state="closed"):
        self.ups_a, self.ups_b, self.brk_a, self.brk_b = uid(), uid(), uid(), uid()
        self.in_a, self.in_b, self.eq = uid(), uid(), uid()
        self.nodes = [
            NodeIn(self.ups_a, "ups", "UPS-A", capacity_kw=ups_cap),
            NodeIn(self.ups_b, "ups", "UPS-B", capacity_kw=ups_cap),
            NodeIn(self.brk_a, "protection_device", "BRK-A", capacity_kw=15.0, rated_kw=15.0, state=a_state),
            NodeIn(self.brk_b, "protection_device", "BRK-B", capacity_kw=15.0, rated_kw=15.0, state=b_state),
            NodeIn(self.in_a, "equipment_power_input", "in-A"),
            NodeIn(self.in_b, "equipment_power_input", "in-B"),
        ]
        self.edges = [
            EdgeIn(self.ups_a, self.brk_a, "A"), EdgeIn(self.brk_a, self.in_a, "A"),
            EdgeIn(self.ups_b, self.brk_b, "B"), EdgeIn(self.brk_b, self.in_b, "B"),
        ]
        self.cap_a, self.cap_b = cap_a, cap_b

    def equipment(self, **kw):
        defaults = {"inlets": ((self.in_a, self.cap_a), (self.in_b, self.cap_b))}
        defaults.update(kw)
        return EquipmentIn(self.eq, "server", **defaults)

    def run(self, eq: EquipmentIn):
        return compute_rollup(self.nodes, self.edges, [eq], now=NOW)


def measured(**kw):
    return {"measured_kw": 10.0, "measured_at": NOW - timedelta(seconds=30), **kw}


def test_normal_dual_feed_counts_demand_once():
    d = Dual()
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "normal_dual_feed"
    assert r.nodes[d.ups_a].load_kw == pytest.approx(5.0)
    assert r.nodes[d.ups_b].load_kw == pytest.approx(5.0)
    # Naive A + B would report 20 kW at the site (each feed carrying the full 10).
    assert r.site.load_kw == pytest.approx(10.0)
    assert r.nodes[d.ups_a].load_kw + r.nodes[d.ups_b].load_kw == pytest.approx(r.site.load_kw)
    assert r.site.capacity_kw == pytest.approx(40.0) and r.site.headroom_kw == pytest.approx(30.0)
    assert r.nodes[d.ups_a].quality == "measured"


def test_one_feed_failed_moves_full_load_to_the_survivor():
    d = Dual(a_state="tripped")
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "one_feed_failed"
    assert r.nodes[d.ups_a].load_kw == 0 and r.nodes[d.ups_b].load_kw == pytest.approx(10.0)
    assert r.nodes[d.brk_b].load_kw == pytest.approx(10.0) and r.nodes[d.brk_b].level == "ok"
    assert r.site.load_kw == pytest.approx(10.0)
    assert any("redundancy lost" in w for w in r.warnings)


def test_survivor_overloads_when_the_failed_side_load_moves_over():
    d = Dual(b_state="open")
    r = d.run(d.equipment(**measured(measured_kw=18.0)))
    assert r.nodes[d.brk_a].load_kw == pytest.approx(18.0)
    assert r.nodes[d.brk_a].overloaded and r.nodes[d.brk_a].level == "overload"
    assert r.nodes[d.brk_a].headroom_kw == pytest.approx(-3.0)  # negative headroom is kept, not clamped


def test_one_feed_missing_when_second_inlet_has_no_connection():
    d = Dual()
    d.edges = [e for e in d.edges if e.target not in (d.in_b, d.brk_b)]
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "one_feed_missing"
    assert r.nodes[d.ups_a].load_kw == pytest.approx(10.0)


def test_asymmetric_feeds_split_by_inlet_capacity_and_still_sum_once():
    d = Dual(cap_a=6.0, cap_b=2.0)
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "asymmetric_feeds"
    assert r.nodes[d.ups_a].load_kw == pytest.approx(7.5) and r.nodes[d.ups_b].load_kw == pytest.approx(2.5)
    assert r.site.load_kw == pytest.approx(10.0)
    # allocated uses the A/B max, not the sum
    assert r.equipment[d.eq].allocated_kw == pytest.approx(6.0)


def test_estimated_only_uses_nameplate_and_says_so():
    d = Dual(cap_a=4.0, cap_b=4.0)
    r = d.run(d.equipment())
    assert r.equipment[d.eq].quality == "estimated"
    assert r.site.load_kw == pytest.approx(4.0)  # max(4, 4), never 8
    assert r.nodes[d.ups_a].quality == "estimated" and r.site.quality == "estimated"


def test_stale_measurement_is_flagged_not_dropped():
    d = Dual()
    r = d.run(d.equipment(measured_kw=9.0, measured_at=NOW - timedelta(hours=3)))
    assert r.equipment[d.eq].quality == "stale"
    assert r.site.load_kw == pytest.approx(9.0) and r.site.quality == "stale"


def test_missing_data_is_not_zero_load_with_confidence():
    d = Dual(cap_a=None, cap_b=None)
    r = d.run(d.equipment())
    assert r.equipment[d.eq].quality == "missing" and r.equipment[d.eq].demand_kw is None
    assert r.nodes[d.ups_a].quality == "missing" and r.nodes[d.ups_a].missing_count == 1
    assert r.site.quality == "missing"


def test_unknown_protection_state_is_not_treated_as_interrupting():
    d = Dual(a_state="unknown")
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "normal_dual_feed"
    assert r.nodes[d.brk_a].warnings and "unknown" in r.nodes[d.brk_a].warnings[0]
    assert r.nodes[d.brk_a].interrupted is False


def test_unserved_when_every_path_is_cut():
    d = Dual(a_state="open", b_state="tripped")
    r = d.run(d.equipment(**measured()))
    assert r.equipment[d.eq].scenario == "unserved" and not r.equipment[d.eq].served
    assert r.site.load_kw == 0 and r.site.unserved_kw == pytest.approx(10.0)


def test_shared_upstream_root_sees_demand_once():
    util, pdu_a, pdu_b = uid(), uid(), uid()
    in_a, in_b, eq = uid(), uid(), uid()
    nodes = [
        NodeIn(util, "utility_intake", "Utility", capacity_kw=50.0), NodeIn(pdu_a, "pdu", "PDU-A"),
        NodeIn(pdu_b, "pdu", "PDU-B"), NodeIn(in_a, "equipment_power_input", "a"), NodeIn(in_b, "equipment_power_input", "b"),
    ]
    edges = [
        EdgeIn(util, pdu_a, "A"), EdgeIn(util, pdu_b, "B"), EdgeIn(pdu_a, in_a, "A"), EdgeIn(pdu_b, in_b, "B"),
    ]
    item = EquipmentIn(eq, "s", ((in_a, 5.0), (in_b, 5.0)), measured_kw=8.0, measured_at=NOW)
    r = compute_rollup(nodes, edges, [item], now=NOW)
    assert r.nodes[util].load_kw == pytest.approx(8.0)  # not 16
    assert r.nodes[pdu_a].load_kw == pytest.approx(4.0)


def test_rack_and_room_add_each_server_once():
    d = Dual()
    rack, room = uid(), uid()
    r = d.run(d.equipment(**measured(), rack_id=rack, room_id=room))
    assert r.racks[rack].load_kw == pytest.approx(10.0) and r.rooms[room].load_kw == pytest.approx(10.0)


def test_2n_tier_capacity_is_halved():
    d = Dual()
    d.nodes = [NodeIn(n.id, n.node_type, n.label, capacity_kw=n.capacity_kw, rated_kw=n.rated_kw, state=n.state,
                      redundancy_factor="2N" if n.node_type == "ups" else None) for n in d.nodes]
    r = d.run(d.equipment(**measured()))
    assert r.site.capacity_kw == pytest.approx(20.0) and r.site.capacity_basis == "ups"


def test_result_is_independent_of_input_order():
    d = Dual(a_state="closed")
    eq = d.equipment(**measured())
    base = d.run(eq)
    nodes, edges = list(d.nodes), list(d.edges)
    random.Random(7).shuffle(nodes)
    random.Random(8).shuffle(edges)
    shuffled = compute_rollup(nodes, edges, [eq], now=NOW)
    assert {k: (v.load_kw, v.quality) for k, v in base.nodes.items()} == {k: (v.load_kw, v.quality) for k, v in shuffled.nodes.items()}
    assert base.site == shuffled.site


def test_equipment_with_no_power_model_is_reported():
    d = Dual()
    r = d.run(EquipmentIn(d.eq, "x", (), measured_kw=3.0, measured_at=NOW))
    assert r.equipment[d.eq].scenario == "no_power_modeled" and r.site.load_kw == 0
