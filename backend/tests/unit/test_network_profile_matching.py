"""Pure matcher tests: vendor longest-prefix, device ranking, firmware bounds and the
refusal to select a profile when the evidence does not single one out."""

import uuid

from app.application.network.profile_matching import (
    AMBIGUOUS,
    MATCHED,
    NO_MATCH,
    VENDOR_ONLY,
    DeviceFacts,
    DeviceView,
    VendorView,
    resolve,
)

CISCO = VendorView(uuid.uuid4(), "cisco", ("1.3.6.1.4.1.9",))
JUNIPER = VendorView(uuid.uuid4(), "juniper", ("1.3.6.1.4.1.2636",))


def device(vendor, code, criteria=(), fmin=None, fmax=None, priority=0):
    return DeviceView(uuid.uuid4(), vendor.id, code, tuple(criteria), fmin, fmax, priority)


def test_vendor_prefix_matches_at_arc_boundary_only():
    facts = DeviceFacts(sys_object_id="1.3.6.1.4.1.99.1.1")
    assert resolve(facts, [CISCO], []).state == NO_MATCH
    assert resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1.5"), [CISCO], []).state == VENDOR_ONLY


def test_missing_sys_object_id_never_matches():
    assert resolve(DeviceFacts(), [CISCO], []).state == NO_MATCH


def test_longest_vendor_prefix_wins():
    nested = VendorView(uuid.uuid4(), "cisco-acme", ("1.3.6.1.4.1.9.1",))
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1.5"), [CISCO, nested], [])
    assert result.state == VENDOR_ONLY and result.vendor_id == nested.id


def test_equal_vendor_prefix_is_ambiguous_and_selects_nothing():
    twin = VendorView(uuid.uuid4(), "cisco-twin", ("1.3.6.1.4.1.9",))
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1.5"), [CISCO, twin], [])
    assert result.state == AMBIGUOUS
    assert result.vendor_id is None and result.device_profile_id is None
    assert set(result.candidate_vendor_ids) == {CISCO.id, twin.id}


def test_single_eligible_device_profile_matches():
    d = device(CISCO, "c9300", [("model", "prefix", "C9300")])
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1.1", model="c9300-48p"), [CISCO], [d])
    assert result.state == MATCHED and result.device_profile_id == d.id


def test_criteria_must_all_hold_and_missing_fact_fails_closed():
    d = device(CISCO, "x", [("model", "equals", "C9300"), ("sys_descr", "contains", "IOS-XE")])
    facts = DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9300")
    assert resolve(facts, [CISCO], [d]).state == VENDOR_ONLY
    facts = DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9300", sys_descr="Cisco IOS-XE 17")
    assert resolve(facts, [CISCO], [d]).state == MATCHED


def test_more_specific_profile_outranks_generic_one():
    generic = device(CISCO, "generic")
    specific = device(CISCO, "specific", [("model", "prefix", "C93")])
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9300"), [CISCO], [generic, specific])
    assert result.state == MATCHED and result.device_profile_id == specific.id


def test_exact_sys_object_id_outranks_prefix():
    prefix = device(CISCO, "prefix", [("sys_object_id", "prefix", "1.3.6.1.4.1.9.1")])
    exact = device(CISCO, "exact", [("sys_object_id", "equals", "1.3.6.1.4.1.9.1.1")])
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1.1"), [CISCO], [prefix, exact])
    assert result.device_profile_id == exact.id


def test_equal_rank_is_ambiguous_not_arbitrary():
    a = device(CISCO, "a", [("model", "prefix", "C93")])
    b = device(CISCO, "b", [("model", "contains", "300")])
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9300"), [CISCO], [a, b])
    assert result.state == AMBIGUOUS
    assert result.device_profile_id is None
    assert set(result.candidate_device_profile_ids) == {a.id, b.id}


def test_priority_breaks_an_otherwise_equal_tie_deliberately():
    a = device(CISCO, "a", [("model", "prefix", "C93")], priority=5)
    b = device(CISCO, "b", [("model", "contains", "300")], priority=1)
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9300"), [CISCO], [a, b])
    assert result.state == MATCHED and result.device_profile_id == a.id


def test_firmware_bounds_are_numeric_not_textual():
    d = device(CISCO, "fw", fmin="9.10", fmax="17.3")
    base = {"sys_object_id": "1.3.6.1.4.1.9.1"}
    assert resolve(DeviceFacts(firmware="9.9", **base), [CISCO], [d]).state == VENDOR_ONLY  # 9.9 < 9.10 numerically
    assert resolve(DeviceFacts(firmware="12.0.1", **base), [CISCO], [d]).state == MATCHED
    assert resolve(DeviceFacts(firmware="17.3.1", **base), [CISCO], [d]).state == VENDOR_ONLY
    assert resolve(DeviceFacts(firmware="17.3", **base), [CISCO], [d]).state == MATCHED


def test_firmware_bound_with_unknown_or_unparseable_firmware_is_ineligible():
    d = device(CISCO, "fw", fmin="9.0")
    base = {"sys_object_id": "1.3.6.1.4.1.9.1"}
    assert resolve(DeviceFacts(**base), [CISCO], [d]).state == VENDOR_ONLY
    assert resolve(DeviceFacts(firmware="16.12.4a", **base), [CISCO], [d]).state == VENDOR_ONLY


def test_device_profiles_of_other_vendors_are_never_considered():
    foreign = device(JUNIPER, "j")
    result = resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1"), [CISCO, JUNIPER], [foreign])
    assert result.state == VENDOR_ONLY and result.vendor_id == CISCO.id


def test_in_operator_and_case_insensitive_text():
    d = device(CISCO, "in", [("model", "in", ("C9200", "C9300"))])
    assert resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="c9300"), [CISCO], [d]).state == MATCHED
    assert resolve(DeviceFacts(sys_object_id="1.3.6.1.4.1.9.1", model="C9400"), [CISCO], [d]).state == VENDOR_ONLY
